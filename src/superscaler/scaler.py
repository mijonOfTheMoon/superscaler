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
    state. New scaling operations are blocked while a previous one
    is still being confirmed. Each target looks up its own queue monitor
    from the monitors dict based on the target queue reference.
    """

    def __init__(self, config, queue_monitors, supervisor_client):
        self.config = config
        self.queue_monitors = queue_monitors
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

    def reload_config(self, new_config, queue_monitors=None):
        """Apply new config after sighup while preserving pending state."""
        old_names = {t.name for t in self.config.targets}
        new_names = {t.name for t in new_config.targets}
        self.config = new_config

        if queue_monitors is not None:
            self.queue_monitors = queue_monitors

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

        Scale up and scale down operations are blocked while pending ones exist
        to prevent cascading stops and configuration divergence.
        """
        cooldown = self.cooldowns[target.name]
        pending = self.pending_scale_down[target.name]

        # Poll queue length from the target queue backend
        monitor = self.queue_monitors.get(target.queue)
        if monitor is None:
            logger.error('[%s] Queue backend %r not found, skipping tick',
                         target.name, target.queue)
            return

        try:
            queue_len = monitor.get_queue_length(target.queue_key)
        except Exception:
            logger.warning('[%s] Queue unavailable, skipping tick',
                           target.name)
            return

        # Calculate desired worker count
        desired = math.ceil(queue_len / target.tasks_per_worker)
        desired = max(target.min_workers, min(target.max_workers, desired))

        # Get current group info from supervisor (only ONE RPC call per tick)
        try:
            info = self.supervisor.get_group_info(target.program_name)
        except Exception:
            logger.warning('[%s] Supervisor unavailable, skipping tick',
                           target.name)
            return

        # Process pending scale downs and find zombies
        proc_states = {p['name']: p['statename'] for p in info['processes']}
        
        stopped_or_zombie = []
        still_stopping = []

        # Check pending processes
        for entry in pending:
            p_name = entry['name']
            state = proc_states.get(p_name)
            
            # If the process is no longer reported by supervisor, or is properly stopped
            if state in STOPPED_STATES or state is None:
                stopped_or_zombie.append(p_name)
            else:
                still_stopping.append(entry)

        # Check for zombies (processes that stopped unexpectedly, not in pending)
        pending_names = {entry['name'] for entry in pending}
        for p in info['processes']:
            if p['statename'] in STOPPED_STATES and p['name'] not in pending_names and p['name'] not in stopped_or_zombie:
                stopped_or_zombie.append(p['name'])
                
        # Remove confirmed stopped and zombie processes efficiently
        if stopped_or_zombie:
            try:
                self.supervisor.confirm_scale_down(
                    target.program_name, stopped_or_zombie)
                logger.info('[%s] Removed %d stopped/zombie processes: %s',
                            target.name, len(stopped_or_zombie), stopped_or_zombie)
                
                # Update info['processes'] locally to avoid an extra RPC call
                info['processes'] = [
                    p for p in info['processes'] if p['name'] not in stopped_or_zombie
                ]
            except Exception:
                logger.exception(
                    '[%s] Confirm scale down failed for %s',
                    target.name, stopped_or_zombie)
                # Keep original pending ones in the pending list for next retry
                for name in stopped_or_zombie:
                    original = next(
                        (e for e in pending if e['name'] == name), None)
                    if original:
                        still_stopping.append(original)

        self.pending_scale_down[target.name] = still_stopping

        # Count active processes
        active = sum(
            1 for p in info['processes']
            if p['statename'] in ACTIVE_STATES
        )

        # Scale up only when no pending operations exist
        if desired > active and not self.pending_scale_down[target.name]:
            if cooldown.can_scale_up():
                total_procs = len(info['processes'])
                count = min(target.scale_up_step,
                            target.max_workers - total_procs)
                if count > 0:
                    try:
                        added = self.supervisor.scale_up(
                            target.program_name, count)
                        cooldown.mark_scale_up()
                        logger.info(
                            '[%s] Scaled up +%d: %s (queue=%d)',
                            target.name, count, added, queue_len)
                    except Exception:
                        logger.exception('[%s] Scale up failed',
                                         target.name)

        # Scale down only when no pending operations exist
        elif desired < active and not self.pending_scale_down[target.name]:
            if cooldown.can_scale_down():
                count = min(target.scale_down_step,
                            active - target.min_workers)
                if count > 0:
                    try:
                        stopping = self.supervisor.scale_down(
                            target.program_name, count)
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