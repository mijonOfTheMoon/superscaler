import os
import glob
import configparser
import logging

from supervisor.states import SupervisorStates, ProcessStates
from supervisor.states import RUNNING_STATES, STOPPED_STATES
from supervisor.states import getProcessStateDescription
from supervisor.xmlrpc import Faults, RPCError

logger = logging.getLogger('superscaler.plugin')

class SuperscalerNamespaceRPCInterface:
    """Custom rpc namespace for handling the scaling process of supervisor process groups.

    Loaded by supervisord via the [rpcinterface:superscaler] config section.
    Manipulates the process group processes dict directly to add or remove
    individual subprocess instances without restarting existing workers.
    """

    def __init__(self, supervisord):
        self.supervisord = supervisord

    def _update(self, text):
        """Validate supervisor is in running state before processing."""
        self.update_text = text
        if (isinstance(self.supervisord.options.mood, int) and
                self.supervisord.options.mood < SupervisorStates.RUNNING):
            raise RPCError(Faults.SHUTDOWN_STATE)

    # Public rpc methods

    def getGroupInfo(self, program_name):
        """Return process count and per process state for a group.

        @param  string program_name  Name of the process group
        @return dict               Count and list of process info dicts
        """
        self._update('getGroupInfo')

        group = self.supervisord.process_groups.get(program_name)
        if group is None:
            raise RPCError(Faults.BAD_NAME,
                           'group %r not found' % program_name)

        processes = []
        for name in sorted(group.processes):
            proc = group.processes[name]
            state = proc.get_state()
            processes.append({
                'name': name,
                'pid': proc.pid,
                'state': state,
                'statename': getProcessStateDescription(state),
            })

        return {
            'count': len(processes),
            'processes': processes,
        }

    def scaleUp(self, program_name, count):
        """Add new processes to an existing group without touching running workers.

        Updates numprocs in the config file on disk, re reads the config
        through supervisor own parser to handle template expansion, then
        injects the new process config objects into the live process group.

        @param  string program_name  Name of the process group
        @param  int    count       Number of processes to add
        @return list               Names of added processes
        """
        self._update('scaleUp')

        group = self.supervisord.process_groups.get(program_name)
        if group is None:
            raise RPCError(Faults.BAD_NAME,
                           'group %r not found' % program_name)

        current_count = len(group.processes)
        new_numprocs = current_count + count

        # Persist new numprocs to config file on disk
        self._update_numprocs_in_config(program_name, new_numprocs)

        # Re read config so supervisor expands process_num templates
        self.supervisord.options.process_config(do_usage=False)

        # Find the updated process group config
        group_config = None
        for gc in self.supervisord.options.process_group_configs:
            if gc.name == program_name:
                group_config = gc
                break

        if group_config is None:
            raise RPCError(Faults.BAD_NAME,
                           'group config %r not found after re-read'
                           % program_name)

        # Inject new process config objects into the live group
        current_names = set(group.processes.keys())
        added = []

        for pconfig in group_config.process_configs:
            if pconfig.name not in current_names:
                pconfig.create_autochildlogs()
                process = pconfig.make_process(group)
                group.processes[pconfig.name] = process
                group.config.process_configs.append(pconfig)
                added.append(pconfig.name)

        logger.info('scaleUp %s +%d: %s (total=%d)',
                     program_name, count, added, len(group.processes))

        # Supervisor main loop transition will auto spawn these processes
        return added

    def scaleDown(self, program_name, count):
        """Send stop signal to processes, highest process number first.

        Does not remove processes from the group. The daemon must call
        confirmScaleDown after verifying processes have stopped.

        @param  string program_name  Name of the process group
        @param  int    count       Number of processes to stop
        @return list               Names of processes being stopped
        """
        self._update('scaleDown')

        group = self.supervisord.process_groups.get(program_name)
        if group is None:
            raise RPCError(Faults.BAD_NAME,
                           'group %r not found' % program_name)

        # Sort descending by name so highest process num stops first
        sorted_procs = sorted(group.processes.values(),
                              key=lambda p: p.config.name,
                              reverse=True)

        stopping = []
        for proc in sorted_procs[:count]:
            state = proc.get_state()
            if state in RUNNING_STATES:
                proc.stop()
                stopping.append(proc.config.name)
            elif state == ProcessStates.BACKOFF:
                proc.give_up()
                stopping.append(proc.config.name)
            elif state in STOPPED_STATES:
                # Already stopped, can be confirmed immediately
                stopping.append(proc.config.name)

        logger.info('scaleDown %s -%d: %s', program_name, count, stopping)
        return stopping

    def confirmScaleDown(self, program_name, process_names):
        """Remove stopped processes from the group and persist config.

        Validates all named processes are stopped, updates the config file
        on disk first, then removes processes from the in memory state.
        This ordering prevents state divergence if the config write fails.

        @param  string program_name      Name of the process group
        @param  list   process_names   Names of processes to remove
        @return bool                   True on success
        """
        self._update('confirmScaleDown')

        group = self.supervisord.process_groups.get(program_name)
        if group is None:
            raise RPCError(Faults.BAD_NAME,
                           'group %r not found' % program_name)

        # Validate all processes exist and are stopped before any mutation
        for name in process_names:
            proc = group.processes.get(name)
            if proc is None:
                raise RPCError(Faults.BAD_NAME,
                               'process %r not in group %r'
                               % (name, program_name))

            state = proc.get_state()
            if state not in STOPPED_STATES:
                raise RPCError(Faults.STILL_RUNNING,
                               'process %r is %s, cannot remove'
                               % (name,
                                  getProcessStateDescription(state)))

        # Calculate new count before any mutation
        new_numprocs = len(group.processes) - len(process_names)

        # Update config on disk first to prevent state divergence
        self._update_numprocs_in_config(program_name, new_numprocs)

        # Re read config to sync supervisor internal state
        self.supervisord.options.process_config(do_usage=False)

        # Now remove processes from in memory state
        names_set = set(process_names)
        for name in process_names:
            del group.processes[name]
        group.config.process_configs = [
            pc for pc in group.config.process_configs
            if pc.name not in names_set
        ]

        logger.info('confirmScaleDown %s: removed %s (total=%d)',
                     program_name, process_names, new_numprocs)
        return True

    # Internal helpers (not exposed via xml rpc due to underscore prefix)

    def _find_config_files(self):
        """Return list of all supervisor config files including includes.

        Parses the main supervisord config to find [include] file patterns
        and expands them with glob. Returns the main config plus all
        matched include files.
        """
        main = self.supervisord.options.configfile
        files = [main]

        try:
            parser = configparser.ConfigParser()
            parser.read(main)
            if (parser.has_section('include')
                    and parser.has_option('include', 'files')):
                include_glob = parser.get('include', 'files')
                base_dir = os.path.dirname(os.path.abspath(main))
                for pattern in include_glob.split():
                    full_pattern = os.path.join(base_dir, pattern)
                    files.extend(sorted(glob.glob(full_pattern)))
        except Exception:
            logger.warning('Failed to parse include files from %s', main,
                           exc_info=True)

        return files

    def _update_numprocs_in_config(self, program_name, new_numprocs):
        """Update numprocs for a program section in supervisor config.

        Reads the config file line by line to find the target program
        section and update the numprocs value. This approach preserves
        comments and formatting, and correctly handles square brackets
        that may appear in values or comments within the section.
        """
        section_header = '[program:%s]' % program_name

        for filepath in self._find_config_files():
            try:
                with open(filepath, 'r') as f:
                    lines = f.readlines()
            except OSError:
                continue

            in_section = False
            found = False

            for i, line in enumerate(lines):
                stripped = line.strip()

                # Check for section header match
                if stripped == section_header:
                    in_section = True
                    continue

                # If we are inside the target section and hit a new section
                # header, stop searching this file
                if in_section and stripped.startswith('['):
                    break

                # Look for numprocs key within the target section
                if in_section and stripped.startswith('numprocs'):
                    parts = stripped.split('=', 1)
                    if len(parts) == 2 and parts[0].strip() == 'numprocs':
                        # Preserve the original indentation and spacing style
                        prefix = line[:len(line) - len(line.lstrip())]
                        if '= ' in line:
                            eq_part = '= '
                        elif ' =' in line:
                            eq_part = ' = '
                        else:
                            eq_part = '='
                        lines[i] = '%snumprocs%s%d\n' % (
                            prefix, eq_part, new_numprocs)
                        found = True
                        break

            if found:
                with open(filepath, 'w') as f:
                    f.writelines(lines)
                logger.debug('Updated numprocs=%d for %s in %s',
                             new_numprocs, program_name, filepath)
                return

        logger.warning('Could not find [program:%s] in any config file',
                       program_name)