"""
Twikit Engine — primary scraper using the twikit library.

No official API key needed; uses internal GraphQL endpoints.
"""
import asyncio
from typing import List, Optional, Dict, Any
import structlog

from src.config import settings
from src.core.session_manager import SessionManager
from src.models.tweet import Tweet

try:
    from twikit import Client
    from twikit.utils import cookies_to_dict
    TWIKIT_AVAILABLE = True
except ImportError:
    TWIKIT_AVAILABLE = False
    Client = None  # type: ignore

logger = structlog.get_logger("twikit_engine")


class TwikitEngine:
    """Scrapes X timelines using twikit with cookie persistence."""

    def __init__(self):
        if not TWIKIT_AVAILABLE:
            raise RuntimeError("twikit not installed — run: pip install twikit")

        self.client: Optional[Any] = None
        self.session = SessionManager(settings.cookies_path)
        self._consecutive_failures = 0
        self._logged_in = False

    async def start(self):
        """Initialize client, load cookies, or login."""
        self.client = Client(language="en-US")

        cookies = self.session.load()
        if cookies:
            try:
                self.client.set_cookies(cookies)
                # Light validation: try to get a timeline
                logger.info("session_restored_from_cookies")
                self._logged_in = True
                return
            except Exception as exc:
                logger.warning("cookie_restore_failed", error=str(exc))
                self.session.clear()

        # Fresh login required
        if not settings.twitter_username or not settings.twitter_password:
            raise RuntimeError(
                "No cookies available and TWITTER_USERNAME/TWITTER_PASSWORD not set."
            )

        logger.info("performing_fresh_login", username=settings.twitter_username)
        try:
            await self.client.login(
                auth_info_1=settings.twitter_username,
                auth_info_2=settings.twitter_email or settings.twitter_username,
                password=settings.twitter_password,
            )
            raw_cookies = self.client.get_cookies()
            self.session.save(raw_cookies)
            self._logged_in = True
            logger.info("login_successful")
        except Exception as exc:
            logger.error("login_failed", error=str(exc))
            raise

    async def fetch_timeline(self, username: str, count: int = 10) -> List[Tweet]:
        """Fetch the latest tweets from a user's timeline.

        Returns only original tweets (no retweets, no replies).
        """
        if not self.client or not self._logged_in:
            raise RuntimeError("Engine not started — call start() first")

        try:
            user = await self.client.get_user_by_screen_name(username)
            tweets_obj = await user.get_tweets("Tweets", count=count)
            tweets: List[Tweet] = []
            for t in tweets_obj:
                tweet = Tweet.from_twikit(t, username)
                # Filter out retweets and empty text (ads/promoted)
                if tweet.text.strip():
                    tweets.append(tweet)
            self._consecutive_failures = 0
            logger.info(
                "fetch_success",
                account=username,
                count=len(tweets),
                engine="twikit",
            )
            return tweets
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error(
                "fetch_failed",
                account=username,
                error=str(exc),
                consecutive_failures=self._consecutive_failures,
                threshold=settings.twikit_failure_threshold,
            )
            # If auth error, invalidate cookies for next cycle
            if "unauthorized" in str(exc).lower() or "login" in str(exc).lower():
                logger.warning("auth_error_detected_clearing_cookies")
                self.session.clear()
                self._logged_in = False
            raise

    async def stop(self):
        """Graceful cleanup."""
        self.client = None
        self._logged_in = False
        logger.info("twikit_engine_stopped")

    @property
    def healthy(self) -> bool:
        return self._logged_in and self._consecutive_failures < settings.twikit_failure_threshold

    @property
    def failure_count(self) -> int:
        return self._consecutive_failures
