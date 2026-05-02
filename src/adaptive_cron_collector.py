#!/usr/bin/env python3
"""
Adaptive Cron Collector — Self-healing X/Twitter scraper

Logic:
  1. Every 5 min: audit current state (tweet counts, strategy history, errors)
  2. If account has 0 tweets after N attempts → switch to next strategy
  3. If account stuck (no new tweets in last 3 runs) → switch strategy
  4. If all strategies exhausted → escalate (log error, retry from top)
  5. Once account has >100 tweets and is_complete → switch to FAST maintenance

Strategies (tried in rotation per account):
  A. syndication  — lightweight, no browser needed, ~100 tweets max
  B. selenium_cookies  — Chrome headless + injected Firefox cookies
  C. requests_html  — direct HTTP with cookies, parse HTML
  D. playwright_stealth  — Playwright with stealth plugins
  E. selenium_visible  — Chrome non-headless (fallback for detection)
"""

import json
import re
import sqlite3
import shutil
import tempfile
import time
import random
import signal
import sys
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field

# ── Selenium imports (lazy-loaded per strategy) ────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Config ─────────────────────────────────────────────────────
ACCOUNTS = ["financialjuice", "Deltaone"]
DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "adaptive_state.json"
TWEETS_DIR = DATA_DIR / "tweets"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"
LOG_FILE = DATA_DIR / "adaptive_cron.log"
COOKIE_TTL = 120
MAX_SCROLLS = 150
NO_NEW_THRESHOLD = 5
REQUEST_TIMEOUT = 30

# Strategy rotation order
STRATEGIES = ["syndication", "selenium_cookies", "requests_html", "playwright_stealth"]
if not HAS_SELENIUM:
    STRATEGIES.remove("selenium_cookies")
if not HAS_PLAYWRIGHT:
    STRATEGIES.remove("playwright_stealth")

# ── Models ─────────────────────────────────────────────────────
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
    quotes: int = 0
    is_reply: bool = False
    is_retweet: bool = False
    source: str = "adaptive"


@dataclass
class AccountState:
    screen_name: str
    is_complete: bool = False
    total_collected: int = 0
    last_tweet_id: str = ""
    last_run: str = ""
    last_strategy: str = ""
    strategy_history: list[str] = field(default_factory=list)
    deep_runs: int = 0
    consecutive_no_progress: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


class AdaptiveState:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.accounts: dict[str, AccountState] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    # Handle migration from old state format
                    if "strategy_history" not in v:
                        v["strategy_history"] = []
                    if "consecutive_no_progress" not in v:
                        v["consecutive_no_progress"] = 0
                    self.accounts[k] = AccountState(**v)
            except Exception:
                pass

    def save(self):
        TWEETS_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self.accounts.items()}, f, ensure_ascii=False, indent=2)

    def get_or_create(self, screen_name: str) -> AccountState:
        if screen_name not in self.accounts:
            self.accounts[screen_name] = AccountState(screen_name=screen_name)
        return self.accounts[screen_name]

    def pick_strategy(self, acc: AccountState) -> str:
        """Pick next strategy based on rotation."""
        available = [s for s in STRATEGIES]
        if not available:
            return "syndication"  # fallback

        # If no strategy tried yet, start with syndication (fastest)
        if not acc.strategy_history:
            return "syndication"

        # If stuck (no progress in 3+ runs), rotate to next strategy
        if acc.consecutive_no_progress >= 3:
            last = acc.last_strategy
            if last in available:
                idx = available.index(last)
                next_idx = (idx + 1) % len(available)
                return available[next_idx]
            return available[0]

        # If first runs with no results, rotate faster
        if acc.total_collected == 0 and acc.deep_runs >= 2:
            last = acc.last_strategy
            if last in available:
                idx = available.index(last)
                next_idx = (idx + 1) % len(available)
                return available[next_idx]

        # Default: stick with last if it was working, or try syndication for quick wins
        if acc.total_collected > 0:
            return acc.last_strategy or "syndication"
        return "syndication"


# ── Logging ────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Cookies ────────────────────────────────────────────────────
class FirefoxCookieExtractor:
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
            for m in glob.glob(str(Path(pattern).expanduser())):
                p = Path(m)
                if (p / "cookies.sqlite").exists():
                    candidates.append(p)
        if not candidates:
            raise RuntimeError("No Firefox profile found")
        candidates.sort(key=lambda p: (p / "cookies.sqlite").stat().st_mtime, reverse=True)
        return candidates[0]

    def get_cookies(self) -> dict[str, str]:
        db_src = self.profile / "cookies.sqlite"
        db_tmp = Path(tempfile.gettempdir()) / f"ff_adaptive_{int(time.time())}.sqlite"
        shutil.copy2(db_src, db_tmp)
        wal = db_src.parent / "cookies.sqlite-wal"
        if wal.exists():
            shutil.copy2(wal, str(db_tmp) + "-wal")
        try:
            conn = sqlite3.connect(str(db_tmp))
            cur = conn.cursor()
            cur.execute("SELECT name, value FROM moz_cookies WHERE host LIKE ? ORDER BY name", ("%x.com%",))
            cookies = {n: v for n, v in cur.fetchall()}
            conn.close()
            return cookies
        finally:
            db_tmp.unlink(missing_ok=True)


def get_cookies() -> dict[str, str]:
    if COOKIE_CACHE.exists():
        try:
            meta = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            age = time.time() - meta.get("ts", 0)
            if age < COOKIE_TTL:
                log(f"Using cached cookies ({age:.0f}s old)")
                return meta["cookies"]
        except Exception:
            pass

    extractor = FirefoxCookieExtractor()
    cookies = extractor.get_cookies()
    COOKIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(COOKIE_CACHE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "cookies": cookies}, f)
    log(f"Fresh cookies: {len(cookies)}")
    return cookies


# ── Persistence ────────────────────────────────────────────────
def load_existing(screen_name: str) -> dict[str, Tweet]:
    path = TWEETS_DIR / f"{screen_name}_all.json"
    tweets: dict[str, Tweet] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw:
                t = Tweet(**item)
                tweets[t.tweet_id] = t
        except Exception:
            pass
    return tweets


def save_tweets(screen_name: str, tweets: dict[str, Tweet]):
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    all_path = TWEETS_DIR / f"{screen_name}_all.json"
    with open(all_path, "w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in tweets.values()], f, ensure_ascii=False, indent=2)

    cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
    recent = []
    for t in tweets.values():
        try:
            dt = datetime.strptime(t.created_at, "%a %b %d %H:%M:%S +0000 %Y")
            if dt.replace(tzinfo=timezone.utc) >= cutoff:
                recent.append(t)
        except Exception:
            continue
    recent_path = TWEETS_DIR / f"{screen_name}_last4years.json"
    with open(recent_path, "w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in recent], f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# STRATEGY IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════

# ── Strategy A: Syndication ────────────────────────────────────
def strategy_syndication(screen_name: str, cookies: dict[str, str]) -> list[Tweet]:
    import requests
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{screen_name}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://x.com/",
    }
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log(f"  [syndication] HTTP error: {e}")
        return []
    if resp.status_code == 429:
        log(f"  [syndication] Rate limited")
        return []
    if resp.status_code != 200:
        log(f"  [syndication] HTTP {resp.status_code}")
        return []

    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text)
    if not match:
        log(f"  [syndication] No __NEXT_DATA__")
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        log(f"  [syndication] JSON parse error")
        return []

    timeline = data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
    tweets: list[Tweet] = []
    for entry in timeline:
        t = entry.get("content", {}).get("tweet", {})
        if not t:
            continue
        tid = str(t.get("id", ""))
        if not tid:
            continue
        u = t.get("user", {})
        tweets.append(Tweet(
            tweet_id=tid,
            username=u.get("screen_name", screen_name),
            display_name=u.get("name", ""),
            text=t.get("full_text", ""),
            created_at=t.get("created_at", ""),
            likes=t.get("favorite_count", 0),
            replies=t.get("reply_count", 0),
            retweets=t.get("retweet_count", 0),
            quotes=t.get("quote_count", 0),
            is_reply=bool(t.get("in_reply_to_status_id_str")),
            is_retweet=bool(t.get("retweeted_status")),
            source="syndication",
        ))
    log(f"  [syndication] {len(tweets)} tweets")
    return tweets


# ── Strategy B: Selenium + Chrome + Cookies ────────────────────
def strategy_selenium_cookies(screen_name: str, cookies: dict[str, str], max_scrolls: int = MAX_SCROLLS) -> list[Tweet]:
    if not HAS_SELENIUM:
        log(f"  [selenium] Not available")
        return []

    tweets: list[Tweet] = []
    driver = None
    try:
        opts = ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,800")
        opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        try:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        except Exception:
            driver = webdriver.Chrome(options=opts)

        driver.get("https://x.com")
        time.sleep(3)

        critical = ["auth_token", "ct0", "twid", "kdt", "gt", "att", "_twpid"]
        for name, value in cookies.items():
            if name in critical:
                try:
                    driver.add_cookie({"name": name, "value": value, "domain": ".x.com", "path": "/"})
                except Exception:
                    pass

        driver.get(f"https://x.com/{screen_name}")
        time.sleep(4)

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-testid='tweet']"))
            )
        except Exception:
            log(f"  [selenium] Timeline load timeout")

        seen_ids = set()
        no_new = 0
        for scroll in range(1, max_scrolls + 1):
            articles = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            new_this = 0
            for art in articles:
                tweet = _parse_selenium(art, screen_name)
                if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                    seen_ids.add(tweet.tweet_id)
                    tweets.append(tweet)
                    new_this += 1
            log(f"    scroll {scroll:3d}: +{new_this:3d} new | total {len(tweets):4d} | streak={no_new}")
            if new_this == 0:
                no_new += 1
                if no_new >= NO_NEW_THRESHOLD:
                    log(f"  [selenium] End of timeline")
                    break
            else:
                no_new = 0
            driver.execute_script("window.scrollBy(0, 2500)")
            time.sleep(random.uniform(1.5, 3.0))

        driver.quit()
        driver = None
    except Exception as e:
        log(f"  [selenium] Error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    log(f"  [selenium] {len(tweets)} tweets")
    return tweets


def _parse_selenium(article, default_user: str) -> Tweet | None:
    try:
        tweet_id = ""
        links = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
        for a in links:
            href = a.get_attribute("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if m:
                tweet_id = m.group(1)
                break
        if not tweet_id:
            return None

        username = default_user
        for a in links:
            href = a.get_attribute("href") or ""
            um = re.match(r"https?://x\.com/([A-Za-z0-9_]+)$", href)
            if um:
                username = um.group(1)
                break

        display_name = ""
        try:
            display_name = article.find_element(By.CSS_SELECTOR, "a[data-testid='User-Name'] span").text.strip()
        except Exception:
            pass

        text = ""
        try:
            text = article.find_element(By.CSS_SELECTOR, "div[data-testid='tweetText']").text.strip()
        except Exception:
            pass

        created_at = ""
        try:
            created_at = article.find_element(By.CSS_SELECTOR, "time").get_attribute("datetime") or ""
        except Exception:
            pass

        likes = _count_sel(article, "like")
        replies = _count_sel(article, "reply")
        retweets = _count_sel(article, "retweet")

        is_reply = False
        try:
            article.find_element(By.CSS_SELECTOR, "[data-testid='socialContext']")
            is_reply = True
        except Exception:
            pass

        is_retweet = False
        try:
            article.find_element(By.XPATH, ".//span[contains(text(), 'reposted')]")
            is_retweet = True
        except Exception:
            pass

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
            source="selenium_cookies",
        )
    except Exception:
        return None


def _count_sel(article, suffix: str) -> int:
    try:
        btn = article.find_element(By.CSS_SELECTOR, f"button[data-testid='{suffix}']")
        label = btn.get_attribute("aria-label") or ""
        nums = re.findall(r"[\d,]+", label)
        if nums:
            return int(nums[0].replace(",", ""))
    except Exception:
        pass
    return 0


# ── Strategy C: Requests + HTML parse ────────────────────────────
def strategy_requests_html(screen_name: str, cookies: dict[str, str]) -> list[Tweet]:
    import requests
    url = f"https://x.com/{screen_name}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://x.com/",
    }
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log(f"  [requests_html] HTTP error: {e}")
        return []

    if resp.status_code != 200:
        log(f"  [requests_html] HTTP {resp.status_code}")
        return []

    # Look for __NEXT_DATA__
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text)
    if not match:
        log(f"  [requests_html] No __NEXT_DATA__ — probably login wall")
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        log(f"  [requests_html] JSON parse error")
        return []

    # Extract tweets from NextData
    tweets: list[Tweet] = []
    try:
        timeline = (
            data.get("props", {})
            .get("pageProps", {})
            .get("timeline", {})
            .get("entries", [])
        )
        for entry in timeline:
            t = entry.get("content", {}).get("tweet", {})
            if not t:
                continue
            tid = str(t.get("id", ""))
            if not tid:
                continue
            u = t.get("user", {})
            tweets.append(Tweet(
                tweet_id=tid,
                username=u.get("screen_name", screen_name),
                display_name=u.get("name", ""),
                text=t.get("full_text", ""),
                created_at=t.get("created_at", ""),
                likes=t.get("favorite_count", 0),
                replies=t.get("reply_count", 0),
                retweets=t.get("retweet_count", 0),
                quotes=t.get("quote_count", 0),
                is_reply=bool(t.get("in_reply_to_status_id_str")),
                is_retweet=bool(t.get("retweeted_status")),
                source="requests_html",
            ))
    except Exception as e:
        log(f"  [requests_html] Parse error: {e}")

    log(f"  [requests_html] {len(tweets)} tweets")
    return tweets


# ── Strategy D: Playwright Stealth ───────────────────────────────
def strategy_playwright_stealth(screen_name: str, cookies: dict[str, str], max_scrolls: int = MAX_SCROLLS) -> list[Tweet]:
    if not HAS_PLAYWRIGHT:
        log(f"  [playwright] Not available")
        return []

    tweets: list[Tweet] = []
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            pw_cookies = []
            for name, value in cookies.items():
                pw_cookies.append({
                    "name": name,
                    "value": value,
                    "domain": ".x.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": name in ("auth_token", "kdt", "att"),
                    "secure": True,
                })
            context.add_cookies(pw_cookies)
            page = context.new_page()

            url = f"https://x.com/{screen_name}"
            log(f"  [playwright] Navigating {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_selector("article[data-testid='tweet']", timeout=15000)
                log(f"  [playwright] Loaded")
            except PWTimeout:
                log(f"  [playwright] Timeout")
                return tweets
            except Exception as e:
                log(f"  [playwright] Load error: {e}")
                return tweets

            seen_ids = set()
            no_new = 0
            for scroll in range(1, max_scrolls + 1):
                articles = page.query_selector_all("article[data-testid='tweet']")
                new_this = 0
                for art in articles:
                    tweet = _parse_pw(art, screen_name)
                    if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                        seen_ids.add(tweet.tweet_id)
                        tweets.append(tweet)
                        new_this += 1
                log(f"    scroll {scroll:3d}: +{new_this:3d} new | total {len(tweets):4d}")
                if new_this == 0:
                    no_new += 1
                    if no_new >= NO_NEW_THRESHOLD:
                        log(f"  [playwright] End of timeline")
                        break
                else:
                    no_new = 0
                page.evaluate("window.scrollBy(0, 2500)")
                page.wait_for_timeout(random.randint(1200, 2500))

            browser.close()
            browser = None
    except Exception as e:
        log(f"  [playwright] Error: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    log(f"  [playwright] {len(tweets)} tweets")
    return tweets


def _parse_pw(article, default_user: str) -> Tweet | None:
    try:
        tweet_id = ""
        links = article.query_selector_all("a[href*='/status/']")
        for a in links:
            href = a.get_attribute("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if m:
                tweet_id = m.group(1)
                break
        if not tweet_id:
            return None

        username = default_user
        for a in links:
            href = a.get_attribute("href") or ""
            um = re.match(r"https?://x\.com/([A-Za-z0-9_]+)$", href)
            if um:
                username = um.group(1)
                break

        display_name = ""
        try:
            display_name = article.query_selector("a[data-testid='User-Name'] span").text_content().strip()
        except Exception:
            pass

        text = ""
        try:
            text = article.query_selector("div[data-testid='tweetText']").text_content().strip()
        except Exception:
            pass

        created_at = ""
        try:
            created_at = article.query_selector("time").get_attribute("datetime") or ""
        except Exception:
            pass

        likes = 0
        replies = 0
        retweets = 0
        try:
            label = article.query_selector("button[data-testid='like']").get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                likes = int(nums[0].replace(",", ""))
        except Exception:
                pass
        try:
            label = article.query_selector("button[data-testid='reply']").get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                replies = int(nums[0].replace(",", ""))
        except Exception:
            pass
        try:
            label = article.query_selector("button[data-testid='retweet']").get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                retweets = int(nums[0].replace(",", ""))
        except Exception:
            pass

        is_reply = article.query_selector("[data-testid='socialContext']") is not None
        is_retweet = article.query_selector("span:has-text('reposted')") is not None

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
            source="playwright_stealth",
        )
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════════════
STRATEGY_MAP = {
    "syndication": strategy_syndication,
    "selenium_cookies": strategy_selenium_cookies,
    "requests_html": strategy_requests_html,
    "playwright_stealth": strategy_playwright_stealth,
}


def run_strategy(strategy_name: str, screen_name: str, cookies: dict[str, str]) -> list[Tweet]:
    fn = STRATEGY_MAP.get(strategy_name)
    if not fn:
        log(f"  [!] Unknown strategy: {strategy_name}")
        return []
    return fn(screen_name, cookies)


# ═══════════════════════════════════════════════════════════════
# MAIN LOGIC
# ═══════════════════════════════════════════════════════════════
def process_account(screen_name: str, cookies: dict[str, str], state: AdaptiveState) -> bool:
    acc = state.get_or_create(screen_name)
    acc.last_run = datetime.now(timezone.utc).isoformat()

    # Pick strategy
    strategy = state.pick_strategy(acc)
    acc.last_strategy = strategy
    acc.strategy_history.append(strategy)
    # Keep history manageable
    if len(acc.strategy_history) > 20:
        acc.strategy_history = acc.strategy_history[-20:]

    log(f"\n[@{screen_name}] strategy={strategy} | runs={acc.deep_runs} | progress_streak={acc.consecutive_no_progress} | total={acc.total_collected}")

    # Load existing
    existing = load_existing(screen_name)
    before_count = len(existing)

    # If account marked complete, still do a fast check every 3 runs
    if acc.is_complete and acc.deep_runs % 3 != 0:
        log(f"  [+] Account complete — skipping this run")
        return True

    # Execute strategy
    try:
        new_tweets = run_strategy(strategy, screen_name, cookies)
    except Exception as e:
        log(f"  [!] Strategy {strategy} crashed: {e}")
        acc.error_count += 1
        acc.errors.append(f"{datetime.now(timezone.utc).isoformat()}: {strategy} crashed: {e}")
        state.save()
        return False

    if not new_tweets:
        acc.error_count += 1
        acc.consecutive_no_progress += 1
        log(f"  [!] No tweets from {strategy} (errors={acc.error_count}, no_progress={acc.consecutive_no_progress})")

        # If stuck for too long, force reset and escalate
        if acc.consecutive_no_progress >= 6:
            log(f"  [!!!] ACCOUNT @{screen_name} STUCK for 6 runs — forcing strategy rotation")
            acc.is_complete = False
            # Next run will pick different strategy automatically
        state.save()
        return False

    # Merge
    added = 0
    for t in new_tweets:
        if t.tweet_id not in existing:
            existing[t.tweet_id] = t
            added += 1

    # Evaluate progress
    if added == 0:
        acc.consecutive_no_progress += 1
        log(f"  [!] 0 new tweets added (already had them). no_progress={acc.consecutive_no_progress}")
        # If we got tweets but 0 new, and we already have some, we may be complete
        if len(existing) > 50 and acc.consecutive_no_progress >= 2:
            acc.is_complete = True
            log(f"  [+] Marked COMPLETE (no new tweets, already have {len(existing)})")
    else:
        acc.consecutive_no_progress = 0
        acc.error_count = 0
        acc.deep_runs += 1
        log(f"  [+] Added {added} new tweets")

    acc.total_collected = len(existing)
    if existing:
        sorted_t = sorted(existing.values(), key=lambda x: x.created_at, reverse=True)
        acc.last_tweet_id = sorted_t[0].tweet_id

    # If first success after many failures, celebrate
    if added > 0 and len(acc.strategy_history) > 1 and acc.strategy_history[-2] != strategy:
        log(f"  [***] STRATEGY {strategy} WORKED after trying {acc.strategy_history[-2]}!")

    save_tweets(screen_name, existing)
    state.save()
    log(f"  [+] Before: {before_count} | Added: {added} | Total: {len(existing)}")
    return True


def main():
    def sig_handler(sig, frame):
        log("Interrupted")
        sys.exit(130)
    signal.signal(signal.SIGINT, sig_handler)

    log("=" * 60)
    log(f"Adaptive Cron Collector — {datetime.now(timezone.utc).isoformat()}")
    log(f"Available strategies: {STRATEGIES}")
    log("=" * 60)

    # Verify Firefox profile exists
    if not any([
        os.path.exists("/home/cristian/.config/mozilla/firefox"),
        os.path.exists("/home/cristian/.mozilla/firefox"),
    ]):
        log("[!] Firefox profile not found")
        sys.exit(1)

    # Get cookies
    try:
        cookies = get_cookies()
        missing = [c for c in ["auth_token", "ct0", "twid"] if c not in cookies]
        if missing:
            log(f"[!] Missing critical cookies: {missing}")
            log("[!] Ensure you are logged in to x.com in Firefox")
            sys.exit(1)
    except Exception as e:
        log(f"[!] Cookie extraction failed: {e}")
        sys.exit(1)

    state = AdaptiveState()
    results = {}
    for account in ACCOUNTS:
        try:
            results[account] = process_account(account, cookies, state)
        except Exception as e:
            log(f"[!] Exception @{account}: {e}")
            results[account] = False

    # Summary
    log("\n" + "=" * 60)
    log("SUMMARY")
    log("=" * 60)
    for account, ok in results.items():
        acc = state.accounts.get(account)
        status = "OK" if ok else "FAIL"
        mode = "complete" if (acc and acc.is_complete) else "incomplete"
        last_strat = acc.last_strategy if acc else "none"
        log(f"  @{account:20s} {status} | {mode} | strat={last_strat:20s} | total={acc.total_collected if acc else 0}")
    state.save()

    sys.exit(0 if all(results.values()) else 2)


if __name__ == "__main__":
    main()
