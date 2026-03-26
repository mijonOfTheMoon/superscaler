import abc
import logging

logger = logging.getLogger('superscaler')


class NodeClient(abc.ABC):
    """Abstract base class for node backend clients.

    Subclass this to add support for a new process manager backend.
    Each subclass must implement all abstract methods. Register the
    subclass in NODE_CLIENT_BACKENDS to make it available via the
    factory function.
    """

    @abc.abstractmethod
    def get_group_info(self, program_name):
        """Return group info dict: {'count': int, 'processes': [...]}

        @param  string program_name  Supervisor program/group name
        @return dict                 Group info from the node backend
        """

    @abc.abstractmethod
    def scale_up(self, program_name, count):
        """Add processes to a group. Returns list of added names.

        @param  string program_name  Supervisor program/group name
        @param  int    count         Number of processes to add
        @return list                 Names of added processes
        """

    @abc.abstractmethod
    def scale_down(self, program_name, count):
        """Stop processes in a group. Returns list of names being stopped.

        @param  string program_name  Supervisor program/group name
        @param  int    count         Number of processes to stop
        @return list                 Names of processes being stopped
        """

    @abc.abstractmethod
    def confirm_scale_down(self, program_name, process_names):
        """Remove stopped processes from group. Returns true on success.

        @param  string program_name   Supervisor program/group name
        @param  list   process_names  Names of processes to remove
        @return bool                  True if removal succeeded
        """

    @abc.abstractmethod
    def ping(self):
        """Return true if the node is reachable.

        @return bool  True if connection is healthy
        """


# Backend registry. To add a new backend, subclass NodeClient and
# add an entry here. Values are dotted paths for lazy import.
NODE_CLIENT_BACKENDS = {
    'supervisor': 'superscaler.supervisor_client.SupervisorClient',
    # Future: 'pm2': 'superscaler.pm2_node_client.PM2NodeClient',
}


def create_node_client(node_config):
    """Factory function to create a node client from NodeConfig.

    Lazily imports the backend class to avoid circular imports.
    Currently only the 'supervisor' backend is supported.

    @param  NodeConfig node_config  Node configuration object
    @return NodeClient              Configured client instance
    """
    backend_path = NODE_CLIENT_BACKENDS.get(node_config.type)
    if backend_path is None:
        raise ValueError(
            'Unknown node type %r. Supported types: %s'
            % (node_config.type,
               ', '.join(sorted(NODE_CLIENT_BACKENDS.keys()))))

    # Lazy import to avoid circular dependencies
    if node_config.type == 'supervisor':
        from superscaler.supervisor_client import SupervisorClient
        return SupervisorClient(
            node_config.url,
            node_config.username or None,
            node_config.password or None,
        )

    raise ValueError('No loader implemented for node type %r'
                     % node_config.type)
