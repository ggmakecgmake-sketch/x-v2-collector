#!/usr/bin/env python3
"""
Playwright Deep Scraper — Captures historical tweets via scroll.
Uses stealth techniques to avoid detection. Outputs to x-v2-collector format.
"""

import argparse
import json
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
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"

MAX_SCROLLS = 5000
NO_NEW_THRESHOLD = 30
BATCH_SAVE = 100


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def get_cookies() -> dict[str, str]:
    if COOKIE_CACHE.exists():
        try:
            meta = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            return meta.get("cookies", {})
        except Exception:
            pass
    return {}


def parse_tweet(cell) -> Optional[dict]:
    """Parse a tweet cell from the timeline."""
    try:
        # Tweet ID from link
        links = cell.locator("a[href*='/status/']").all()
        tweet_id = ""
        for link in links:
            href = link.get_attribute("href") or ""
            if "/status/" in href:
                parts = href.split("/status/")
                if len(parts) > 1:
                    tid = parts[1].split("?")[0].split("/")[0]
                    if tid.isdigit():
                        tweet_id = tid
                        break
        if not tweet_id:
            return None

        # Text
        text_els = cell.locator("div[data-testid='tweetText']").all()
        full_text = " ".join(el.inner_text() for el in text_els if el.inner_text())

        # Timestamp
        time_els = cell.locator("time").all()
        created_at = ""
        if time_els:
            dt_str = time_els[0].get_attribute("datetime")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    created_at = dt.strftime("%a %b %d %H:%M:%S +0000 %Y")
                except Exception:
                    created_at = dt_str

        # Engagement
        likes = 0
        replies = 0
        retweets = 0
        try:
            like_btn = cell.locator("button[data-testid='like']").first
            aria = like_btn.get_attribute("aria-label") or ""
            likes = int("".join(c for c in aria if c.isdigit())) if any(c.isdigit() for c in aria) else 0
        except Exception:
            pass
        try:
            reply_btn = cell.locator("button[data-testid='reply']").first
            aria = reply_btn.get_attribute("aria-label") or ""
            replies = int("".join(c for c in aria if c.isdigit())) if any(c.isdigit() for c in aria) else 0
        except Exception:
            pass
        try:
            rt_btn = cell.locator("button[data-testid='retweet']").first
            aria = rt_btn.get_attribute("aria-label") or ""
            retweets = int("".join(c for c in aria if c.isdigit())) if any(c.isdigit() for c in aria) else 0
        except Exception:
            pass

        # Display name
        display_name = ""
        try:
            name_el = cell.locator("div[data-testid='User-Name'] a").first
            display_name = name_el.inner_text().split("\n")[0]
        except Exception:
            pass

        is_reply = cell.locator("div[data-testid='tweetReplyContext']").count() > 0
        is_retweet = cell.locator("span[data-testid='socialContext']").count() > 0

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
            "source": "playwright_deep",
        }
    except Exception:
        return None


def run_playwright_scrape(account: str, max_scrolls: int = MAX_SCROLLS):
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    output_file = TWEETS_DIR / f"{account}_all.json"

    # Load existing
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

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        # Inject cookies
        page = context.new_page()
        page.goto("https://x.com")
        time.sleep(2)
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

        # Navigate to profile
        log(f"Navigating to https://x.com/{account}...")
        page.goto(f"https://x.com/{account}")
        time.sleep(4)

        # Wait for tweets
        try:
            page.wait_for_selector("article[data-testid='tweet']", timeout=15000)
        except PWTimeout:
            log("Warning: Timeline timeout. Saving and exiting.")
            browser.close()
            _save(output_file, existing)
            return

        log(f"Scrolling @{account} timeline (max {max_scrolls} scrolls)...")

        seen = set(existing.keys())
        tweets = dict(existing)
        no_new = 0
        last_save = len(tweets)
        start = time.time()

        for scroll in range(1, max_scrolls + 1):
            cells = page.locator("article[data-testid='tweet']").all()
            new_this = 0
            for cell in cells:
                t = parse_tweet(cell)
                if t and t["tweet_id"] and t["tweet_id"] not in seen:
                    t["username"] = account
                    seen.add(t["tweet_id"])
                    tweets[t["tweet_id"]] = t
                    new_this += 1

            elapsed = time.time() - start
            rate = len(tweets) / (elapsed / 60) if elapsed > 0 else 0
            log(f"  scroll {scroll:4d}: +{new_this:3d} new | total {len(tweets):5d} | rate {rate:.0f}/min | streak={no_new}")

            if len(tweets) - last_save >= BATCH_SAVE:
                _save(output_file, tweets)
                last_save = len(tweets)
                log(f"  💾 Saved ({len(tweets)} total)")

            if new_this == 0:
                no_new += 1
                if no_new >= NO_NEW_THRESHOLD:
                    log(f"No new tweets for {NO_NEW_THRESHOLD} scrolls. End of timeline.")
                    break
            else:
                no_new = 0

            # Scroll
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5 + (scroll % 3) * 0.3)

        _save(output_file, tweets)
        log(f"✅ Done! @{account}: {len(tweets)} total tweets")
        browser.close()


def _save(path: Path, tweets: dict):
    sorted_tweets = sorted(tweets.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted_tweets, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", "-a", required=True)
    parser.add_argument("--max-scrolls", "-m", type=int, default=MAX_SCROLLS)
    args = parser.parse_args()

    def handler(sig, frame):
        print("\n⚠️ Interrupted! Progress saved.")
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

    run_playwright_scrape(args.account, max_scrolls=args.max_scrolls)


if __name__ == "__main__":
    main()
