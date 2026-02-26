import signal
import sys
import time
import logging

from superscaler.config import load_config
from superscaler.redis_monitor import RedisMonitor
from superscaler.supervisor_client import SupervisorClient
from superscaler.scaler import ScalerEngine

logger = logging.getLogger('superscaler')

def setup_logging():
    """Configure logging to stderr for journald capture."""
    root = logging.getLogger('superscaler')
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    root.addHandler(handler)

def main():
    """Entry point for the superscaler daemon.

    Loads configuration, performs health checks against redis and supervisor,
    then enters the main loop that periodically evaluates all targets.
    Handles SIGTERM and SIGINT for graceful shutdown, and SIGHUP for live
    configuration reload without restarting the service.
    """
    if len(sys.argv) > 1 and sys.argv[1] in ('-v', '--version'):
        try:
            import importlib.metadata
            print("superscaler version", importlib.metadata.version("superscaler"))
        except Exception:
            print("superscaler version unknown")
        sys.exit(0)

    config_path = sys.argv[1] if len(sys.argv) > 1 \
        else '/etc/superscaler/superscaler.conf'

    setup_logging()

    # Load config
    try:
        config = load_config(config_path)
    except (ValueError, OSError) as exc:
        logger.error('Failed to load config: %s', exc)
        sys.exit(1)

    logger.info('Loaded %d target(s)', len(config.targets))

    # Build unix socket url for supervisor xml rpc transport
    xmlrpc_url = config.unix_socket_path

    # Health checks
    redis_mon = RedisMonitor(
        config.redis_host, config.redis_port,
        config.redis_password, config.redis_db)
    if not redis_mon.ping():
        logger.error('Cannot connect to redis at %s:%d',
                     config.redis_host, config.redis_port)
        sys.exit(1)

    sv_client = SupervisorClient(
        xmlrpc_url, config.sv_username or None,
        config.sv_password or None)
    if not sv_client.ping():
        logger.error('Cannot connect to supervisor at %s',
                     config.unix_socket_path)
        sys.exit(1)

    # Create engine
    engine = ScalerEngine(config, redis_mon, sv_client)

    # Signal handling
    reload_requested = False

    def handle_sigterm(signum, frame):
        logger.info('Signal %d received, shutting down', signum)
        engine.running = False

    def handle_sighup(signum, frame):
        nonlocal reload_requested
        logger.info('Sighup received, scheduling config reload')
        reload_requested = True

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGHUP, handle_sighup)

    # Main loop
    logger.info('Superscaler started')
    while engine.running:
        # Handle config reload
        if reload_requested:
            reload_requested = False
            try:
                new_config = load_config(config_path)
                engine.reload_config(new_config)
                logger.info('Config reloaded successfully')
            except Exception:
                logger.exception('Config reload failed, keeping old config')

        # Process all targets
        engine.tick()

        # Sleep in small increments so signals are handled promptly.
        # Uses monotonic clock to avoid issues with ntp adjustments.
        min_interval = min(
            (t.poll_interval for t in engine.config.targets), default=2)
        deadline = time.monotonic() + min_interval
        while engine.running and not reload_requested \
                and time.monotonic() < deadline:
            time.sleep(0.5)

    logger.info('Superscaler stopped')