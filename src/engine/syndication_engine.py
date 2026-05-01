"""
Syndication Engine — scrapes X timelines using the official embeddable
timeline endpoint (NO login required).

Uses: https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}

This returns ~100 most recent tweets as JSON embedded in HTML __NEXT_DATA__.

Pros:
- No authentication needed (works with just User-Agent)
- Returns real tweet data with engagement metrics
- Stable endpoint (used by Twitter's embedded timeline widgets)

Cons:
- Only ~100 tweets returned (no pagination)
- No filter by date; returns most recent only
- Cannot get full historical timeline
- Rate limited (use delays between requests)
"""
import json
import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import structlog

from src.config import settings
from src.models.tweet import Tweet
from src.core.rate_limiter import RateLimiter

logger = structlog.get_logger("syndication_engine")


class SyndicationEngine:
    """Scrapes X timelines via syndication endpoint without authentication."""

    BASE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"

    # Realistic browser headers
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "identity",
        "DNT": "1",
        "Connection": "keep-alive",
    }

    def __init__(self, rate_limiter: Optional[RateLimiter] = None):
        self._active = False
        self.rate_limiter = rate_limiter or RateLimiter(min_seconds=10, max_seconds=20)
        self._consecutive_failures = 0

    async def start(self):
        """No-op — this engine is stateless."""
        self._active = True
        logger.info("syndication_engine_started")

    async def stop(self):
        """No-op."""
        self._active = False
        logger.info("syndication_engine_stopped")

    # ------------------------------------------------------------------
    # Core fetch
    # ------------------------------------------------------------------

    @classmethod
    def _parse_timestamp(cls, s: str) -> Optional[str]:
        """Convert X/Twitter date string to ISO 8601."""
        # Format: Thu Apr 06 17:22:35 +0000 2023
        try:
            dt = datetime.strptime(s, "%a %b %d %H:%M:%S +0000 %Y")
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except (ValueError, TypeError):
            return None

    def _extract_tweets(self, html: str) -> List[Dict[str, Any]]:
        """Extract tweet objects from syndication response HTML."""
        match = re.search(
            r'\u003cscript id="__NEXT_DATA__" type="application/json"\u003e(.+?)\u003c/script\u003e',
            html,
            re.DOTALL,
        )
        if not match:
            raise ValueError("No __NEXT_DATA__ found in response")

        data = json.loads(match.group(1))
        entries = data["props"]["pageProps"]["timeline"]["entries"]
        raw_tweets = []
        for entry in entries:
            tweet_data = entry.get("content", {}).get("tweet", {})
            if not tweet_data:
                continue
            raw_tweets.append(tweet_data)
        return raw_tweets

    def _build_tweet(self, raw: Dict[str, Any], fallback_username: str = "") -> Tweet:
        """Build a Tweet model from raw syndication data."""
        user = raw.get("user", {})
        screen_name = user.get("screen_name", fallback_username)

        # Parse timestamp
        created_iso = self._parse_timestamp(raw.get("created_at", ""))

        # Engagement metrics
        engagement = {
            "favorite_count": raw.get("favorite_count", 0),
            "reply_count": raw.get("reply_count", 0),
            "retweet_count": raw.get("retweet_count", 0),
            "quote_count": raw.get("quote_count", 0),
        }

        # Conversation metadata
        conversation_id = raw.get("conversation_id_str")
        reply_to = raw.get("in_reply_to_screen_name")
        lang = raw.get("lang")
        is_reply = bool(reply_to)
        is_quote = raw.get("is_quote_status", False)

        return Tweet(
            tweet_id=str(raw.get("id_str", "")),
            author_username=screen_name,
            author_id=str(user.get("id_str", "")),
            text=(raw.get("text") or raw.get("full_text", "")),
            created_at=created_iso,
            engagement=engagement,
            raw_data={
                "source": "syndication",
                "permalink": raw.get("permalink"),
                "conversation_id": conversation_id,
                "is_reply": is_reply,
                "is_quote": is_quote,
                "reply_to_screen_name": reply_to,
                "lang": lang,
                "user": {
                    "name": user.get("name"),
                    "screen_name": screen_name,
                    "followers_count": user.get("followers_count"),
                    "verified": user.get("verified"),
                    "is_blue_verified": user.get("is_blue_verified"),
                },
            },
            source_engine="syndication",
        )

    def fetch_timeline(self, username: str) -> List[Tweet]:
        """Fetch tweets for a given screen name via syndication endpoint.

        Returns ~100 most recent tweets (no pagination).
        """
        url = self.BASE_URL.format(username=username)
        req = urllib.request.Request(url, headers=self.HEADERS)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as e:
            self._consecutive_failures += 1
            logger.error(
                "fetch_failed_http",
                account=username,
                status=e.code,
                error=str(e),
                consecutive_failures=self._consecutive_failures,
            )
            if e.code == 429:
                raise RuntimeError("Rate limited by X syndication endpoint — backoff needed")
            raise
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error("fetch_failed", account=username, error=str(exc))
            raise

        if status != 200:
            self._consecutive_failures += 1
            raise RuntimeError(f"Unexpected HTTP {status}")

        try:
            raw_tweets = self._extract_tweets(html)
        except ValueError as exc:
            self._consecutive_failures += 1
            logger.error("parse_failed", account=username, error=str(exc))
            raise

        tweets = [self._build_tweet(raw, username) for raw in raw_tweets]
        self._consecutive_failures = 0

        logger.info(
            "fetch_success",
            account=username,
            count=len(tweets),
            engine="syndication",
        )
        return tweets

    async def fetch_timeline_async(self, username: str) -> List[Tweet]:
        """Async wrapper for fetch_timeline (runs in thread pool)."""
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.fetch_timeline, username)

    # ------------------------------------------------------------------
    # Historical backfill (limited by endpoint)
    # ------------------------------------------------------------------

    def backfill(self, accounts: Optional[List[str]] = None, min_date: Optional[str] = None) -> Dict[str, List[Tweet]]:
        """Fetch latest tweets for all tracked accounts.

        Args:
            accounts: List of screen names to scrape. Defaults to settings.
            min_date: ISO date string. If set, only return tweets newer than this.

        Returns:
            Dict mapping username to list of Tweet objects.
        """
        targets = accounts or settings.x_accounts_to_track
        results: Dict[str, List[Tweet]] = {}

        for username in targets:
            try:
                tweets = self.fetch_timeline(username)
                if min_date:
                    # Filter by date
                    min_dt = datetime.fromisoformat(min_date.replace("Z", "+00:00"))
                    tweets = [
                        t for t in tweets
                        if t.created_at and datetime.fromisoformat(t.created_at) >= min_dt
                    ]
                results[username] = tweets
                logger.info("backfill_account", account=username, count=len(tweets))
            except Exception as exc:
                logger.error("backfill_account_failed", account=username, error=str(exc))
                results[username] = []

        return results

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._active and self._consecutive_failures < 3

    @property
    def failure_count(self) -> int:
        return self._consecutive_failures

    @property
    def logged_in(self) -> bool:
        return False  # No login state

    @property
    def supports_pagination(self) -> bool:
        return False

    @property
    def approximate_tweet_limit(self) -> int:
        return 100
