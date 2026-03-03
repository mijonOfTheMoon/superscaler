import math
import time
import logging

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

        # Per target state keyed by target name. Each value is a dict:
        # last_tick, last_up, last_down, cooldown_up, cooldown_down, pending
        self._state = {}

        for target in config.targets:
            self._ensure_target_state(target)

    def _ensure_target_state(self, target):
        """Create state entry for a target if it does not exist."""
        if target.name not in self._state:
            self._state[target.name] = {
                'last_tick': 0.0,
                'last_up': 0.0,
                'last_down': 0.0,
                'cooldown_up': target.cooldown_up,
                'cooldown_down': target.cooldown_down,
                'pending': [],
            }

    def reload_config(self, new_config, queue_monitors=None):
        """Apply new config after sighup while preserving pending state."""
        old_names = {t.name for t in self.config.targets}
        new_names = {t.name for t in new_config.targets}
        self.config = new_config

        if queue_monitors is not None:
            self.queue_monitors = queue_monitors

        # Remove state for deleted targets
        for removed in old_names - new_names:
            self._state.pop(removed, None)

        # Add state for new targets and update existing cooldown params
        for target in new_config.targets:
            self._ensure_target_state(target)
            state = self._state[target.name]
            state['cooldown_up'] = target.cooldown_up
            state['cooldown_down'] = target.cooldown_down

        logger.info('Config reloaded: %d target(s)', len(new_config.targets))

    def tick(self):
        """Process all targets that are due for evaluation."""
        now = time.monotonic()

        for target in self.config.targets:
            state = self._state[target.name]
            if now - state['last_tick'] < target.poll_interval:
                continue

            state['last_tick'] = now
            try:
                self._process_target(target, state, now)
            except Exception:
                logger.exception('[%s] Tick error', target.name)

    def _process_target(self, target, state, now):
        """Evaluate and act on a single target.

        Scale up and scale down operations are blocked while pending ones exist
        to prevent cascading stops and configuration divergence.
        """
        pending = state['pending']

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
        processes = info['processes']
        proc_states = {p['name']: p['statename'] for p in processes}

        stopped_or_zombie = set()
        still_pending = []

        # Check pending processes
        if pending:
            pending_names = set(pending)
            for name in pending:
                s = proc_states.get(name)
                if s in STOPPED_STATES or s is None:
                    stopped_or_zombie.add(name)
                else:
                    still_pending.append(name)
        else:
            pending_names = set()

        # Check for zombies (processes that stopped unexpectedly, not pending)
        for p in processes:
            name = p['name']
            if (p['statename'] in STOPPED_STATES
                    and name not in pending_names
                    and name not in stopped_or_zombie):
                stopped_or_zombie.add(name)

        # Remove confirmed stopped and zombie processes
        if stopped_or_zombie:
            try:
                names_list = list(stopped_or_zombie)
                self.supervisor.confirm_scale_down(
                    target.program_name, names_list)
                logger.info('[%s] Removed %d stopped/zombie processes: %s',
                            target.name, len(names_list), names_list)

                # Update processes locally to avoid an extra RPC call
                processes = [
                    p for p in processes if p['name'] not in stopped_or_zombie
                ]
            except Exception:
                logger.exception(
                    '[%s] Confirm scale down failed for %s',
                    target.name, list(stopped_or_zombie))
                # Keep original pending ones in the list for next retry
                for name in stopped_or_zombie:
                    if name in pending_names:
                        still_pending.append(name)

        state['pending'] = still_pending

        # Count active processes
        active = sum(
            1 for p in processes
            if p['statename'] in ACTIVE_STATES
        )

        # Scale up only when no pending operations exist
        if desired > active and not still_pending:
            if now - state['last_up'] >= state['cooldown_up']:
                total_procs = len(processes)
                count = min(target.scale_up_step,
                            target.max_workers - total_procs)
                if count > 0:
                    try:
                        added = self.supervisor.scale_up(
                            target.program_name, count)
                        state['last_up'] = now
                        logger.info(
                            '[%s] Scaled up +%d: %s (queue=%d)',
                            target.name, count, added, queue_len)
                    except Exception:
                        logger.exception('[%s] Scale up failed',
                                         target.name)

        # Scale down only when no pending operations exist
        elif desired < active and not still_pending:
            if now - state['last_down'] >= state['cooldown_down']:
                count = min(target.scale_down_step,
                            active - target.min_workers)
                if count > 0:
                    try:
                        stopping = self.supervisor.scale_down(
                            target.program_name, count)
                        state['pending'] = list(stopping)
                        state['last_down'] = now
                        logger.info(
                            '[%s] Scaled down -%d: %s (queue=%d)',
                            target.name, count, stopping, queue_len)
                    except Exception:
                        logger.exception('[%s] Scale down failed',
                                         target.name)