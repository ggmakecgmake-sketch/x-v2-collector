"""
Rate limiter with jitter — ensures we never hit X too fast.
"""
import asyncio
import random
import time
from typing import Dict
import structlog

logger = structlog.get_logger("rate_limiter")


class RateLimiter:
    """Per-account rate limiter with min/max interval and jitter."""

    def __init__(self, min_seconds: int = 45, max_seconds: int = 55):
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self._last_fetch: Dict[str, float] = {}

    def next_interval(self) -> float:
        """Return a randomized sleep duration in seconds."""
        return random.uniform(self.min_seconds, self.max_seconds)

    def can_fetch(self, account: str) -> bool:
        """Check if enough time has passed since last fetch for this account."""
        now = time.monotonic()
        last = self._last_fetch.get(account)
        if last is None:
            return True
        elapsed = now - last
        return elapsed >= self.min_seconds

    async def wait_for(self, account: str):
        """Block until the account is rate-limit safe."""
        now = time.monotonic()
        last = self._last_fetch.get(account, 0)
        elapsed = now - last
        if elapsed < self.min_seconds:
            sleep_needed = self.min_seconds - elapsed + random.uniform(1, 5)
            logger.debug(
                "rate_limit_wait",
                account=account,
                elapsed=elapsed,
                sleep=sleep_needed,
            )
            await asyncio.sleep(sleep_needed)

    def mark_fetched(self, account: str):
        """Record that we just fetched this account."""
        self._last_fetch[account] = time.monotonic()
        logger.debug("rate_limit_marked", account=account, timestamp=self._last_fetch[account])

    def wait_for_global(self):
        """Deprecated: kept for compatibility. Use per-account wait_for."""
        return self.next_interval()
