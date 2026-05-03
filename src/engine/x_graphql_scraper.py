"""
X GraphQL Interceptor Scraper — Zero API, Zero Cookies, Zero Login Required
===========================================================================
Intercepta los responses GraphQL que X envía a su SPA interno.
Los timelines públicos (financialjuice, Deltaone) no requieren autenticación.
Estrategia: Playwright + Firefox → navegar → interceptar api.x.com/graphql → parsear JSON

Config vía env vars:
  X_SCRAPER_SCROLLS    — scrolls por target (default: 5)
  X_SCRAPER_PAGE_WAIT  — segundos espera carga (default: 5)
  X_SCRAPER_POST_WAIT  — segundos después de scroll (default: 3)
"""

import json
import time
import random
import os
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from playwright.sync_api import sync_playwright


# ── Configuration ──────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent.parent / "data" / "tweets" / "graphql"
DATA_DIR.mkdir(parents=True, exist_ok=True)

TARGETS = ["financialjuice", "Deltaone"]

# Anti-detection
SCROLL_AMOUNT = 2500
MAX_SCROLLS = int(os.environ.get("X_SCRAPER_SCROLLS", "5"))
PAGE_WAIT = int(os.environ.get("X_SCRAPER_PAGE_WAIT", "5"))
POST_SCROLL_WAIT = int(os.environ.get("X_SCRAPER_POST_WAIT", "3"))
MIN_SCROLL_DELAY = 2.0
MAX_SCROLL_DELAY = 4.0


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
    is_pinned: bool = False
    source: str = "graphql_interceptor"


def _parse_engagement(legacy: dict, key: str) -> int:
    val = legacy.get(key)
    if isinstance(val, (int, float, str)):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0
    return 0


def _extract_tweets_from_entry(entry: dict, default_username: str, is_pinned: bool = False) -> Optional[Tweet]:
    try:
        content = entry.get("content", {})
        item = content.get("itemContent", {})
        if item.get("itemType") != "TimelineTweet":
            return None

        tweet_result = item.get("tweet_results", {}).get("result", {})
        if not isinstance(tweet_result, dict):
            return None

        __typename = tweet_result.get("__typename", "")
        if __typename == "TweetWithVisibilityResults":
            tweet_result = tweet_result.get("tweet", {}).get("result", {})
            if not isinstance(tweet_result, dict):
                return None

        legacy = tweet_result.get("legacy", {})
        tweet_id = str(legacy.get("id_str", ""))
        if not tweet_id:
            return None

        core = tweet_result.get("core", {})
        user_result = core.get("user_results", {}).get("result", {})
        if not isinstance(user_result, dict):
            user_result = {}
        user_legacy = user_result.get("legacy", {}) if isinstance(user_result, dict) else {}
        username = user_legacy.get("screen_name", default_username)
        display_name = user_legacy.get("name", "")

        text = legacy.get("full_text", "")
        created_at = legacy.get("created_at", "")

        likes = _parse_engagement(legacy, "favorite_count")
        replies = _parse_engagement(legacy, "reply_count")
        retweets = _parse_engagement(legacy, "retweet_count")
        quotes = _parse_engagement(legacy, "quote_count")

        is_reply = bool(legacy.get("in_reply_to_status_id_str"))
        is_retweet = bool(legacy.get("retweeted_status_result"))

        return Tweet(
            tweet_id=tweet_id,
            username=username,
            display_name=display_name,
            text=text,
            created_at=created_at,
            likes=likes,
            replies=replies,
            retweets=retweets,
            quotes=quotes,
            is_reply=is_reply,
            is_retweet=is_retweet,
            is_pinned=is_pinned,
        )
    except Exception:
        return None


def _extract_tweets_from_response(data: dict, screen_name: str) -> list[Tweet]:
    tweets: list[Tweet] = []
    seen_ids = set()

    result = data.get("data", {}).get("user", {}).get("result", {})
    if not isinstance(result, dict):
        return tweets

    timeline_inner = result.get("timeline", {}).get("timeline", {})
    instructions = timeline_inner.get("instructions", [])

    for inst in instructions:
        inst_type = inst.get("type", "")

        if inst_type == "TimelineAddEntries":
            entries = inst.get("entries", [])
            for entry in entries:
                tweet = _extract_tweets_from_entry(entry, screen_name)
                if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                    seen_ids.add(tweet.tweet_id)
                    tweets.append(tweet)

        elif inst_type == "TimelinePinEntry":
            entry = inst.get("entry", {})
            tweet = _extract_tweets_from_entry(entry, screen_name, is_pinned=True)
            if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                seen_ids.add(tweet.tweet_id)
                tweets.append(tweet)

    return tweets


def _load_existing(screen_name: str) -> dict[str, Tweet]:
    path = DATA_DIR / f"{screen_name}_all.json"
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


def _save_tweets(screen_name: str, tweets: dict[str, Tweet]) -> Path:
    path = DATA_DIR / f"{screen_name}_all.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in tweets.values()], f, ensure_ascii=False, indent=2)
    return path


def scrape_account(screen_name: str, headless: bool = True, max_scrolls: int = None) -> tuple[int, int]:
    if max_scrolls is None:
        max_scrolls = MAX_SCROLLS

    print(f"\n[▶] Scraping @{screen_name} via GraphQL interception")
    all_tweets: list[Tweet] = []

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()

        def on_response(response):
            url = response.url
            if "api.x.com/graphql" in url and "UserTweets" in url:
                try:
                    data = response.json()
                    tweets = _extract_tweets_from_response(data, screen_name)
                    if tweets:
                        all_tweets.extend(tweets)
                        print(f"  [✓] Intercepted {len(tweets)} tweets from GraphQL")
                except Exception:
                    pass

        page.on("response", on_response)

        url = f"https://x.com/{screen_name}"
        print(f"  [→] Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(PAGE_WAIT)

        for scroll_num in range(1, max_scrolls + 1):
            page.evaluate(f"window.scrollBy(0, {SCROLL_AMOUNT})")
            delay = random.uniform(MIN_SCROLL_DELAY, MAX_SCROLL_DELAY)
            time.sleep(delay)
            time.sleep(POST_SCROLL_WAIT)

            if scroll_num % 3 == 0 or scroll_num == max_scrolls:
                print(f"    Scroll {scroll_num}/{max_scrolls} | intercepted: {len(all_tweets)}")

        browser.close()

    existing = _load_existing(screen_name)
    before = len(existing)

    for t in all_tweets:
        if t.tweet_id not in existing:
            existing[t.tweet_id] = t

    after = len(existing)
    added = after - before

    if after > 0:
        _save_tweets(screen_name, existing)

    print(f"  [✓] Found: {len(all_tweets)} | Before: {before} | New: {added} | Total: {after}")
    return len(all_tweets), added


def scrape_with_retry(screen_name: str, max_retries: int = 3) -> tuple[int, int]:
    for attempt in range(1, max_retries + 1):
        try:
            print(f"\n[Attempt {attempt}/{max_retries}] @{screen_name}")
            found, added = scrape_account(screen_name)
            if found > 0:
                return found, added
            print(f"  [!] No tweets found, retrying...")
        except Exception as e:
            print(f"  [!] Error: {e}")
            time.sleep(random.uniform(5, 10))

    print(f"  [✗] Failed after {max_retries} attempts")
    return 0, 0


def run_all_targets() -> dict[str, dict]:
    results = {}
    print("=" * 60)
    print(f"X GraphQL Interceptor — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    for target in TARGETS:
        found, added = scrape_with_retry(target)
        results[target] = {
            "found": found,
            "added": added,
            "success": found > 0,
        }
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_found = sum(r["found"] for r in results.values())
    total_added = sum(r["added"] for r in results.values())
    for target, r in results.items():
        status = "✓" if r["success"] else "✗"
        print(f"  {status} @{target}: found={r['found']} new={r['added']}")
    print(f"  TOTAL: found={total_found} new={total_added}")

    return results


if __name__ == "__main__":
    run_all_targets()
