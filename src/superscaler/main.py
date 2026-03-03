import signal
import sys
import time
import logging

from superscaler.config import load_config
from superscaler.queue_monitor import create_queue_monitor
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

    Loads configuration, creates queue monitors for each configured backend,
    performs health checks against all queue backends and supervisor,
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

    # Build queue monitors from config
    queue_monitors = {}
    for qname, qconfig in config.queues.items():
        try:
            monitor = create_queue_monitor(qconfig.type, qconfig.params)
            queue_monitors[qname] = monitor
        except Exception as exc:
            logger.error('Failed to create queue monitor %r: %s', qname, exc)
            sys.exit(1)

    # Health check all queue backends
    for qname, monitor in queue_monitors.items():
        if not monitor.ping():
            logger.error('Cannot connect to queue backend %r', qname)
            sys.exit(1)
        logger.info('Successfully connected to queue backend %r', qname)

    # Build unix socket url for supervisor xml rpc transport
    xmlrpc_url = config.unix_socket_path

    sv_client = SupervisorClient(
        xmlrpc_url, config.sv_username or None,
        config.sv_password or None)
    if not sv_client.ping():
        logger.error('Cannot connect to supervisor at %s',
                     config.unix_socket_path)
        sys.exit(1)

    # Create engine
    engine = ScalerEngine(config, queue_monitors, sv_client)

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

                # Rebuild queue monitors for new/changed backends
                new_monitors = {}
                for qname, qconfig in new_config.queues.items():
                    if qname in queue_monitors:
                        # Reuse existing monitor for unchanged backends
                        new_monitors[qname] = queue_monitors[qname]
                    else:
                        new_monitors[qname] = create_queue_monitor(
                            qconfig.type, qconfig.params)

                queue_monitors = new_monitors
                engine.reload_config(new_config, queue_monitors)
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