import time

class CooldownManager:
    """Track cooldown periods for scale up and scale down actions.

    One instance per target. Prevents rapid scaling oscillations by
    enforcing minimum intervals between consecutive scale actions.
    """

    def __init__(self, cooldown_up, cooldown_down):
        self.cooldown_up = cooldown_up
        self.cooldown_down = cooldown_down
        self._last_scale_up = 0.0
        self._last_scale_down = 0.0

    def can_scale_up(self):
        """Return true if enough time has passed since the last scale up."""
        return time.monotonic() - self._last_scale_up >= self.cooldown_up

    def can_scale_down(self):
        """Return true if enough time has passed since the last scale down."""
        return time.monotonic() - self._last_scale_down >= self.cooldown_down

    def mark_scale_up(self):
        """Record that a scale up action just occurred."""
        self._last_scale_up = time.monotonic()

    def mark_scale_down(self):
        """Record that a scale down action just occurred."""
        self._last_scale_down = time.monotonic()