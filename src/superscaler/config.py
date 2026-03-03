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


@dataclasses.dataclass
class QueueConfig:
    """Configuration for a named queue backend.

    Each queue config defines a backend type and its connection parameters.
    Multiple targets can reference the same queue config by name.
    """

    name: str
    type: str
    params: dict


@dataclasses.dataclass
class TargetConfig:
    """Configuration for a single scaling target.

    Each target maps a queue name in a specific backend to a supervisor
    process group and defines all scaling parameters that control how
    the group is managed.
    """

    name: str
    queue: str
    queue_key: str
    program_name: str
    poll_interval: int
    tasks_per_worker: int
    min_workers: int
    max_workers: int
    scale_up_step: int
    scale_down_step: int
    cooldown_up: int
    cooldown_down: int


@dataclasses.dataclass
class SuperscalerConfig:
    """Top level configuration holding queue backends, supervisor, and target settings."""

    config_path: str
    unix_socket_path: str
    sv_username: str
    sv_password: str
    queues: Dict[str, QueueConfig]
    targets: List[TargetConfig]


def load_config(path):
    """Parse the superscaler configuration file and return a config object.

    Expected sections: [queue:*], [supervisor], [target:*].
    Queue backends are defined in named sections and referenced by targets.
    """
    parser = configparser.ConfigParser()
    read_ok = parser.read(path)
    if not read_ok:
        raise ValueError('Cannot read config file: %s' % path)

    # Supervisor section
    if not parser.has_section('supervisor'):
        raise ValueError('Missing required section [supervisor]')
    unix_socket_path = parser.get('supervisor', 'unix_socket_path',
                                  fallback=None)
    if not unix_socket_path:
        raise ValueError(
            '[supervisor] missing required option: unix_socket_path')
    sv_username = parser.get('supervisor', 'username', fallback='')
    sv_password = parser.get('supervisor', 'password', fallback='')

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
            name=queue_name,
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
            queue=queue_ref,
            queue_key=queue_key_val,
            program_name=program_name,
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

        targets.append(target)

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