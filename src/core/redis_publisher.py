"""
Redis Publisher — pushes tweets into the same stream the v1 collector uses.
"""
import json
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis
import structlog

from src.config import settings
from src.models.tweet import Tweet

logger = structlog.get_logger("redis_publisher")


class RedisPublisher:
    """Publishes tweets to a Redis Stream with the same schema as v1 collector."""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or settings.resolved_redis_url
        self._client: Optional[redis.Redis] = None

    async def connect(self):
        logger.info("connecting_to_redis", url=self.redis_url)
        self._client = redis.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        # Quick sanity check
        await self._client.ping()
        logger.info("redis_connected")

    async def disconnect(self):
        if self._client:
            await self._client.close()
            self._client = None
            logger.info("redis_disconnected")

    async def publish(self, tweet: Tweet) -> Optional[str]:
        """Publish a tweet to the Redis stream. Returns the message ID."""
        if not self._client:
            raise RuntimeError("Publisher not connected")

        payload = tweet.to_redis_payload()
        # All values MUST be str for Redis Streams
        payload = {k: str(v) for k, v in payload.items()}

        msg_id = await self._client.xadd(
            name=settings.stream_key,
            fields=payload,
            maxlen=settings.stream_maxlen,
            approximate=True,
        )
        logger.debug(
            "tweet_published",
            stream=settings.stream_key,
            tweet_id=tweet.tweet_id,
            msg_id=msg_id,
        )
        return msg_id

    async def health_check(self) -> bool:
        try:
            if self._client:
                return await self._client.ping()
        except Exception:
            pass
        return False
