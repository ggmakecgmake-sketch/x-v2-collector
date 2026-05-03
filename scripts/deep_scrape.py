#!/usr/bin/env python3
"""
Deep Historical Scraper — Captures ALL tweets from an X account via Selenium scroll.

Usage:
    PYTHONPATH=src python scripts/deep_scrape.py --account financialjuice --years 4
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    print("ERROR: selenium no instalado. pip install selenium webdriver-manager")
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TWEETS_DIR = DATA_DIR / "tweets"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"
REQUEST_TIMEOUT = 30
MAX_SCROLLS = 5000  # Aumentado para 4 años de historia
NO_NEW_THRESHOLD = 20  # Más tolerante al inicio
BATCH_SAVE_EVERY = 100  # Guardar cada 100 scrolls


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def get_cookies() -> dict[str, str]:
    """Load cookies from x-v2-collector cache or Firefox."""
    if COOKIE_CACHE.exists():
        try:
            meta = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            return meta.get("cookies", {})
        except Exception:
            pass
    # Fallback: extract directly from Firefox
    import sqlite3, shutil, tempfile, glob
    profiles = [
        str(Path.home() / ".config/mozilla/firefox/*.default-release*"),
        str(Path.home() / ".mozilla/firefox/*.default-release*"),
    ]
    candidates = []
    for pattern in profiles:
        for m in glob.glob(pattern):
            p = Path(m)
            if (p / "cookies.sqlite").exists():
                candidates.append(p)
    if not candidates:
        raise RuntimeError("No Firefox profile found")
    candidates.sort(key=lambda p: (p / "cookies.sqlite").stat().st_mtime, reverse=True)
    profile = candidates[0]
    db_tmp = Path(tempfile.gettempdir()) / f"ff_deep_{int(time.time())}.sqlite"
    shutil.copy2(profile / "cookies.sqlite", db_tmp)
    wal = profile / "cookies.sqlite-wal"
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


def parse_tweet_from_article(article) -> Optional[dict]:
    """Extract tweet data from a Selenium WebElement article."""
    try:
        # Tweet ID from link
        links = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
        tweet_id = ""
        for link in links:
            href = link.get_attribute("href") or ""
            if "/status/" in href:
                parts = href.split("/status/")
                if len(parts) > 1:
                    tweet_id = parts[1].split("?")[0].split("/")[0]
                    break
        if not tweet_id or not tweet_id.isdigit():
            return None

        # Text
        text_elements = article.find_elements(By.CSS_SELECTOR, "div[data-testid='tweetText']")
        full_text = " ".join(el.text for el in text_elements if el.text)

        # Timestamp from time element
        time_elements = article.find_elements(By.TAG_NAME, "time")
        created_at = ""
        if time_elements:
            dt_str = time_elements[0].get_attribute("datetime")
            if dt_str:
                # Convert ISO 8601 to Twitter format
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    created_at = dt.strftime("%a %b %d %H:%M:%S +0000 %Y")
                except Exception:
                    created_at = dt_str

        # Engagement counts
        likes = 0
        replies = 0
        retweets = 0
        try:
            like_el = article.find_element(By.CSS_SELECTOR, "button[data-testid='like']")
            likes_txt = like_el.get_attribute("aria-label") or ""
            likes = int("".join(c for c in likes_txt if c.isdigit())) if any(c.isdigit() for c in likes_txt) else 0
        except Exception:
            pass
        try:
            reply_el = article.find_element(By.CSS_SELECTOR, "button[data-testid='reply']")
            replies_txt = reply_el.get_attribute("aria-label") or ""
            replies = int("".join(c for c in replies_txt if c.isdigit())) if any(c.isdigit() for c in replies_txt) else 0
        except Exception:
            pass
        try:
            rt_el = article.find_element(By.CSS_SELECTOR, "button[data-testid='retweet']")
            rt_txt = rt_el.get_attribute("aria-label") or ""
            retweets = int("".join(c for c in rt_txt if c.isdigit())) if any(c.isdigit() for c in rt_txt) else 0
        except Exception:
            pass

        # Display name
        display_name = ""
        try:
            name_el = article.find_element(By.CSS_SELECTOR, "div[data-testid='User-Name'] a")
            display_name = name_el.text.split("\n")[0]
        except Exception:
            pass

        # Is reply?
        is_reply = bool(article.find_elements(By.CSS_SELECTOR, "div[data-testid='tweetReplyContext']"))
        # Is retweet?
        is_retweet = bool(article.find_elements(By.CSS_SELECTOR, "span[data-testid='socialContext']"))

        return {
            "tweet_id": tweet_id,
            "username": "",
            "display_name": display_name,
            "text": full_text,
            "created_at": created_at,
            "likes": likes,
            "replies": replies,
            "retweets": retweets,
            "quotes": 0,
            "is_reply": is_reply,
            "is_retweet": is_retweet,
            "source": "selenium_deep",
        }
    except Exception as e:
        return None


def run_deep_scrape(account: str, years: int = 4, max_scrolls: int = MAX_SCROLLS):
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    output_file = TWEETS_DIR / f"{account}_all.json"

    # Load existing tweets
    existing: dict[str, dict] = {}
    if output_file.exists():
        try:
            raw = json.loads(output_file.read_text(encoding="utf-8"))
            for t in raw:
                existing[t.get("tweet_id", "")] = t
            log(f"Loaded {len(existing)} existing tweets for @{account}")
        except Exception:
            pass

    cookies = get_cookies()
    log(f"Cookies loaded: {len(cookies)} entries")

    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    driver = None
    try:
        try:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        except Exception:
            driver = webdriver.Chrome(options=opts)

        # Inject cookies
        driver.get("https://x.com")
        time.sleep(2)
        critical = ["auth_token", "ct0", "twid", "kdt", "gt", "att", "_twpid"]
        for name, value in cookies.items():
            if name in critical:
                try:
                    driver.add_cookie({"name": name, "value": value, "domain": ".x.com", "path": "/"})
                except Exception:
                    pass

        # Navigate to profile
        driver.get(f"https://x.com/{account}")
        time.sleep(4)

        # Wait for timeline
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-testid='tweet']"))
            )
        except Exception:
            log(f"Warning: Timeline load timeout. Proceeding anyway...")

        log(f"Scrolling @{account} timeline (target: ~{years} years, max {max_scrolls} scrolls)...")

        seen_ids = set(existing.keys())
        tweets = dict(existing)
        no_new = 0
        last_save_count = len(tweets)
        start_time = time.time()

        for scroll in range(1, max_scrolls + 1):
            articles = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            new_this = 0
            for art in articles:
                tweet = parse_tweet_from_article(art)
                if tweet and tweet["tweet_id"] and tweet["tweet_id"] not in seen_ids:
                    tweet["username"] = account
                    seen_ids.add(tweet["tweet_id"])
                    tweets[tweet["tweet_id"]] = tweet
                    new_this += 1

            elapsed = time.time() - start_time
            rate = len(tweets) / (elapsed / 60) if elapsed > 0 else 0
            log(f"  scroll {scroll:4d}: +{new_this:3d} new | total {len(tweets):5d} | rate {rate:.0f}/min | streak={no_new}")

            # Periodic save
            if len(tweets) - last_save_count >= BATCH_SAVE_EVERY:
                _save_tweets(output_file, tweets)
                last_save_count = len(tweets)
                log(f"  💾 Saved batch ({len(tweets)} total)")

            if new_this == 0:
                no_new += 1
                if no_new >= NO_NEW_THRESHOLD:
                    log(f"No new tweets for {NO_NEW_THRESHOLD} scrolls. End of timeline reached.")
                    break
            else:
                no_new = 0

            # Scroll
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5 + (scroll % 3) * 0.3)  # Variable delay

        # Final save
        _save_tweets(output_file, tweets)
        _save_last4years(account, tweets)
        log(f"✅ Done! @{account}: {len(tweets)} total tweets saved")

    finally:
        if driver:
            driver.quit()


def _save_tweets(path: Path, tweets: dict):
    sorted_tweets = sorted(tweets.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted_tweets, f, ensure_ascii=False, indent=2)


def _save_last4years(account: str, tweets: dict):
    cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
    recent = []
    for t in tweets.values():
        try:
            dt = datetime.strptime(t["created_at"], "%a %b %d %H:%M:%S +0000 %Y")
            if dt.replace(tzinfo=timezone.utc) >= cutoff:
                recent.append(t)
        except Exception:
            continue
    path = TWEETS_DIR / f"{account}_last4years.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(recent, key=lambda x: x.get("created_at", ""), reverse=True), f, ensure_ascii=False, indent=2)
    log(f"  📅 Last 4 years: {len(recent)} tweets")


def main():
    parser = argparse.ArgumentParser(description="Deep historical X scraper")
    parser.add_argument("--account", "-a", required=True, help="X account to scrape (without @)")
    parser.add_argument("--years", "-y", type=int, default=4, help="Years of history to target")
    parser.add_argument("--max-scrolls", "-m", type=int, default=MAX_SCROLLS, help="Max scrolls")
    args = parser.parse_args()

    def signal_handler(sig, frame):
        print("\n⚠️ Interrupted! Progress saved. Exiting...")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)

    run_deep_scrape(args.account, years=args.years, max_scrolls=args.max_scrolls)


if __name__ == "__main__":
    main()
