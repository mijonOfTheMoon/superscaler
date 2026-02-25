import logging

import redis

logger = logging.getLogger('superscaler')


class RedisMonitor:
    """Monitor redis list lengths to determine queue depth.

    Provides a thin wrapper around the redis client focused on the LLEN
    command used for queue depth monitoring and a health check ping.
    """

    def __init__(self, host, port, password, db):
        connect_kwargs = {
            'host': host,
            'port': port,
            'db': db,
            'socket_connect_timeout': 5,
            'socket_timeout': 5,
        }
        if password:
            connect_kwargs['password'] = password
        self.client = redis.Redis(**connect_kwargs)

    def get_queue_length(self, queue_key):
        """Return the list length for the given key.

        Returns 0 if the key does not exist. Raises on connection errors
        so the caller can decide how to handle unavailability.
        """
        return self.client.llen(queue_key)

    def ping(self):
        """Return true if redis is reachable."""
        try:
            return self.client.ping()
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            logger.error('Redis ping failed: %s', exc)
            return False