"""
Playwright Engine — fallback scraper using a real browser.

Supports TWO modes:
- Anonymous: no login required; navigates timeline pages directly
             and extracts tweets visible without authentication.
- Authenticated: uses TWITTER_USERNAME/TWITTER_PASSWORD to log in.

After X changes in 2024, anonymous access is severely restricted.
Most timelines redirect to login. This engine works best with credentials.
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
    """Scrapes X timelines using Playwright as a real browser.

    Can operate in anonymous mode (attempting to fetch public visible tweets)
    or authenticated mode (logging in with burner account).
    """

    def __init__(self, headless: bool = True, anonymous: bool = False):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright not installed — run: pip install playwright")

        self.headless = headless
        self.anonymous = anonymous  # True = no login
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in = False
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Launch browser and optionally log in to X."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-images",  # Reduces bandwidth
                "--disable-javascript",  # DISABLED: X needs JS. Keep commented.
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
        )
        self._page = await self._context.new_page()

        if not self.anonymous:
            # Attempt login if credentials available
            if settings.twitter_username and settings.twitter_password:
                await self._login()
            else:
                logger.warning("playwright_no_credentials_switching_to_anonymous")
                self.anonymous = True

        logger.info("playwright_engine_started", anonymous=self.anonymous, logged_in=self._logged_in)

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
            raise RuntimeError("TWITTER_USERNAME and TWITTER_PASSWORD required for authenticated mode")

        page = self._page
        await page.goto("https://x.com/i/flow/login", wait_until="networkidle")

        try:
            # Step 1: username
            await page.fill("input[autocomplete='username']", settings.twitter_username, timeout=10000)
            await page.click("button:has-text('Next')")
            await page.wait_for_timeout(1500)

            # Step 2: optional challenge (email/phone)
            try:
                challenge_input = page.locator("input[data-testid='ocfEnterTextTextInput']")
                if await challenge_input.is_visible(timeout=3000):
                    await challenge_input.fill(settings.twitter_email or settings.twitter_username)
                    await page.click("button:has-text('Next')")
                    await page.wait_for_timeout(1500)
            except Exception:
                pass

            # Step 3: password
            await page.fill("input[name='password']", settings.twitter_password, timeout=10000)
            await page.click("button[data-testid='LoginForm_Login_Button']")

            # Wait for navigation or confirmation
            try:
                await page.wait_for_url("https://x.com/home", timeout=20000)
            except Exception:
                # Sometimes it redirects to onboarding or other path
                current_url = page.url
                if "x.com/" in current_url and "i/flow" not in current_url:
                    logger.info("login_successful_alternative_redirect", url=current_url)
                else:
                    raise RuntimeError(f"Login failed. Current URL: {current_url}")

            self._logged_in = True
            logger.info("playwright_login_successful")
        except Exception as exc:
            logger.error("playwright_login_failed", error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    async def fetch_timeline(self, username: str, count: int = 10) -> List[Tweet]:
        """Navigate to user's timeline and extract tweets from DOM.

        In anonymous mode, most timelines will redirect to login page.
        We handle this gracefully and report what we see.
        """
        if not self._page:
            raise RuntimeError("Engine not started")

        page = self._page
        url = f"https://x.com/{username}"

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(500)  # Brief wait for JS hydration
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error("navigation_failed", account=username, error=str(exc))
            raise

        current_url = page.url
        logger.info("page_loaded", account=username, url=current_url)

        # Detect login wall (X redirects to login if not authenticated)
        if "i/flow/login" in current_url and not self._logged_in:
            self._consecutive_failures += 1
            logger.error(
                "login_wall_detected",
                account=username,
                note="X requires login to view this timeline. Use TWITTER_USERNAME/TWITTER_PASSWORD or the account may be restricted.",
            )
            raise RuntimeError("X redirected to login — anonymous access blocked")

        # Wait for timeline to load — in anonymous mode this often fails
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=8000)
        except Exception:
            # Take screenshot for debugging
            screenshot_path = f"/tmp/x_v2_debug_{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.info("debug_screenshot_saved", path=screenshot_path)
            except Exception:
                pass
            raise RuntimeError(f"No tweets found on page. Possible login wall or rate limit. URL: {current_url}")

        tweets: List[Tweet] = []
        seen_ids = set()

        try:
            articles = await page.query_selector_all("article[data-testid='tweet']")
            for article in articles[:count]:
                # --- Text ---
                text = ""
                try:
                    text_el = await article.query_selector("div[data-testid='tweetText']")
                    if text_el:
                        text = await text_el.inner_text(timeout=5000)
                except Exception:
                    pass

                if not text.strip():
                    # Try alternative selector
                    try:
                        text_el = await article.query_selector("div[dir='auto']")
                        if text_el:
                            text = await text_el.inner_text(timeout=2000)
                    except Exception:
                        pass

                if not text.strip():
                    continue

                # --- Tweet ID ---
                tweet_id = ""
                try:
                    time_link = await article.query_selector("a[href*='/status/']")
                    if time_link:
                        href = await time_link.get_attribute("href") or ""
                        parts = href.split("/status/")
                        if len(parts) > 1:
                            tweet_id = parts[1].split("?")[0]
                except Exception:
                    pass

                if tweet_id and tweet_id in seen_ids:
                    continue
                seen_ids.add(tweet_id)

                # --- Timestamp ---
                created_at = None
                try:
                    time_el = await article.query_selector("time")
                    if time_el:
                        created_at = await time_el.get_attribute("datetime")
                except Exception:
                    pass

                # --- Metrics (best-effort) ---
                metrics = {}
                metric_labels = [
                    ("reply_count", "[data-testid='reply'] span"),
                    ("retweet_count", "[data-testid='retweet'] span"),
                    ("favorite_count", "[data-testid='like'] span"),
                ]
                for key, selector in metric_labels:
                    try:
                        el = await article.query_selector(selector)
                        if el:
                            val = await el.inner_text(timeout=2000)
                            metrics[key] = val
                    except Exception:
                        pass

                tweet = Tweet(
                    tweet_id=tweet_id or f"pw_{hash(text) & 0xFFFFFFFF}",
                    author_username=username,
                    text=text,
                    created_at=created_at,
                    engagement=metrics,
                    raw_data={
                        "playwright_scrape": True,
                        "anonymous": self.anonymous,
                        "url": current_url,
                    },
                    source_engine="playwright_anonymous" if self.anonymous else "playwright",
                )
                tweets.append(tweet)

        except Exception as exc:
            self._consecutive_failures += 1
            logger.error("playwright_scrape_failed", account=username, error=str(exc))
            raise

        self._consecutive_failures = 0
        logger.info(
            "fetch_success",
            account=username,
            count=len(tweets),
            engine="playwright",
            anonymous=self.anonymous,
        )
        return tweets

    # ------------------------------------------------------------------
    # Public timeline / search without login (experimental)
    # ------------------------------------------------------------------

    async def search_anonymous(self, query: str, count: int = 20) -> List[Tweet]:
        """Attempt to perform a search on X without being logged in.

        This navigates to the X search URL directly.
        Warning: highly likely to hit login wall.
        """
        if not self._page:
            raise RuntimeError("Engine not started")

        page = self._page
        encoded_query = (
            query.replace(" ", "%20")
            .replace("#", "%23")
            .replace("@", "%40")
            .replace("&", "%26")
        )
        url = f"https://x.com/search?q={encoded_query}&f=live"

        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        current_url = page.url
        if "i/flow/login" in current_url:
            raise RuntimeError("Search requires login — anonymous access blocked")

        tweets: List[Tweet] = []
        seen_ids = set()

        articles = await page.query_selector_all("article[data-testid='tweet']")
        for article in articles[:count]:
            text = ""
            tweet_id = ""
            author_username = query  # Placeholder

            try:
                text_el = await article.query_selector("div[data-testid='tweetText']")
                if text_el:
                    text = await text_el.inner_text()
            except Exception:
                pass

            if not text.strip():
                continue

            try:
                time_link = await article.query_selector("a[href*='/status/']")
                if time_link:
                    href = await time_link.get_attribute("href") or ""
                    # href format: /username/status/123456
                    parts = href.strip("/").split("/")
                    if len(parts) >= 3 and parts[1] == "status":
                        tweet_id = parts[2].split("?")[0]
                        author_username = parts[0]
            except Exception:
                pass

            if tweet_id and tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)

            tweet = Tweet(
                tweet_id=tweet_id or f"pw_search_{hash(text) & 0xFFFFFFFF}",
                author_username=author_username,
                text=text,
                source_engine="playwright_search",
                raw_data={"query": query, "anonymous": True},
            )
            tweets.append(tweet)

        logger.info("search_anonymous_complete", query=query, count=len(tweets))
        return tweets

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._consecutive_failures < 3

    @property
    def failure_count(self) -> int:
        return self._consecutive_failures

    @property
    def logged_in(self) -> bool:
        return self._logged_in
