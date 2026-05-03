#!/usr/bin/env python3
"""
Scroll-Screen-Transcribe Scraper
Indetectable by X because it behaves exactly like a human:
- Opens a VISIBLE browser (Firefox/Chrome)
- Scrolls at human speed with random pauses
- Takes screenshots for audit trail
- Extracts tweet data from DOM (not HTTP requests)
- Stops when it reaches the last known tweet

Usage:
    cd ~/projects/x-v2-collector && . venv/bin/activate
    python scripts/scroll_screenscraper.py --account financialjuice
"""

import argparse
import json
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: pip install playwright")
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TWEETS_DIR = DATA_DIR / "tweets"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"

MAX_SCROLLS = 10_000
NO_NEW_THRESHOLD = 15  # Scrolls sin tweets nuevos antes de parar
BATCH_SAVE_EVERY = 50

# Human-like delays (seconds)
MIN_SCROLL_DELAY = 1.0
MAX_SCROLL_DELAY = 3.5
MIN_EXTRACT_DELAY = 0.3
MAX_EXTRACT_DELAY = 1.2


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    (DATA_DIR / "scroll_scraper.log").parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "scroll_scraper.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_cookies() -> dict[str, str]:
    if COOKIE_CACHE.exists():
        try:
            meta = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            return meta.get("cookies", {})
        except Exception:
            pass
    return {}


def parse_tweet_from_cell(cell) -> Optional[dict]:
    """Extract tweet from a Playwright locator using DOM access."""
    try:
        # Get all links to find tweet ID
        links = cell.locator("a[href*='/status/']").all()
        tweet_id = ""
        for link in links:
            href = link.get_attribute("href") or ""
            if "/status/" in href and "/analytics" not in href:
                parts = href.split("/status/")
                if len(parts) > 1:
                    tid = parts[1].split("?")[0].split("/")[0]
                    if tid.isdigit() and len(tid) > 10:
                        tweet_id = tid
                        break
        if not tweet_id:
            return None

        # Text content
        text = ""
        text_els = cell.locator("div[data-testid='tweetText']").all()
        for el in text_els:
            try:
                text += el.inner_text() + " "
            except Exception:
                pass
        text = text.strip()

        # Timestamp
        created_at = ""
        time_els = cell.locator("time").all()
        if time_els:
            dt_str = time_els[0].get_attribute("datetime")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    created_at = dt.strftime("%a %b %d %H:%M:%S +0000 %Y")
                except Exception:
                    created_at = dt_str

        # Engagement via aria-labels
        likes = replies = retweets = 0
        try:
            like_btn = cell.locator("button[data-testid='like']").first
            aria = like_btn.get_attribute("aria-label") or ""
            digits = "".join(c for c in aria if c.isdigit())
            likes = int(digits) if digits else 0
        except Exception:
            pass
        try:
            reply_btn = cell.locator("button[data-testid='reply']").first
            aria = reply_btn.get_attribute("aria-label") or ""
            digits = "".join(c for c in aria if c.isdigit())
            replies = int(digits) if digits else 0
        except Exception:
            pass
        try:
            rt_btn = cell.locator("button[data-testid='retweet']").first
            aria = rt_btn.get_attribute("aria-label") or ""
            digits = "".join(c for c in aria if c.isdigit())
            retweets = int(digits) if digits else 0
        except Exception:
            pass

        # Display name & username
        display_name = ""
        try:
            name_el = cell.locator("div[data-testid='User-Name']").first
            name_text = name_el.inner_text()
            display_name = name_text.split("\n")[0] if "\n" in name_text else name_text[:30]
        except Exception:
            pass

        is_reply = cell.locator("div[data-testid='tweetReplyContext']").count() > 0
        is_retweet = cell.locator("span[data-testid='socialContext']").count() > 0

        return {
            "tweet_id": tweet_id,
            "username": "",
            "display_name": display_name,
            "text": text,
            "created_at": created_at,
            "likes": likes,
            "replies": replies,
            "retweets": retweets,
            "quotes": 0,
            "is_reply": is_reply,
            "is_retweet": is_retweet,
            "source": "scroll_screenscraper",
        }
    except Exception as e:
        return None


def run_scroll_scraper(account: str, max_scrolls: int = MAX_SCROLLS, resume: bool = True):
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    output_file = TWEETS_DIR / f"{account}_all.json"

    # Load existing tweets
    existing: dict[str, dict] = {}
    oldest_known_id = None
    if output_file.exists() and resume:
        try:
            raw = json.loads(output_file.read_text(encoding="utf-8"))
            for t in raw:
                existing[t.get("tweet_id", "")] = t
            # Sort by date to find oldest
            valid = [t for t in raw if t.get("created_at")]
            if valid:
                oldest = min(valid, key=lambda x: x.get("created_at", ""))
                oldest_known_id = oldest.get("tweet_id")
            log(f"📦 Resuming @{account}: {len(existing)} tweets already saved. Oldest known: {oldest_known_id}")
        except Exception:
            pass
    else:
        log(f"🆕 Starting fresh scrape of @{account}")

    cookies = get_cookies()
    log(f"🍪 Cookies: {len(cookies)} entries")

    with sync_playwright() as p:
        # Launch VISIBLE browser (not headless) - key for evasion
        browser = p.firefox.launch(
            headless=False,  # VISIBLE - human-like
            slow_mo=200,     # Slow down operations
            firefox_user_prefs={
                "dom.webnotifications.enabled": False,
                "media.navigator.permission.disabled": True,
            },
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            locale="en-US",
            timezone_id="America/New_York",
        )

        page = context.new_page()

        # Inject cookies first
        page.goto("https://x.com")
        time.sleep(random.uniform(2, 4))

        critical = ["auth_token", "ct0", "twid", "kdt", "gt", "att", "_twpid"]
        for name, value in cookies.items():
            if name in critical:
                try:
                    context.add_cookies([{
                        "name": name,
                        "value": value,
                        "domain": ".x.com",
                        "path": "/",
                    }])
                except Exception:
                    pass
        log("✅ Cookies injected into visible Firefox")

        # Navigate to profile
        url = f"https://x.com/{account}"
        log(f"🌐 Opening {url} in VISIBLE browser...")
        page.goto(url)
        time.sleep(random.uniform(4, 7))

        # Wait for timeline
        try:
            page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
            log("✅ Timeline loaded")
        except PWTimeout:
            log("❌ Timeline failed to load. Taking screenshot for debug...")
            page.screenshot(path=str(SCREENSHOTS_DIR / f"{account}_error.png"))
            browser.close()
            return

        # Take initial screenshot
        screenshot_path = SCREENSHOTS_DIR / f"{account}_start_{datetime.now(timezone.utc).strftime('%H%M%S')}.png"
        page.screenshot(path=str(screenshot_path))
        log(f"📸 Screenshot saved: {screenshot_path.name}")

        # State
        seen = set(existing.keys())
        tweets = dict(existing)
        no_new_streak = 0
        last_save_count = len(tweets)
        reached_known = False
        start_time = time.time()
        scroll_num = 0

        log(f"🚀 Starting scroll loop (max {max_scrolls} scrolls, visible browser)...")
        log(f"   Will stop when reaching tweet_id={oldest_known_id} or {NO_NEW_THRESHOLD} empty scrolls")

        while scroll_num < max_scrolls and not reached_known:
            scroll_num += 1

            # Extract all visible tweets
            cells = page.locator("article[data-testid='tweet']").all()
            new_this_scroll = 0
            oldest_this_scroll = None

            for idx, cell in enumerate(cells):
                tweet = parse_tweet_from_cell(cell)
                if not tweet or not tweet["tweet_id"]:
                    continue

                tid = tweet["tweet_id"]

                # Check if we reached the oldest known tweet
                if oldest_known_id and tid == oldest_known_id:
                    log(f"🎯 REACHED oldest known tweet {tid}! Stopping.")
                    reached_known = True
                    break

                if tid in seen:
                    continue

                tweet["username"] = account
                seen.add(tid)
                tweets[tid] = tweet
                new_this_scroll += 1
                time.sleep(random.uniform(MIN_EXTRACT_DELAY, MAX_EXTRACT_DELAY))

            # Stats
            elapsed = time.time() - start_time
            rate = len(tweets) / (elapsed / 60) if elapsed > 0 else 0
            log(f"  scroll {scroll_num:4d}: +{new_this_scroll:3d} new | total {len(tweets):5d} | rate {rate:.1f}/min | streak={no_new_streak}")

            # Periodic screenshot (every 25 scrolls)
            if scroll_num % 25 == 0:
                ss_path = SCREENSHOTS_DIR / f"{account}_scroll{scroll_num}_{datetime.now(timezone.utc).strftime('%H%M%S')}.png"
                page.screenshot(path=str(ss_path))

            # Save batch
            if len(tweets) - last_save_count >= BATCH_SAVE_EVERY:
                _save_tweets(output_file, tweets)
                last_save_count = len(tweets)
                log(f"  💾 Saved batch ({len(tweets)} total)")

            # Check end conditions
            if new_this_scroll == 0:
                no_new_streak += 1
                if no_new_streak >= NO_NEW_THRESHOLD:
                    log(f"⏹️ No new tweets for {NO_NEW_THRESHOLD} scrolls. End of timeline.")
                    break
            else:
                no_new_streak = 0

            # Human-like scroll
            scroll_amount = random.randint(600, 1200)  # Variable scroll
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            time.sleep(random.uniform(MIN_SCROLL_DELAY, MAX_SCROLL_DELAY))

            # Random extra pause every ~50 scrolls (like a human reading)
            if scroll_num % random.randint(40, 60) == 0:
                pause = random.uniform(5, 12)
                log(f"  ☕ Human pause: {pause:.1f}s")
                time.sleep(pause)

        # Final save
        _save_tweets(output_file, tweets)
        _save_last4years(account, tweets)

        # Final screenshot
        final_ss = SCREENSHOTS_DIR / f"{account}_final_{datetime.now(timezone.utc).strftime('%H%M%S')}.png"
        page.screenshot(path=str(final_ss))

        log(f"✅ DONE! @{account}: {len(tweets)} total tweets | {len(tweets) - len(existing)} new | {scroll_num} scrolls")
        browser.close()


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
    log(f"📅 Last 4 years file: {len(recent)} tweets")


def main():
    parser = argparse.ArgumentParser(description="Human-like scroll+screenshot scraper for X")
    parser.add_argument("--account", "-a", required=True, help="Account to scrape (without @)")
    parser.add_argument("--max-scrolls", "-m", type=int, default=MAX_SCROLLS)
    parser.add_argument("--fresh", "-f", action="store_true", help="Ignore existing data, start fresh")
    args = parser.parse_args()

    def handler(sig, frame):
        print("\n⚠️ INTERRUPTED! Saving progress...")
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

    run_scroll_scraper(args.account, max_scrolls=args.max_scrolls, resume=not args.fresh)


if __name__ == "__main__":
    main()
