import configparser
import dataclasses
import logging
from typing import List, Dict

logger = logging.getLogger('superscaler')

# Required parameters for each target section. Every target must specify all
# of these explicitly. There are no default values or fallback mechanisms.
REQUIRED_TARGET_PARAMS = {
    'tasks_per_worker': int,
    'min_workers': int,
    'max_workers': int,
}

# Optional parameters and their default values
OPTIONAL_TARGET_PARAMS = {
    'poll_interval': (int, 10),
    'scale_up_step': (int, 1),
    'scale_down_step': (int, 1),
    'cooldown_up': (int, 0),
    'cooldown_down': (int, 0),
}

# Reserved keys in queue sections that are not backend params
QUEUE_RESERVED_KEYS = {'type'}

# Valid values for the target type field
VALID_TARGET_TYPES = ('supervisor', 'pm2')


@dataclasses.dataclass
class QueueConfig:
    """Configuration for a named queue backend.

    Each queue config defines a backend type and its connection parameters.
    Multiple targets can reference the same queue config by name.
    The dict key in SuperscalerConfig.queues serves as the queue name.
    """

    type: str
    params: dict


@dataclasses.dataclass
class TargetConfig:
    """Configuration for a single scaling target.

    Each target maps a queue name in a specific backend to a supervisor
    or PM2 process group and defines all scaling parameters that control
    how the group is managed.
    """

    name: str
    type: str = 'supervisor'
    queue: str = ''
    queue_key: str = ''
    program_name: str = ''
    poll_interval: int = 0
    tasks_per_worker: int = 0
    min_workers: int = 0
    max_workers: int = 0
    scale_up_step: int = 0
    scale_down_step: int = 0
    cooldown_up: int = 0
    cooldown_down: int = 0
    pm2_path: str = ''
    pm2_home: str = ''
    run_as_user: str = ''


@dataclasses.dataclass
class SuperscalerConfig:
    """Top level configuration holding queue backends, supervisor, and target settings."""

    config_path: str
    unix_socket_path: str = ''
    sv_username: str = ''
    sv_password: str = ''
    queues: Dict[str, QueueConfig] = dataclasses.field(default_factory=dict)
    targets: List[TargetConfig] = dataclasses.field(default_factory=list)


def load_config(path):
    """Parse the superscaler configuration file and return a config object.

    Expected sections: [queue:*], [supervisor], [target:*].
    Queue backends are defined in named sections and referenced by targets.
    """
    parser = configparser.ConfigParser()
    read_ok = parser.read(path)
    if not read_ok:
        raise ValueError('Cannot read config file: %s' % path)

    # Queue sections
    queues = {}
    queue_sections = [s for s in parser.sections()
                      if s.startswith('queue:')]

    for section in queue_sections:
        queue_name = section.split(':', 1)[1]

        queue_type = parser.get(section, 'type', fallback=None)
        if not queue_type:
            raise ValueError('[%s] missing required option: type' % section)

        # Collect all non-reserved keys as backend params
        params = {}
        for key, value in parser.items(section):
            if key not in QUEUE_RESERVED_KEYS:
                params[key] = value

        queues[queue_name] = QueueConfig(
            type=queue_type,
            params=params,
        )

    if not queues:
        raise ValueError('No [queue:*] sections found in config')

    # Target sections
    targets = []
    target_sections = [s for s in parser.sections()
                       if s.startswith('target:')]

    for section in target_sections:
        target_name = section.split(':', 1)[1]

        queue_ref = parser.get(section, 'queue', fallback=None)
        if not queue_ref:
            raise ValueError('[%s] missing required option: queue' % section)
        if queue_ref not in queues:
            raise ValueError(
                '[%s] queue %r does not match any [queue:*] section'
                % (section, queue_ref))

        queue_key_val = parser.get(section, 'queue_key', fallback=None)
        if not queue_key_val:
            raise ValueError('[%s] missing required option: queue_key'
                             % section)

        program_name = parser.get(section, 'program_name', fallback=None)
        if not program_name:
            raise ValueError('[%s] missing required option: program_name'
                             % section)

        # Target type — defaults to 'supervisor'
        target_type = parser.get(section, 'type', fallback='supervisor')
        if target_type not in VALID_TARGET_TYPES:
            raise ValueError(
                '[%s] invalid type %r, supported types: %s'
                % (section, target_type,
                   ', '.join(VALID_TARGET_TYPES)))

        # PM2 execution environment fields — optional, default to ''
        pm2_path = parser.get(section, 'pm2_path', fallback='')
        pm2_home = parser.get(section, 'pm2_home', fallback='')
        run_as_user = parser.get(section, 'run_as_user', fallback='')

        # All scaling parameters are mandatory per target
        params = {}
        for param_name, param_type in REQUIRED_TARGET_PARAMS.items():
            raw = parser.get(section, param_name, fallback=None)
            if raw is None:
                raise ValueError(
                    '[%s] missing required option: %s'
                    % (section, param_name))
            params[param_name] = param_type(raw)

        # Optional scaling parameters
        for param_name, (param_type, default_val) in OPTIONAL_TARGET_PARAMS.items():
            raw = parser.get(section, param_name, fallback=str(default_val))
            params[param_name] = param_type(raw)

        target = TargetConfig(
            name=target_name,
            type=target_type,
            queue=queue_ref,
            queue_key=queue_key_val,
            program_name=program_name,
            pm2_path=pm2_path,
            pm2_home=pm2_home,
            run_as_user=run_as_user,
            **params,
        )

        # Validate constraints
        if target.min_workers < 0:
            raise ValueError('[%s] min_workers must be >= 0' % section)
        if target.max_workers < target.min_workers:
            raise ValueError('[%s] max_workers must be >= min_workers'
                             % section)
        if target.tasks_per_worker < 1:
            raise ValueError('[%s] tasks_per_worker must be >= 1' % section)
        if target.scale_up_step < 1:
            raise ValueError('[%s] scale_up_step must be >= 1' % section)
        if target.scale_down_step < 1:
            raise ValueError('[%s] scale_down_step must be >= 1' % section)
        if target.poll_interval < 1:
            raise ValueError('[%s] poll_interval must be >= 1' % section)

        # Warn if PM2 fields are set on a supervisor target
        if target.type == 'supervisor':
            pm2_fields = []
            if target.pm2_path:
                pm2_fields.append('pm2_path')
            if target.pm2_home:
                pm2_fields.append('pm2_home')
            if target.run_as_user:
                pm2_fields.append('run_as_user')
            if pm2_fields:
                logger.warning(
                    '[%s] %s ignored for supervisor target',
                    section, ', '.join(pm2_fields))

        targets.append(target)

    # Supervisor section — only required when supervisor targets exist
    has_supervisor_targets = any(t.type == 'supervisor' for t in targets)
    unix_socket_path = ''
    sv_username = ''
    sv_password = ''

    if has_supervisor_targets:
        if not parser.has_section('supervisor'):
            raise ValueError('Missing required section [supervisor]')
        unix_socket_path = parser.get('supervisor', 'unix_socket_path',
                                      fallback=None)
        if not unix_socket_path:
            raise ValueError(
                '[supervisor] missing required option: unix_socket_path')
        sv_username = parser.get('supervisor', 'username', fallback='')
        sv_password = parser.get('supervisor', 'password', fallback='')

    config = SuperscalerConfig(
        config_path=path,
        unix_socket_path=unix_socket_path,
        sv_username=sv_username,
        sv_password=sv_password,
        queues=queues,
        targets=targets,
    )

    logger.info('Loaded config: %d queue(s), %d target(s) from %s',
                len(queues), len(targets), path)
    return config