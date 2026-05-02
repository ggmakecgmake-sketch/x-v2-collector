#!/usr/bin/env python3
"""
Cron Collector for X/Twitter — Selenium + Chrome version
Runs every 5 minutes, extracts cookies from Firefox, scrolls timelines.
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
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict, field

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── Configuration ────────────────────────────────────────────────
ACCOUNTS = ["financialjuice", "Deltaone"]
DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "collection_state.json"
TWEETS_DIR = DATA_DIR / "tweets"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"
COOKIE_CACHE_TTL_SECONDS = 120
MAX_SCROLLS_PER_RUN = 120
FAST_NO_NEW_THRESHOLD = 5
REQUEST_TIMEOUT = 30


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
    source: str = "selenium_chrome"


@dataclass
class AccountState:
    screen_name: str
    is_complete: bool = False
    total_collected: int = 0
    last_tweet_id: str = ""
    last_run: str = ""
    deep_runs: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


class CollectionState:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.accounts: dict[str, AccountState] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for k, v in raw.items():
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


# ── Cookies ───────────────────────────────────────────────────
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
        db_tmp = Path(tempfile.gettempdir()) / f"ff_cron_{int(time.time())}.sqlite"
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
            if age < COOKIE_CACHE_TTL_SECONDS:
                print(f"[+] Using cached cookies ({age:.0f}s old)")
                return meta["cookies"]
        except Exception:
            pass

    extractor = FirefoxCookieExtractor()
    cookies = extractor.get_cookies()
    COOKIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(COOKIE_CACHE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "cookies": cookies}, f)
    print(f"[+] Fresh cookies: {len(cookies)}")
    return cookies


# ── Syndication (fast mode) ────────────────────────────────────
def fast_collect(screen_name: str, cookies: dict[str, str]) -> list[Tweet]:
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
        print(f"  [!] HTTP error: {e}")
        return []
    if resp.status_code == 429:
        print(f"  [!] Rate limited")
        return []
    if resp.status_code != 200:
        print(f"  [!] HTTP {resp.status_code}")
        return []

    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text)
    if not match:
        print(f"  [!] No __NEXT_DATA__")
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        print(f"  [!] JSON parse error")
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
            source="syndication_fast",
        ))
    print(f"  [+] Fast: {len(tweets)} tweets")
    return tweets


# ── Deep mode: Selenium + Chrome ───────────────────────────────
def deep_collect(screen_name: str, cookies: dict[str, str], max_scrolls: int = MAX_SCROLLS_PER_RUN) -> list[Tweet]:
    """Scroll timeline with Selenium + Chrome."""
    print(f"  [+] Starting Chrome deep mode for @{screen_name}")
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
        # Use webdriver-manager if available, otherwise find system chrome
        try:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        except Exception:
            driver = webdriver.Chrome(options=opts)

        # Navigate to x.com first, then inject cookies
        driver.get("https://x.com")
        time.sleep(3)

        # Inject critical cookies (without expiry to avoid issues)
        critical = ["auth_token", "ct0", "twid", "kdt", "gt", "att", "_twpid"]
        for name, value in cookies.items():
            if name in critical:
                try:
                    driver.add_cookie({
                        "name": name,
                        "value": value,
                        "domain": ".x.com",
                        "path": "/",
                    })
                except Exception:
                    pass

        # Now navigate to target profile
        driver.get(f"https://x.com/{screen_name}")
        time.sleep(4)

        # Wait for tweets
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-testid='tweet']"))
            )
            print(f"  [+] Timeline loaded")
        except Exception:
            print(f"  [!] Timeline load timeout — checking anyway...")

        seen_ids = set()
        no_new = 0

        for scroll in range(1, max_scrolls + 1):
            articles = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            new_this = 0

            for art in articles:
                tweet = _parse_article_selenium(art, screen_name)
                if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                    seen_ids.add(tweet.tweet_id)
                    tweets.append(tweet)
                    new_this += 1

            print(f"    Scroll {scroll:3d}: +{new_this:3d} new | total {len(tweets):4d} | streak={no_new}")

            if new_this == 0:
                no_new += 1
                if no_new >= FAST_NO_NEW_THRESHOLD:
                    print(f"  [+] End of timeline")
                    break
            else:
                no_new = 0

            driver.execute_script("window.scrollBy(0, 2500)")
            time.sleep(random.uniform(1.5, 3.0))

        driver.quit()
        driver = None
        print(f"  [+] Deep: {len(tweets)} tweets")
    except Exception as e:
        print(f"  [!] Selenium error: {e}")
        if driver:
            try:
                driver.save_screenshot(str(DATA_DIR / f"debug_{screen_name}.png"))
            except Exception:
                pass
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return tweets


def _parse_article_selenium(article, default_user: str) -> Tweet | None:
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

        likes = _count_selenium(article, "like")
        replies = _count_selenium(article, "reply")
        retweets = _count_selenium(article, "retweet")

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
            source="selenium_deep",
        )
    except Exception:
        return None


def _count_selenium(article, suffix: str) -> int:
    try:
        btn = article.find_element(By.CSS_SELECTOR, f"button[data-testid='{suffix}']")
        label = btn.get_attribute("aria-label") or ""
        nums = re.findall(r"[\d,]+", label)
        if nums:
            return int(nums[0].replace(",", ""))
    except Exception:
        pass
    return 0


# ── Persistence ──────────────────────────────────────────────────
def load_existing(screen_name: str) -> tuple[dict[str, Tweet], Path]:
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
    return tweets, path


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
    print(f"  [+] Saved {len(tweets)} total | {len(recent)} recent")


# ── Main loop ──────────────────────────────────────────────────
def process_account(screen_name: str, cookies: dict[str, str], state: CollectionState) -> bool:
    acc = state.get_or_create(screen_name)
    acc.last_run = datetime.now(timezone.utc).isoformat()
    print(f"\n[@{screen_name}] mode={'FAST' if acc.is_complete else 'DEEP'}")

    existing, _ = load_existing(screen_name)
    before = len(existing)

    if acc.is_complete:
        new = fast_collect(screen_name, cookies)
    else:
        new = deep_collect(screen_name, cookies, max_scrolls=MAX_SCROLLS_PER_RUN)

    if not new:
        acc.error_count += 1
        return False

    added = 0
    for t in new:
        if t.tweet_id not in existing:
            existing[t.tweet_id] = t
            added += 1

    acc.total_collected = len(existing)
    if existing:
        sorted_t = sorted(existing.values(), key=lambda x: x.created_at, reverse=True)
        acc.last_tweet_id = sorted_t[0].tweet_id

    # Mark complete if we got no new tweets and we got some data (already had it all)
    if not acc.is_complete and added == 0 and len(new) > 0:
        acc.is_complete = True
        print(f"  [+] Marked COMPLETE (already had all {len(existing)} tweets)")
    elif not acc.is_complete and len(new) < 10 and added == 0:
        acc.is_complete = True
        print(f"  [+] Marked COMPLETE (small batch, end of timeline)")

    if added > 0:
        acc.error_count = 0
        if not acc.is_complete:
            acc.deep_runs += 1

    save_tweets(screen_name, existing)
    state.save()
    print(f"  [+] Before: {before} | Added: {added} | Total: {len(existing)}")
    return True


def main():
    def sig_handler(sig, frame):
        print("\n[!] Interrupted")
        sys.exit(130)
    signal.signal(signal.SIGINT, sig_handler)

    print("=" * 60)
    print(f"Cron Collector — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    try:
        cookies = get_cookies()
        missing = [c for c in ["auth_token", "ct0", "twid"] if c not in cookies]
        if missing:
            print(f"[!] Missing cookies: {missing}")
            sys.exit(1)
    except Exception as e:
        print(f"[!] Cookie error: {e}")
        sys.exit(1)

    state = CollectionState()
    results = {}
    for account in ACCOUNTS:
        try:
            results[account] = process_account(account, cookies, state)
        except Exception as e:
            print(f"[!] Exception @{account}: {e}")
            results[account] = False

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for account, ok in results.items():
        acc = state.accounts.get(account)
        status = "OK" if ok else "FAIL"
        mode = "complete" if (acc and acc.is_complete) else "incomplete"
        print(f"  @{account:20s} {status} | {mode} | total={acc.total_collected if acc else 0}")
    state.save()

    sys.exit(0 if all(results.values()) else 2)


if __name__ == "__main__":
    main()
