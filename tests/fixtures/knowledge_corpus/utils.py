"""Sample Python source used as knowledge-base test fixture."""


def calculate_checksum(data: bytes) -> str:
    """Compute a hex checksum for the given bytes."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


class RateLimiter:
    """A simple token-bucket rate limiter for throttling requests."""

    def __init__(self, capacity: int, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity

    def allow(self) -> bool:
        """Return True if a request is allowed under the current rate."""
        if self.tokens > 0:
            self.tokens -= 1
            return True
        return False
