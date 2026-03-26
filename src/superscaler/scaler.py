import math
import time
import logging

from superscaler.pm2_client import pm2_get_group_info, pm2_scale_up, pm2_scale_down, PM2Error

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

    def __init__(self, config, queue_monitors, node_clients=None):
        self.config = config
        self.queue_monitors = queue_monitors
        self.node_clients = node_clients if node_clients is not None else {}
        self.running = True

        # Backward compat: expose first node client as self.supervisor
        # so _process_supervisor_target keeps working until task 4.2
        # rewrites it for multi-node support.
        if self.node_clients:
            self.supervisor = next(iter(self.node_clients.values()))
        else:
            self.supervisor = None

        # Per target state keyed by target name. Each value is a dict:
        # last_tick, last_up, last_down, pending
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
                'pending': {},
            }

    def reload_config(self, new_config, queue_monitors=None,
                      node_clients=None):
        """Apply new config after sighup while preserving pending state."""
        old_names = {t.name for t in self.config.targets}
        new_names = {t.name for t in new_config.targets}
        self.config = new_config

        if queue_monitors is not None:
            self.queue_monitors = queue_monitors

        if node_clients is not None:
            self.node_clients = node_clients
            # Update backward compat reference
            if self.node_clients:
                self.supervisor = next(iter(self.node_clients.values()))
            else:
                self.supervisor = None

        # Remove state for deleted targets
        for removed in old_names - new_names:
            self._state.pop(removed, None)

        # Add state for new targets
        for target in new_config.targets:
            self._ensure_target_state(target)

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
        """Dispatch to the appropriate target handler based on type."""
        if target.type == 'pm2':
            self._process_pm2_target(target, state, now)
        else:
            self._process_supervisor_target(target, state, now)

    def _process_pm2_target(self, target, state, now):
        """Evaluate and act on a single PM2 target.

        No pending state tracking — PM2 removes processes synchronously.
        """
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

        desired = math.ceil(queue_len / target.tasks_per_worker)
        desired = max(target.min_workers, min(target.max_workers, desired))

        try:
            info = pm2_get_group_info(
                target.program_name,
                pm2_path=target.pm2_path,
                pm2_home=target.pm2_home,
                run_as_user=target.run_as_user)
        except PM2Error as exc:
            logger.warning('[%s] %s', target.name, exc)
            return
        except Exception as exc:
            logger.warning('[%s] PM2 unavailable: %s', target.name, exc)
            return

        processes = info['processes']
        active = sum(1 for p in processes if p['statename'] in ACTIVE_STATES)

        if desired > active:
            if now - state['last_up'] >= target.cooldown_up:
                count = min(target.scale_up_step,
                            target.max_workers - active)
                if count > 0:
                    try:
                        added = pm2_scale_up(
                            target.program_name, count,
                            pm2_path=target.pm2_path,
                            pm2_home=target.pm2_home,
                            run_as_user=target.run_as_user)
                        state['last_up'] = now
                        logger.info('[%s] PM2 scaled up +%d: %s (queue=%d)',
                                    target.name, count, added, queue_len)
                    except PM2Error as exc:
                        logger.error('[%s] PM2 scale up failed: %s',
                                     target.name, exc)
                    except Exception as exc:
                        logger.error('[%s] PM2 scale up failed: %s',
                                     target.name, exc)

        elif desired < active:
            if now - state['last_down'] >= target.cooldown_down:
                count = min(target.scale_down_step,
                            active - target.min_workers)
                if count > 0:
                    desired_count = active - count
                    try:
                        pm2_scale_down(
                            target.program_name, desired_count,
                            pm2_path=target.pm2_path,
                            pm2_home=target.pm2_home,
                            run_as_user=target.run_as_user)
                        state['last_down'] = now
                        logger.info('[%s] PM2 scaled down to %d (queue=%d)',
                                    target.name, desired_count, queue_len)
                    except PM2Error as exc:
                        logger.error('[%s] PM2 scale down failed: %s',
                                     target.name, exc)
                    except Exception as exc:
                        logger.error('[%s] PM2 scale down failed: %s',
                                     target.name, exc)

    def _confirm_pending_per_node(self, target, state):
        """Confirm pending scale-down processes independently per node.

        For each node that has pending processes, query the node for
        current group info and check if pending processes have reached
        STOPPED_STATES. Confirm removal per-node independently so that
        failure on one node does not block others.

        Returns a dict of node_name -> active count for nodes that were
        checked, and updates state['pending'] in place.
        """
        pending = state['pending']
        new_pending = {}

        for node_name, proc_names in list(pending.items()):
            if not proc_names:
                continue

            client = self.node_clients.get(node_name)
            if client is None:
                # Node no longer configured, drop its pending state
                continue

            try:
                info = client.get_group_info(target.program_name)
            except Exception:
                logger.warning(
                    '[%s][%s] Node unreachable during pending confirmation, '
                    'keeping pending state for retry',
                    target.name, node_name)
                new_pending[node_name] = list(proc_names)
                continue

            processes = info['processes']
            current_names = {p['name'] for p in processes}
            pending_set = set(proc_names)

            # Identify processes that have stopped or disappeared
            stopped = set()
            still_pending = []
            for p in processes:
                if p['name'] in pending_set:
                    if p['statename'] in STOPPED_STATES:
                        stopped.add(p['name'])
                    else:
                        still_pending.append(p['name'])

            # Processes that disappeared entirely are also done
            for name in pending_set:
                if name not in current_names:
                    stopped.add(name)

            # Confirm removal of stopped processes on this node
            if stopped:
                confirmable = [n for n in stopped if n in current_names]
                try:
                    if confirmable:
                        client.confirm_scale_down(
                            target.program_name, confirmable)
                        logger.info(
                            '[%s][%s] Removed %d stopped processes: %s',
                            target.name, node_name,
                            len(confirmable), confirmable)
                except Exception as exc:
                    logger.error(
                        '[%s][%s] Confirm scale down failed: %s',
                        target.name, node_name, exc)
                    # Keep them for retry
                    still_pending.extend(
                        n for n in stopped if n in pending_set)

            if still_pending:
                new_pending[node_name] = still_pending

        state['pending'] = new_pending

    def _process_supervisor_target(self, target, state, now):
        """Evaluate and act on a multi-node supervisor target.

        Gathers info from all nodes, confirms pending scale-downs
        per-node independently, then distributes scale up (least-loaded
        first) or scale down (most-loaded first) across reachable nodes.
        Per-node pending state only blocks scaling on that specific node.
        """
        # --- Phase 1: Confirm pending scale-downs per node ---
        self._confirm_pending_per_node(target, state)
        pending = state['pending']

        # --- Phase 2: Poll queue length ---
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

        # --- Phase 3: Gather per-node info ---
        # node_info: {node_name: {'active': int, 'processes': list,
        #                         'client': NodeClient}}
        node_info = {}
        for node_name in target.nodes:
            client = self.node_clients.get(node_name)
            if client is None:
                logger.warning('[%s][%s] Node client not found, skipping',
                               target.name, node_name)
                continue
            try:
                info = client.get_group_info(target.program_name)
            except Exception:
                logger.warning('[%s][%s] Node unreachable, skipping',
                               target.name, node_name)
                continue

            processes = info['processes']
            active = sum(
                1 for p in processes if p['statename'] in ACTIVE_STATES)
            node_info[node_name] = {
                'active': active,
                'processes': processes,
                'client': client,
            }

        if not node_info:
            logger.warning('[%s] No reachable nodes, skipping tick',
                           target.name)
            return

        # --- Phase 4: Calculate desired workers ---
        total_active = sum(ni['active'] for ni in node_info.values())
        desired = math.ceil(queue_len / target.tasks_per_worker)
        desired = max(target.min_workers, min(target.max_workers, desired))

        # --- Phase 5: Scale up (least-loaded first) ---
        if desired > total_active:
            if now - state['last_up'] >= target.cooldown_up:
                total_to_add = min(target.scale_up_step,
                                   target.max_workers - total_active)
                if total_to_add > 0:
                    # Sort nodes by active count ascending (least-loaded)
                    # Only consider nodes without pending operations
                    scalable = [
                        (name, ni) for name, ni in node_info.items()
                        if name not in pending
                    ]
                    if scalable:
                        added_any = False
                        remaining = total_to_add
                        # Build mutable active counts for distribution
                        active_counts = {
                            name: ni['active']
                            for name, ni in scalable
                        }
                        while remaining > 0 and active_counts:
                            # Pick the least-loaded node
                            least = min(active_counts,
                                        key=active_counts.get)
                            try:
                                added = node_info[least]['client'].scale_up(
                                    target.program_name, 1)
                                active_counts[least] += 1
                                remaining -= 1
                                added_any = True
                                logger.info(
                                    '[%s][%s] Scaled up +1: %s (queue=%d)',
                                    target.name, least, added, queue_len)
                            except Exception as exc:
                                logger.error(
                                    '[%s][%s] Scale up failed: %s',
                                    target.name, least, exc)
                                # Remove this node from candidates
                                del active_counts[least]
                        if added_any:
                            state['last_up'] = now

        # --- Phase 6: Scale down (most-loaded first) ---
        elif desired < total_active:
            if now - state['last_down'] >= target.cooldown_down:
                total_to_remove = min(target.scale_down_step,
                                      total_active - target.min_workers)
                if total_to_remove > 0:
                    # Sort nodes by active count descending (most-loaded)
                    # Only consider nodes without pending operations
                    scalable = [
                        (name, ni) for name, ni in node_info.items()
                        if name not in pending
                    ]
                    if scalable:
                        removed_any = False
                        remaining = total_to_remove
                        active_counts = {
                            name: ni['active']
                            for name, ni in scalable
                        }
                        while remaining > 0 and active_counts:
                            # Pick the most-loaded node
                            most = max(active_counts,
                                       key=active_counts.get)
                            if active_counts[most] <= 0:
                                break
                            try:
                                stopping = node_info[most][
                                    'client'].scale_down(
                                    target.program_name, 1)
                                # Track pending per-node
                                if most not in pending:
                                    pending[most] = []
                                pending[most].extend(stopping)
                                active_counts[most] -= 1
                                remaining -= 1
                                removed_any = True
                                logger.info(
                                    '[%s][%s] Scaled down -1: %s (queue=%d)',
                                    target.name, most, stopping, queue_len)
                            except Exception as exc:
                                logger.error(
                                    '[%s][%s] Scale down failed: %s',
                                    target.name, most, exc)
                                del active_counts[most]
                        if removed_any:
                            state['last_down'] = now