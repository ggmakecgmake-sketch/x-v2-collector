"""
Playwright Engine — fallback scraper using a real browser.

Activated when twikit fails repeatedly. This uses visible/headless
Chromium to navigate X and extract tweets from the DOM.

NOTE: This is a stub / foundation implementation. Full DOM scraping
logic can be expanded when twikit breaks.
"""
from typing import List, Optional
import structlog

from src.config import settings
from src.models.tweet import Tweet

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Page = None  # type: ignore

logger = structlog.get_logger("playwright_engine")


class PlaywrightEngine:
    """Scrapes X timelines using Playwright as a real browser."""

    def __init__(self, headless: bool = True):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright not installed — run: pip install playwright")

        self.headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Launch browser and log in to X."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            # Hardening: realistic viewport and args
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()

        await self._login()
        logger.info("playwright_engine_started")

    async def stop(self):
        """Graceful shutdown."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._logged_in = False
        logger.info("playwright_engine_stopped")

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login(self):
        if not settings.twitter_username or not settings.twitter_password:
            raise RuntimeError("TWITTER_USERNAME and TWITTER_PASSWORD required for Playwright login")

        page = self._page
        await page.goto("https://x.com/login", wait_until="networkidle")

        # X login form has changed many times — we try the most common selectors.
        # If this breaks, the maintainer must update selectors.
        try:
            await page.fill("input[autocomplete='username']", settings.twitter_username)
            await page.click("text=Next")
            await page.wait_for_timeout(1000)

            # Sometimes X asks for email/phone again
            try:
                await page.fill("input[data-testid='ocfEnterTextTextInput']", settings.twitter_email or settings.twitter_username)
                await page.click("text=Next")
                await page.wait_for_timeout(1000)
            except Exception:
                pass  # No extra step required

            await page.fill("input[name='password']", settings.twitter_password)
            await page.click("text=Log in")
            await page.wait_for_url("**/home", timeout=15000)
            self._logged_in = True
            logger.info("playwright_login_successful")
        except Exception as exc:
            logger.error("playwright_login_failed", error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    async def fetch_timeline(self, username: str, count: int = 10) -> List[Tweet]:
        """Navigate to user's timeline and extract tweets from DOM."""
        if not self._page or not self._logged_in:
            raise RuntimeError("Engine not started or not logged in")

        page = self._page
        url = f"https://x.com/{username}"
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(2000)  # Let timeline hydrate

        # X articles contain tweets. We extract text from each.
        tweets: List[Tweet] = []
        seen_ids = set()

        try:
            articles = await page.query_selector_all("article[data-testid='tweet']")
            for article in articles[:count]:
                text_el = await article.query_selector("div[data-testid='tweetText']")
                text = await text_el.inner_text() if text_el else ""

                if not text.strip():
                    continue

                # Try to extract tweet ID from link href
                link_el = await article.query_selector("a[href*='/status/']")
                tweet_id = ""
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    parts = href.split("/status/")
                    if len(parts) > 1:
                        tweet_id = parts[1].split("?")[0]

                if tweet_id and tweet_id in seen_ids:
                    continue
                seen_ids.add(tweet_id)

                # Try to get metrics (likes, replies, etc.) — best-effort
                metrics = {}
                metric_labels = [
                    ("reply_count", "[data-testid='reply']"),
                    ("retweet_count", "[data-testid='retweet']"),
                    ("favorite_count", "[data-testid='like']"),
                ]
                for key, selector in metric_labels:
                    el = await article.query_selector(selector)
                    if el:
                        val = await el.inner_text()
                        metrics[key] = val

                tweet = Tweet(
                    tweet_id=tweet_id or f"pw_{hash(text) & 0xFFFFFFFF}",
                    author_username=username,
                    text=text,
                    engagement=metrics,
                    raw_data={"playwright_scrape": True},
                    source_engine="playwright",
                )
                tweets.append(tweet)
        except Exception as exc:
            logger.error("playwright_scrape_failed", account=username, error=str(exc))
            raise

        logger.info(
            "fetch_success",
            account=username,
            count=len(tweets),
            engine="playwright",
        )
        return tweets

    @property
    def healthy(self) -> bool:
        return self._logged_in and self._browser is not None

    @property
    def failure_count(self) -> int:
        return 0  # TODO: track failures
