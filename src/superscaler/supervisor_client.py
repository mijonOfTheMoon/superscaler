import logging
import xmlrpc.client
from urllib.parse import urlparse, urlunparse

from supervisor.xmlrpc import SupervisorTransport

from superscaler.node_client import NodeClient

logger = logging.getLogger('superscaler')

class SupervisorClient(NodeClient):
    """XML rpc client for supervisor using unix socket or HTTP transport.

    Wraps both the standard supervisor namespace and the custom superscaler
    namespace provided by the superscaler plugin. Supports two transport
    modes based on URL scheme:

    - ``unix://`` — Uses ``SupervisorTransport`` for local unix socket.
    - ``http://`` — Uses standard ``xmlrpc.client.ServerProxy`` with
      optional HTTP Basic Auth for remote nodes.
    """

    def __init__(self, url, username=None, password=None):
        self.url = url
        if url.startswith('unix://'):
            transport = SupervisorTransport(
                username or None, password or None, url)
            self.server = xmlrpc.client.ServerProxy(
                'http://127.0.0.1', transport=transport)
        elif url.startswith('http://'):
            if username and password:
                parsed = urlparse(url)
                netloc = '%s:%s@%s' % (username, password, parsed.netloc)
                auth_url = urlunparse(parsed._replace(netloc=netloc))
                self.server = xmlrpc.client.ServerProxy(auth_url)
            else:
                self.server = xmlrpc.client.ServerProxy(url)
        else:
            raise ValueError(
                'Unsupported URL scheme for SupervisorClient: %r. '
                'Expected unix:// or http://' % url)

    # Supervisor namespace

    def get_state(self):
        """Get supervisor daemon state."""
        return self.server.supervisor.getState()

    # Superscaler namespace (custom plugin)

    def get_group_info(self, program_name):
        """Return group info dict from the superscaler rpc plugin."""
        return self.server.superscaler.getGroupInfo(program_name)

    def scale_up(self, program_name, count):
        """Add processes to a group. Returns list of added names."""
        return self.server.superscaler.scaleUp(program_name, count)

    def scale_down(self, program_name, count):
        """Stop processes in a group. Returns list of names being stopped."""
        return self.server.superscaler.scaleDown(program_name, count)

    def confirm_scale_down(self, program_name, process_names):
        """Remove stopped processes from group. Returns true on success."""
        return self.server.superscaler.confirmScaleDown(
            program_name, process_names)

    # Health check

    def ping(self):
        """Return true if supervisor is reachable and in running state."""
        try:
            state = self.get_state()
            return state.get('statecode', -1) == 1
        except Exception as exc:
            logger.error('Supervisor ping failed: %s', exc)
            return False