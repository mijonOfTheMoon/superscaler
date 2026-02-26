import configparser
import dataclasses
import logging
from typing import List

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
    'cooldown_up': (int, 5),
    'cooldown_down': (int, 10),
    'pending_timeout': (int, 10),
}


@dataclasses.dataclass
class TargetConfig:
    """Configuration for a single scaling target.

    Each target maps a redis queue key to a supervisor process group and
    defines all scaling parameters that control how the group is managed.
    """

    name: str
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
    pending_timeout: int


@dataclasses.dataclass
class SuperscalerConfig:
    """Top level configuration holding redis, supervisor, and target settings."""

    config_path: str
    redis_host: str
    redis_port: int
    redis_password: str
    redis_db: int
    unix_socket_path: str
    sv_username: str
    sv_password: str
    targets: List[TargetConfig]


def load_config(path):
    """Parse the superscaler configuration file and return a config object.

    Expected sections: [redis], [supervisor], [target:*].
    Every target must specify all scaling parameters explicitly.
    """
    parser = configparser.ConfigParser()
    read_ok = parser.read(path)
    if not read_ok:
        raise ValueError('Cannot read config file: %s' % path)

    # Redis section
    if not parser.has_section('redis'):
        raise ValueError('Missing required section [redis]')
    redis_host = parser.get('redis', 'host', fallback='127.0.0.1')
    redis_port = parser.getint('redis', 'port', fallback=6379)
    redis_password = parser.get('redis', 'password', fallback='')
    redis_db = parser.getint('redis', 'db', fallback=0)

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

    # Target sections
    targets = []
    target_sections = [s for s in parser.sections()
                       if s.startswith('target:')]

    for section in target_sections:
        target_name = section.split(':', 1)[1]

        queue_key = parser.get(section, 'queue_key', fallback=None)
        if not queue_key:
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
            queue_key=queue_key,
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
        if target.pending_timeout < 1:
            raise ValueError('[%s] pending_timeout must be >= 1' % section)

        targets.append(target)

    config = SuperscalerConfig(
        config_path=path,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_password=redis_password,
        redis_db=redis_db,
        unix_socket_path=unix_socket_path,
        sv_username=sv_username,
        sv_password=sv_password,
        targets=targets,
    )

    logger.info('Loaded config: %d target(s) from %s', len(targets), path)
    return config