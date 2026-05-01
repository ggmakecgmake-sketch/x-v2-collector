"""
Deduplicator — keeps last N tweet IDs per account in memory.
"""
from collections import deque
from typing import Dict, Deque
import structlog

logger = structlog.get_logger("deduplicator")


class Deduplicator:
    """In-memory deduplication of tweet IDs per account.

    Trade-off: cache is lost on restart. Acceptable because X timelines
    never re-emit old tweets.
    """

    def __init__(self, max_per_account: int = 200):
        self.max_per_account = max_per_account
        self._seen: Dict[str, Deque[str]] = {}

    def is_new(self, account: str, tweet_id: str) -> bool:
        """Return True if this tweet_id has not been seen for this account."""
        cache = self._seen.setdefault(account, deque(maxlen=self.max_per_account))
        if tweet_id in cache:
            logger.debug("deduplicate_hit", account=account, tweet_id=tweet_id)
            return False
        return True

    def add(self, account: str, tweet_id: str):
        """Register a tweet_id as seen for this account."""
        cache = self._seen.setdefault(account, deque(maxlen=self.max_per_account))
        cache.append(tweet_id)
        logger.debug("deduplicate_added", account=account, tweet_id=tweet_id, cache_size=len(cache))

    def stats(self) -> Dict[str, int]:
        """Return cache sizes per account."""
        return {account: len(ids) for account, ids in self._seen.items()}
