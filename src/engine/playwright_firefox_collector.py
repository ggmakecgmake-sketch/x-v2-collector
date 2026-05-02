"""
Playwright + Injected Firefox Cookies — Full Timeline Scraper
Reads cookies from your running Firefox, injects them into a fresh
Chromium browser, and scrolls X timeline to extract all tweets.
"""

import json
import re
import sqlite3
import shutil
import tempfile
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict

from playwright.async_api import async_playwright


@dataclass
class Tweet:
    tweet_id: str
    username: str
    display_name: str
    text: str
    created_at: str
    likes: int
    replies: int
    retweets: int
    quotes: int
    is_reply: bool
    is_retweet: bool
    source: str = "playwright_cookie_injected"


class FirefoxCookieReader:
    """Reads X cookies from Firefox's cookies.sqlite (live copy)."""

    PROFILES = [
        "~/.config/mozilla/firefox/*.default-release*",
        "~/.mozilla/firefox/*.default-release*",
    ]

    def __init__(self):
        self.profile = self._find_profile()

    def _find_profile(self) -> Path:
        import glob
        candidates = []
        for pattern in self.PROFILES:
            matches = glob.glob(Path(pattern).expanduser().as_posix())
            for m in matches:
                p = Path(m)
                if (p / "cookies.sqlite").exists():
                    candidates.append(p)
        if not candidates:
            raise RuntimeError("No Firefox profile found")
        candidates.sort(key=lambda p: (p / "cookies.sqlite").stat().st_mtime, reverse=True)
        return candidates[0]

    def get_cookies(self, domain_filter: str = "x.com") -> list[dict]:
        """Return cookies in Playwright format."""
        db_src = self.profile / "cookies.sqlite"
        db_tmp = Path(tempfile.gettempdir()) / f"ff_cookies_{int(time.time())}.sqlite"
        shutil.copy2(db_src, db_tmp)
        wal = db_src.parent / "cookies.sqlite-wal"
        if wal.exists():
            shutil.copy2(wal, str(db_tmp) + "-wal")

        try:
            conn = sqlite3.connect(str(db_tmp))
            cur = conn.cursor()
            cur.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies WHERE host LIKE ? ORDER BY name",
                (f"%{domain_filter}%",),
            )
            rows = cur.fetchall()
            conn.close()

            cookies = []
            for name, value, host, path, expiry, secure, httponly, samesite in rows:
                # Fix expires for Playwright: Firefox stores ms, Playwright expects seconds
                expires = -1
                if expiry is not None and isinstance(expiry, (int, float)) and expiry > 0:
                    # Firefox stores expiry in milliseconds; convert to seconds
                    expires = int(expiry / 1000)
                    # Sanity: if still absurdly large, cap it
                    if expires > 4102444800:  # year 2100
                        expires = 4102444800
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": path or "/",
                    "expires": expires,
                    "httpOnly": bool(httponly),
                    "secure": bool(secure),
                    "sameSite": "Lax" if samesite == 0 else "Strict" if samesite == 1 else "None",
                })
            return cookies
        finally:
            db_tmp.unlink(missing_ok=True)


class XTimelineScraper:
    """Uses Playwright with injected Firefox cookies to scroll timelines."""

    def __init__(self, cookies: list[dict]):
        self.cookies = cookies

    async def collect(self, screen_name: str, max_scrolls: int = 300, headless: bool = True):
        print(f"\n[==>] Scraping @{screen_name} (Playwright + injected cookies)")

        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                )
                await context.add_cookies(self.cookies)
                page = await context.new_page()

                url = f"https://x.com/{screen_name}"
                print(f"[+] Navigating to {url}")

                try:
                    # domcontentloaded is faster than networkidle for X SPAs
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # Wait for first tweet with shorter timeout
                    await page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
                    print("[+] Timeline loaded")
                except Exception as e:
                    print(f"[!] Failed to load ({e.__class__.__name__}): {e}")
                    try:
                        await page.screenshot(path=f"data/debug_{screen_name}.png")
                    except Exception:
                        pass
                    return []

                seen_ids = set()
                all_tweets: list[Tweet] = []
                no_new = 0

                for scroll in range(1, max_scrolls + 1):
                    # Parse visible tweets
                    articles = await page.query_selector_all("article[data-testid='tweet']")
                    new_this_scroll = 0

                    for art in articles:
                        tweet = await self._parse_article(art, screen_name)
                        if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                            seen_ids.add(tweet.tweet_id)
                            all_tweets.append(tweet)
                            new_this_scroll += 1

                    print(f"[+] Scroll {scroll}/{max_scrolls}: +{new_this_scroll} new | total {len(all_tweets)} | streak={no_new}")

                    if new_this_scroll == 0:
                        no_new += 1
                        if no_new >= 5:
                            print("[+] No new tweets after 5 scrolls — end of timeline")
                            break
                    else:
                        no_new = 0

                    # Scroll
                    await page.evaluate("window.scrollBy(0, 2500)")
                    await page.wait_for_timeout(random.randint(1200, 2500))

                await browser.close()
                browser = None
                print(f"[==>] Total: {len(all_tweets)} tweets")
                return all_tweets
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _parse_article(self, article, default_user: str) -> Tweet | None:
        try:
            # Tweet ID from status link
            tweet_id = ""
            links = await article.query_selector_all("a[href*='/status/']")
            for a in links:
                href = await a.get_attribute("href") or ""
                m = re.search(r"/status/(\d+)", href)
                if m:
                    tweet_id = m.group(1)
                    break
            if not tweet_id:
                return None

            # Username from link href /text
            username = default_user
            for a in links:
                href = await a.get_attribute("href") or ""
                um = re.match(r"^/([A-Za-z0-9_]+)$", href)
                if um:
                    username = um.group(1)
                    break

            # Display name
            display_name = ""
            name_el = await article.query_selector("a[data-testid='User-Name'] span")
            if name_el:
                display_name = (await name_el.text_content() or "").strip()

            # Tweet text
            text = ""
            text_el = await article.query_selector("div[data-testid='tweetText']")
            if text_el:
                text = (await text_el.text_content() or "").strip()

            # Date
            created_at = ""
            time_el = await article.query_selector("time")
            if time_el:
                created_at = await time_el.get_attribute("datetime") or ""

            # Engagement
            likes = await self._count(article, "like")
            replies = await self._count(article, "reply")
            retweets = await self._count(article, "retweet")

            # Reply / retweet detection
            is_reply = await article.query_selector("div[data-testid='socialContext']") is not None
            is_retweet = False
            rt_label = await article.query_selector("span:has-text('reposted')")
            if rt_label:
                is_retweet = True

            return Tweet(
                tweet_id=tweet_id,
                username=username,
                display_name=display_name,
                text=text,
                created_at=created_at,
                likes=likes,
                replies=replies,
                retweets=retweets,
                quotes=0,
                is_reply=is_reply,
                is_retweet=is_retweet,
            )
        except Exception:
            return None

    async def _count(self, article, suffix: str) -> int:
        try:
            btn = await article.query_selector(f"button[data-testid='{suffix}']")
            if btn:
                label = await btn.get_attribute("aria-label") or ""
                nums = re.findall(r"[\d,]+", label)
                if nums:
                    return int(nums[0].replace(",", ""))
        except Exception:
            pass
        return 0

    def save(self, tweets: list[Tweet], screen_name: str, out_dir: Path = Path("data/playwright_results")):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        raw = out_dir / f"{screen_name}_all_{ts}.json"
        with open(raw, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in tweets], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved all to {raw}")

        cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
        recent = []
        for t in tweets:
            try:
                dt = datetime.fromisoformat(t.created_at.replace("Z", "+00:00"))
                if dt >= cutoff:
                    recent.append(t)
            except Exception:
                continue
        rec = out_dir / f"{screen_name}_last4years_{ts}.json"
        with open(rec, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in recent], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved recent to {rec}")
        return raw, rec


async def main():
    print("=" * 60)
    print("Playwright + Injected Firefox Cookies — Full Timeline Scraper")
    print("=" * 60)

    # Step 1: Read cookies
    print("\n[1/3] Reading cookies from Firefox...")
    reader = FirefoxCookieReader()
    cookies = reader.get_cookies("x.com")
    print(f"[+] Read {len(cookies)} cookies from Firefox")

    # Step 2: Scrape
    print("\n[2/3] Scraping timelines...")
    scraper = XTimelineScraper(cookies)

    targets = ["financialjuice", "Deltaone"]
    for target in targets:
        tweets = await scraper.collect(target, max_scrolls=300, headless=True)
        if tweets:
            scraper.save(tweets, target)
        print()

    print("[3/3] Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
