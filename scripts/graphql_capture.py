#!/usr/bin/env python3
"""
GraphQL Response Capture — Intercepts X's own API responses via page.on('response').
Indetectable: no extra requests, just listens to what the browser already fetches.
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
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: pip install playwright")
    sys.exit(1)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TWEETS_DIR = DATA_DIR / "tweets"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"

MAX_SCROLLS = 10_000
NO_NEW_THRESHOLD = 15
BATCH_SAVE = 100


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(DATA_DIR / "graphql_capture.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_cookies() -> dict[str, str]:
    if COOKIE_CACHE.exists():
        try:
            meta = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            return meta.get("cookies", {})
        except Exception:
            pass
    return {}


def parse_tweets_from_response(data: dict, account: str) -> list[dict]:
    tweets = []
    try:
        timeline = data
        for key in ["data", "user", "result", "timeline_v2", "timeline"]:
            if isinstance(timeline, dict) and key in timeline:
                timeline = timeline[key]
            else:
                return tweets

        if not isinstance(timeline, dict):
            return tweets

        instructions = timeline.get("instructions", [])
        if not isinstance(instructions, list):
            return tweets

        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            entries = instruction.get("entries", [])
            if not isinstance(entries, list):
                continue

            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content", {})
                item_content = content.get("itemContent", {})
                tweet_result = item_content.get("tweet_results", {}).get("result", {})

                if not tweet_result:
                    continue

                if "tweet" in tweet_result:
                    tweet_result = tweet_result["tweet"]

                legacy = tweet_result.get("legacy", {})
                user_data = tweet_result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})

                tid = str(legacy.get("id_str", legacy.get("id", "")))
                if not tid or tid == "0":
                    continue

                created_at = legacy.get("created_at", "")
                text = legacy.get("full_text", legacy.get("text", ""))
                likes = legacy.get("favorite_count", 0)
                replies = legacy.get("reply_count", 0)
                retweets = legacy.get("retweet_count", 0)
                quotes = legacy.get("quote_count", 0)
                is_reply = bool(legacy.get("in_reply_to_status_id_str"))
                is_retweet = bool(legacy.get("retweeted_status_result"))

                tweets.append({
                    "tweet_id": tid,
                    "username": user_data.get("screen_name", account),
                    "display_name": user_data.get("name", ""),
                    "text": text,
                    "created_at": created_at,
                    "likes": likes,
                    "replies": replies,
                    "retweets": retweets,
                    "quotes": quotes,
                    "is_reply": is_reply,
                    "is_retweet": is_retweet,
                    "source": "graphql_capture",
                })
    except Exception as e:
        log(f"  Parse error: {e}")
    return tweets


def run_capture(account: str, max_scrolls: int = MAX_SCROLLS):
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    output_file = TWEETS_DIR / f"{account}_all.json"

    existing: dict[str, dict] = {}
    oldest_known_id = None
    if output_file.exists():
        try:
            raw = json.loads(output_file.read_text(encoding="utf-8"))
            for t in raw:
                existing[t.get("tweet_id", "")] = t
            valid = [t for t in raw if t.get("created_at")]
            if valid:
                oldest = min(valid, key=lambda x: x.get("created_at", ""))
                oldest_known_id = oldest.get("tweet_id")
            log(f"📦 Resuming @{account}: {len(existing)} tweets. Oldest: {oldest_known_id}")
        except Exception:
            pass
    else:
        log(f"🆕 Fresh scrape @{account}")

    cookies = get_cookies()
    log(f"🍪 Cookies: {len(cookies)}")

    seen = set(existing.keys())
    tweets = dict(existing)
    last_save = len(existing)
    no_new_streak = 0
    reached_oldest = False
    total_new_this_scroll = 0

    def on_response(response):
        nonlocal total_new_this_scroll, reached_oldest
        url = response.url
        if "api.x.com/graphql" not in url and "x.com/i/api/graphql" not in url:
            return
        if "UserTweets" not in url and "Timeline" not in url and "Home" not in url:
            return

        try:
            data = response.json()
            new_tweets = parse_tweets_from_response(data, account)
            for t in new_tweets:
                tid = t["tweet_id"]
                if oldest_known_id and tid == oldest_known_id:
                    log(f"🎯 REACHED oldest known tweet {tid}")
                    reached_oldest = True
                    return
                if tid not in seen:
                    seen.add(tid)
                    tweets[tid] = t
                    total_new_this_scroll += 1
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,  # headless to run in background
            slow_mo=200,
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            locale="en-US",
        )

        # Inject cookies before creating page
        critical = ["auth_token", "ct0", "twid", "kdt", "gt", "att", "_twpid"]
        for name, value in cookies.items():
            if name in critical:
                try:
                    context.add_cookies([{
                        "name": name, "value": value, "domain": ".x.com", "path": "/",
                    }])
                except Exception:
                    pass

        page = context.new_page()
        page.on("response", on_response)

        log(f"🌐 Opening https://x.com/{account}...")
        page.goto(f"https://x.com/{account}")
        time.sleep(random.uniform(5, 8))

        try:
            page.wait_for_selector("article[data-testid='tweet']", timeout=25000)
            log("✅ Timeline loaded")
        except Exception:
            log("❌ Timeline timeout")
            browser.close()
            return

        log(f"🚀 Scroll loop (max {max_scrolls}, listening to GraphQL)...")
        log(f"   Will stop when reaching tweet_id={oldest_known_id} or {NO_NEW_THRESHOLD} empty scrolls")
        start_time = time.time()

        for scroll_num in range(1, max_scrolls + 1):
            if reached_oldest:
                break

            total_new_this_scroll = 0

            # Human-like scroll
            scroll_amount = random.randint(500, 1000)
            page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            time.sleep(random.uniform(1.5, 3.5))

            # Wait for lazy loading + GraphQL response
            time.sleep(random.uniform(2, 4))

            new_this = total_new_this_scroll
            elapsed = time.time() - start_time
            rate = len(tweets) / (elapsed / 60) if elapsed > 0 else 0
            log(f"  scroll {scroll_num:4d}: +{new_this:3d} new | total {len(tweets):5d} | rate {rate:.1f}/min | streak={no_new_streak}")

            # Batch save
            if len(tweets) - last_save >= BATCH_SAVE:
                _save(output_file, tweets)
                last_save = len(tweets)
                log(f"  💾 Saved ({len(tweets)} total)")

            # End conditions
            if new_this == 0:
                no_new_streak += 1
                if no_new_streak >= NO_NEW_THRESHOLD:
                    log(f"⏹️ No new tweets for {NO_NEW_THRESHOLD} scrolls")
                    break
            else:
                no_new_streak = 0

            # Random pause
            if scroll_num % random.randint(50, 70) == 0:
                pause = random.uniform(5, 10)
                log(f"  ☕ Pause {pause:.1f}s")
                time.sleep(pause)

        # Final save
        _save(output_file, tweets)
        _save_last4years(account, tweets)
        log(f"✅ DONE! @{account}: {len(tweets)} tweets | {len(tweets) - len(existing)} new")
        browser.close()


def _save(path: Path, tweets: dict):
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
    log(f"📅 Last 4y: {len(recent)} tweets")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", "-a", required=True)
    parser.add_argument("--max-scrolls", "-m", type=int, default=MAX_SCROLLS)
    args = parser.parse_args()

    def handler(sig, frame):
        print("\n⚠️ Interrupted!")
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

    run_capture(args.account, max_scrolls=args.max_scrolls)


if __name__ == "__main__":
    main()
