#!/usr/bin/env python3
"""
GraphQL Timeline Crawler — Captures X's internal API responses.
Uses Playwright to intercept JSON that X.com fetches while scrolling.
"""

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TWEETS_DIR = DATA_DIR / "tweets"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_cookies():
    if COOKIE_CACHE.exists():
        return json.loads(COOKIE_CACHE.read_text()).get("cookies", {})
    return {}


def parse_tweets(data, account):
    tweets = []
    try:
        timeline = data.get("data", {}).get("user", {}).get("result", {}).get("timeline_v2", {}).get("timeline", {})
        instructions = timeline.get("instructions", [])
        for inst in instructions:
            entries = inst.get("entries", [])
            for entry in entries:
                content = entry.get("content", {})
                item = content.get("itemContent", {})
                result = item.get("tweet_results", {}).get("result", {})
                if not result:
                    continue
                if "tweet" in result:
                    result = result["tweet"]
                legacy = result.get("legacy", {})
                user = result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
                tid = str(legacy.get("id_str", legacy.get("id", "")))
                if not tid or tid == "0":
                    continue
                tweets.append({
                    "tweet_id": tid,
                    "username": user.get("screen_name", account),
                    "display_name": user.get("name", ""),
                    "text": legacy.get("full_text", legacy.get("text", "")),
                    "created_at": legacy.get("created_at", ""),
                    "likes": legacy.get("favorite_count", 0),
                    "replies": legacy.get("reply_count", 0),
                    "retweets": legacy.get("retweet_count", 0),
                    "quotes": legacy.get("quote_count", 0),
                    "is_reply": bool(legacy.get("in_reply_to_status_id_str")),
                    "is_retweet": bool(legacy.get("retweeted_status_result")),
                    "source": "graphql_crawler",
                })
    except Exception as e:
        log(f"Parse error: {e}")
    return tweets


def find_cursor(data):
    try:
        timeline = data.get("data", {}).get("user", {}).get("result", {}).get("timeline_v2", {}).get("timeline", {})
        instructions = timeline.get("instructions", [])
        for inst in instructions:
            entries = inst.get("entries", [])
            for entry in entries:
                content = entry.get("content", {})
                if content.get("entryType") == "TimelineTimelineCursor" and content.get("cursorType") == "Bottom":
                    return content.get("value")
    except Exception:
        pass
    return None


def run_crawler(account, max_batches=2000):
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = TWEETS_DIR / f"{account}_all.json"

    existing = {}
    if out_file.exists():
        for t in json.loads(out_file.read_text()):
            existing[t.get("tweet_id", "")] = t
        log(f"Resuming: {len(existing)} tweets")

    cookies = get_cookies()
    log(f"Cookies: {len(cookies)}")

    tweets = dict(existing)
    seen = set(existing.keys())
    last_save = len(tweets)
    no_new = 0

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True, slow_mo=200)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
        )

        for name, val in cookies.items():
            try:
                ctx.add_cookies([{"name": name, "value": val, "domain": ".x.com", "path": "/"}])
            except Exception:
                pass

        page = ctx.new_page()

        # Track all GraphQL responses
        def on_response(resp):
            url = resp.url
            if "api.x.com/graphql" in url and "UserTweets" in url:
                try:
                    data = resp.json()
                    new_tweets = parse_tweets(data, account)
                    cursor = find_cursor(data)
                    batch_new = 0
                    for t in new_tweets:
                        if t["tweet_id"] not in seen:
                            seen.add(t["tweet_id"])
                            tweets[t["tweet_id"]] = t
                            batch_new += 1
                    if batch_new > 0:
                        log(f"  API response: +{batch_new} tweets (cursor: {cursor[:20] if cursor else 'none'}...)")
                except Exception:
                    pass

        page.on("response", on_response)

        # Load timeline
        log(f"Opening x.com/{account}...")
        page.goto(f"https://x.com/{account}")
        time.sleep(random.uniform(5, 8))
        page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
        log("Timeline loaded")

        # Initial wait for first API response
        time.sleep(3)

        log(f"Scrolling (max {max_batches} batches)...")
        start = time.time()

        for batch in range(1, max_batches + 1):
            prev_count = len(tweets)

            page.evaluate("window.scrollBy(0, window.innerHeight)")
            time.sleep(random.uniform(2, 4))

            # Scroll a bit more to trigger lazy load
            page.evaluate("window.scrollBy(0, 500)")
            time.sleep(random.uniform(1, 3))

            new_this = len(tweets) - prev_count
            elapsed = time.time() - start
            rate = len(tweets) / (elapsed / 60) if elapsed > 0 else 0
            log(f"  batch {batch:4d}: +{new_this:3d} new | total {len(tweets):5d} | rate {rate:.1f}/min | streak={no_new}")

            if len(tweets) - last_save >= 100:
                _save(out_file, tweets)
                last_save = len(tweets)
                log(f"  💾 Saved ({len(tweets)} total)")

            if new_this == 0:
                no_new += 1
                if no_new >= 10:
                    log("No new tweets for 10 batches. Stopping.")
                    break
            else:
                no_new = 0

            if batch % 50 == 0:
                pause = random.uniform(10, 20)
                log(f"  ☕ Long pause {pause:.1f}s")
                time.sleep(pause)

        _save(out_file, tweets)
        log(f"✅ DONE! {len(tweets)} tweets ({len(tweets) - len(existing)} new)")
        browser.close()


def _save(path, tweets):
    sorted_tweets = sorted(tweets.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted_tweets, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", "-a", required=True)
    parser.add_argument("--max-batches", "-m", type=int, default=2000)
    args = parser.parse_args()
    run_crawler(args.account, args.max_batches)
