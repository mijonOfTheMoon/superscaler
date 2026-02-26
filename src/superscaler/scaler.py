import math
import time
import logging

from superscaler.cooldown import CooldownManager

logger = logging.getLogger('superscaler')

# States considered active, these count toward current worker count
ACTIVE_STATES = frozenset({'RUNNING', 'STARTING', 'BACKOFF'})

# States considered stopped, safe to confirm removal
STOPPED_STATES = frozenset({'STOPPED', 'EXITED', 'FATAL', 'UNKNOWN'})

class ScalerEngine:
    """Core scaling loop that processes all configured targets independently.

    Each target maintains its own cooldown timers and pending scale down
    state. Scale up is always allowed even when a scale down operation is
    pending. New scale down operations are blocked while a previous one
    is still being confirmed.
    """

    def __init__(self, config, redis_monitor, supervisor_client):
        self.config = config
        self.redis = redis_monitor
        self.supervisor = supervisor_client
        self.running = True

        # Per target state dictionaries keyed by target name
        self.cooldowns = {}
        self.pending_scale_down = {}
        self.last_tick = {}

        self._init_targets()

    def _init_targets(self):
        """Initialize per target state from config."""
        for target in self.config.targets:
            self.cooldowns[target.name] = CooldownManager(
                target.cooldown_up, target.cooldown_down)
            if target.name not in self.pending_scale_down:
                self.pending_scale_down[target.name] = []
            self.last_tick[target.name] = 0.0

    def reload_config(self, new_config):
        """Apply new config after sighup while preserving pending state."""
        old_names = {t.name for t in self.config.targets}
        new_names = {t.name for t in new_config.targets}
        self.config = new_config

        # Remove state for deleted targets
        for removed in old_names - new_names:
            self.cooldowns.pop(removed, None)
            self.pending_scale_down.pop(removed, None)
            self.last_tick.pop(removed, None)

        # Add state for new targets and update existing cooldown params
        for target in new_config.targets:
            if target.name not in self.cooldowns:
                self.cooldowns[target.name] = CooldownManager(
                    target.cooldown_up, target.cooldown_down)
                self.pending_scale_down[target.name] = []
                self.last_tick[target.name] = 0.0
            else:
                cd = self.cooldowns[target.name]
                cd.cooldown_up = target.cooldown_up
                cd.cooldown_down = target.cooldown_down

        logger.info('Config reloaded: %d target(s)', len(new_config.targets))

    def tick(self):
        """Process all targets that are due for evaluation."""
        now = time.monotonic()

        for target in self.config.targets:
            elapsed = now - self.last_tick.get(target.name, 0.0)
            if elapsed < target.poll_interval:
                continue

            self.last_tick[target.name] = now
            try:
                self._process_target(target)
            except Exception:
                logger.exception('[%s] Tick error', target.name)

    def _process_target(self, target):
        """Evaluate and act on a single target.

        Scale up is allowed even when there are pending scale down operations.
        New scale down operations are blocked while pending ones exist to
        prevent cascading stops.
        """
        cooldown = self.cooldowns[target.name]
        pending = self.pending_scale_down[target.name]

        # Step 1: Check and clean up pending scale down operations
        if pending:
            self._check_pending_scale_down(target)

        # Step 2: Clear timed out pending entries
        self._check_pending_timeout(target)

        # Step 3: Poll redis queue length
        try:
            queue_len = self.redis.get_queue_length(target.queue_key)
        except Exception:
            logger.warning('[%s] Redis unavailable, skipping tick',
                           target.name)
            return

        # Step 4: Calculate desired worker count
        desired = math.ceil(queue_len / target.tasks_per_worker)
        desired = max(target.min_workers, min(target.max_workers, desired))

        # Step 5: Get current active worker count from supervisor
        try:
            info = self.supervisor.get_group_info(target.group_name)
        except Exception:
            logger.warning('[%s] Supervisor unavailable, skipping tick',
                           target.name)
            return

        active = sum(
            1 for p in info['processes']
            if p['statename'] in ACTIVE_STATES
        )

        # Step 6: Scale up is always allowed, even during pending scale down
        if desired > active:
            if cooldown.can_scale_up():
                count = min(target.scale_up_step,
                            target.max_workers - active)
                if count > 0:
                    try:
                        added = self.supervisor.scale_up(
                            target.group_name, count)
                        cooldown.mark_scale_up()
                        logger.info(
                            '[%s] Scaled up +%d: %s (queue=%d)',
                            target.name, count, added, queue_len)
                    except Exception:
                        logger.exception('[%s] Scale up failed',
                                         target.name)

        # Step 7: Scale down only when no pending operations exist
        elif desired < active and not self.pending_scale_down[target.name]:
            if cooldown.can_scale_down():
                count = min(target.scale_down_step,
                            active - target.min_workers)
                if count > 0:
                    try:
                        stopping = self.supervisor.scale_down(
                            target.group_name, count)
                        self.pending_scale_down[target.name] = [
                            {'name': n, 'started': time.monotonic()}
                            for n in stopping
                        ]
                        cooldown.mark_scale_down()
                        logger.info(
                            '[%s] Scaled down -%d: %s (queue=%d)',
                            target.name, count, stopping, queue_len)
                    except Exception:
                        logger.exception('[%s] Scale down failed',
                                         target.name)

    def _check_pending_scale_down(self, target):
        """Poll pending processes and confirm those that have stopped.

        Processes that are confirmed stopped are removed from the supervisor
        group via confirm_scale_down. Processes still stopping or unreachable
        remain in the pending list for the next check.
        """
        pending = self.pending_scale_down[target.name]
        if not pending:
            return

        stopped = []
        still_stopping = []

        for entry in pending:
            namespec = '%s:%s' % (target.group_name, entry['name'])
            try:
                proc_info = self.supervisor.get_process_info(namespec)
                if proc_info['statename'] in STOPPED_STATES:
                    stopped.append(entry['name'])
                else:
                    still_stopping.append(entry)
            except Exception:
                logger.warning(
                    '[%s] Cannot get info for %s, keeping pending',
                    target.name, namespec)
                still_stopping.append(entry)

        if stopped:
            try:
                self.supervisor.confirm_scale_down(
                    target.group_name, stopped)
                logger.info('[%s] Scale down confirmed: removed %s',
                            target.name, stopped)
            except Exception:
                logger.exception(
                    '[%s] Confirm scale down failed for %s',
                    target.name, stopped)
                # Keep them in pending with original timestamps so we retry
                for name in stopped:
                    original = next(
                        (e for e in pending if e['name'] == name), None)
                    if original:
                        still_stopping.append(original)

        self.pending_scale_down[target.name] = still_stopping

    def _check_pending_timeout(self, target):
        """Clear pending entries that have exceeded the configured timeout.

        When a process takes too long to stop or supervisor loses contact,
        the pending entry is dropped with a warning so that the target is
        not blocked indefinitely.
        """
        pending = self.pending_scale_down[target.name]
        if not pending:
            return

        now = time.monotonic()
        timeout = target.pending_timeout
        timed_out = []
        remaining = []

        for entry in pending:
            if now - entry['started'] >= timeout:
                timed_out.append(entry['name'])
            else:
                remaining.append(entry)

        if timed_out:
            logger.warning(
                '[%s] Pending scale down timed out after %ds for: %s',
                target.name, timeout, timed_out)

        self.pending_scale_down[target.name] = remaining