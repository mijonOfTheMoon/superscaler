import logging
import xmlrpc.client

from supervisor.xmlrpc import SupervisorTransport

logger = logging.getLogger('superscaler')

class SupervisorClient:
    """XML rpc client for supervisor using unix socket transport.

    Wraps both the standard supervisor namespace and the custom superscaler
    namespace provided by the superscaler plugin. Authentication is passed
    through to the transport layer for unix socket xml rpc calls.
    """

    def __init__(self, url, username=None, password=None):
        self.url = url
        transport = SupervisorTransport(
            username or None, password or None, url)
        self.server = xmlrpc.client.ServerProxy(
            'http://127.0.0.1', transport=transport)

    # Supervisor namespace

    def get_process_info(self, namespec):
        """Get info for a single process by namespec (group:name)."""
        return self.server.supervisor.getProcessInfo(namespec)

    def get_state(self):
        """Get supervisor daemon state."""
        return self.server.supervisor.getState()

    # Superscaler namespace (custom plugin)

    def get_group_info(self, group_name):
        """Return group info dict from the superscaler rpc plugin."""
        return self.server.superscaler.getGroupInfo(group_name)

    def scale_up(self, group_name, count):
        """Add processes to a group. Returns list of added names."""
        return self.server.superscaler.scaleUp(group_name, count)

    def scale_down(self, group_name, count):
        """Stop processes in a group. Returns list of names being stopped."""
        return self.server.superscaler.scaleDown(group_name, count)

    def confirm_scale_down(self, group_name, process_names):
        """Remove stopped processes from group. Returns true on success."""
        return self.server.superscaler.confirmScaleDown(
            group_name, process_names)

    # Health check

    def ping(self):
        """Return true if supervisor is reachable and in running state."""
        try:
            state = self.get_state()
            return state.get('statecode', -1) == 1
        except Exception as exc:
            logger.error('Supervisor ping failed: %s', exc)
            return False