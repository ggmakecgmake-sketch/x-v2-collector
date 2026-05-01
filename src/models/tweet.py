"""
Data models for the X-v2 Collector.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any


@dataclass
class Tweet:
    """Unified tweet model — emitted to Redis with the same schema as v1 collector."""

    tweet_id: str
    author_username: str
    author_id: Optional[str] = None
    text: str = ""
    created_at: Optional[str] = None
    engagement: Dict[str, Any] = field(default_factory=dict)
    raw_data: Dict[str, Any] = field(default_factory=dict)
    ingested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_engine: str = "twikit"  # "twikit" or "playwright"

    def to_redis_payload(self) -> Dict[str, Any]:
        """Serialize to the same flat-map format the v1 collector uses.

        Redis Streams fields must be strings, so we JSON-dump nested objects.
        """
        import json

        return {
            "tweet_id": self.tweet_id,
            "author_id": self.author_id or "",
            "author_username": self.author_username,
            "text": self.text,
            "created_at": self.created_at or "",
            "engagement": json.dumps(self.engagement) if self.engagement else "{}",
            "raw_data": json.dumps(self.raw_data) if self.raw_data else "{}",
            "ingested_at": self.ingested_at,
            "source_engine": self.source_engine,
        }

    @classmethod
    def from_twikit(cls, tweet: Any, username: str) -> "Tweet":
        """Build a Tweet from a twikit Tweet object."""
        # Twikit tweet attributes vary slightly by version; we defensively extract.
        tweet_id = getattr(tweet, "id", None) or getattr(tweet, "text", "")[:20] + "_"
        text = getattr(tweet, "text", "")
        created = getattr(tweet, "created_at", None)
        author_id = getattr(tweet, "author_id", None)

        # Public metrics: likes, retweets, replies, quotes
        metrics = {}
        for attr in ("favorite_count", "retweet_count", "reply_count", "quote_count"):
            val = getattr(tweet, attr, None)
            if val is not None:
                metrics[attr] = val

        raw = {"source": "twikit"}
        # Attempt to capture full raw JSON if available
        if hasattr(tweet, "_data"):
            raw = tweet._data  # type: ignore

        return cls(
            tweet_id=str(tweet_id),
            author_username=username,
            author_id=str(author_id) if author_id else None,
            text=text or "",
            created_at=str(created) if created else None,
            engagement=metrics,
            raw_data=raw,
            source_engine="twikit",
        )

    @classmethod
    def from_playwright_dict(cls, data: Dict[str, Any], username: str) -> "Tweet":
        """Build a Tweet from a Playwright-extracted dict."""
        return cls(
            tweet_id=str(data.get("id", "")),
            author_username=username,
            author_id=str(data.get("author_id", "")),
            text=data.get("text", ""),
            created_at=data.get("created_at"),
            engagement=data.get("public_metrics", {}),
            raw_data=data.get("raw", {}),
            source_engine="playwright",
        )
