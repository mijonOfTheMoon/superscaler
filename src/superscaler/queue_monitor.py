import abc
import logging

logger = logging.getLogger('superscaler')

class QueueMonitor(abc.ABC):
    """Abstract base class for queue backend monitors.

    Subclass this to add support for a new queue system. Each subclass
    must implement get_queue_length and ping. Register the subclass
    in QUEUE_BACKENDS to make it available via the factory function.
    """

    @abc.abstractmethod
    def get_queue_length(self, queue_key):
        """Return the number of pending messages in the given queue.

        Returns 0 if the queue does not exist. Raises on connection errors
        so the caller can decide how to handle unavailability.

        @param  string queue_key  Queue identifier (interpretation varies by backend)
        @return int               Number of pending messages
        """

    @abc.abstractmethod
    def ping(self):
        """Return true if the queue backend is reachable.

        @return bool  True if connection is healthy
        """

class RedisMonitor(QueueMonitor):
    """Queue monitor for Redis lists.

    Uses the llen command to determine queue depth. The queue_key
    parameter maps directly to a Redis list key.
    """

    def __init__(self, host='127.0.0.1', port=6379, password='', db=0,
                 **kwargs):
        import redis
        connect_kwargs = {
            'host': host,
            'port': int(port),
            'db': int(db),
            'socket_connect_timeout': 5,
            'socket_timeout': 5,
        }
        if password:
            connect_kwargs['password'] = password
        self.client = redis.Redis(**connect_kwargs)

    def get_queue_length(self, queue_key):
        """Return the list length for the given Redis key.

        @param  string queue_key  Redis list key
        @return int               Number of items in the list
        """
        return self.client.llen(queue_key)

    def ping(self):
        """Return true if Redis is reachable."""
        import redis
        try:
            return self.client.ping()
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            logger.error('Redis ping failed: %s', exc)
            return False

class RabbitMQMonitor(QueueMonitor):
    """Queue monitor for RabbitMQ queues.

    Uses pika with a passive queue_declare to read the message count.
    Maintains a persistent connection and channel, reconnecting on failure.
    The queue_key parameter maps to a RabbitMQ queue name.
    """

    def __init__(self, host='127.0.0.1', port=5672, username='guest',
                 password='guest', vhost='/', **kwargs):
        import pika
        self._credentials = pika.PlainCredentials(username, password)
        self._params = pika.ConnectionParameters(
            host=host,
            port=int(port),
            virtual_host=vhost,
            credentials=self._credentials,
            connection_attempts=3,
            retry_delay=1,
            socket_timeout=5,
        )
        self._connection = None
        self._channel = None

    def _ensure_channel(self):
        """Return a live channel, reconnecting if necessary."""
        import pika
        if (self._connection is not None
                and self._connection.is_open
                and self._channel is not None
                and self._channel.is_open):
            return self._channel

        # Close stale connection if any
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass

        self._connection = pika.BlockingConnection(self._params)
        self._channel = self._connection.channel()
        return self._channel

    def get_queue_length(self, queue_key):
        """Return the message count for the given RabbitMQ queue.

        @param  string queue_key  RabbitMQ queue name
        @return int               Number of pending messages
        """
        channel = self._ensure_channel()
        result = channel.queue_declare(queue=queue_key, passive=True)
        return result.method.message_count

    def ping(self):
        """Return true if RabbitMQ is reachable."""
        try:
            self._ensure_channel()
            return True
        except Exception as exc:
            logger.error('RabbitMQ ping failed: %s', exc)
            return False

# Backend registry. To add a new backend, subclass QueueMonitor and
# add an entry here.
QUEUE_BACKENDS = {
    'redis': RedisMonitor,
    'rabbitmq': RabbitMQMonitor,
}

def create_queue_monitor(queue_type, params):
    """Factory function to create a queue monitor from config.

    @param  string queue_type  Backend type key from QUEUE_BACKENDS
    @param  dict   params      Backend specific connection parameters
    @return QueueMonitor       Configured monitor instance
    """
    backend_class = QUEUE_BACKENDS.get(queue_type)
    if backend_class is None:
        raise ValueError(
            'Unknown queue type %r. Supported types: %s'
            % (queue_type, ', '.join(sorted(QUEUE_BACKENDS.keys()))))
    return backend_class(**params)