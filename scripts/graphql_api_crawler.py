#!/usr/bin/env python3
"""
GraphQL API Crawler — Calls X's internal GraphQL API directly with Firefox cookies.
This is EXACTLY what the browser does when scrolling. Indetectable.
Paginates backwards in time using cursor tokens.

Usage:
    cd ~/projects/x-v2-collector && . venv/bin/activate
    python scripts/graphql_api_crawler.py --account financialjuice
"""

import argparse
import json
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TWEETS_DIR = DATA_DIR / "tweets"
COOKIE_CACHE = DATA_DIR / "cookies_cache.json"

BATCH_SIZE = 20  # Matches X's own batch size
MAX_BATCHES = 5000  # 5000 * 20 = 100k tweets max
NO_NEW_THRESHOLD = 3


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(DATA_DIR / "graphql_api_crawler.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_cookies() -> dict[str, str]:
    if COOKIE_CACHE.exists():
        try:
            meta = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            return meta.get("cookies", {})
        except Exception:
            pass
    return {}


def get_user_id(screen_name: str, cookies: dict, headers: dict) -> Optional[str]:
    """Get numeric user ID from screen name."""
    url = f"https://api.x.com/graphql/G3CXAL7z3vVOF7-KGk-1Mg/UserByScreenName"
    variables = json.dumps({"screen_name": screen_name})
    features = json.dumps({
        "hidden_profile_subscriptions_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "responsive_web_twitter_article_notes_tab_enabled": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    })
    params = {"variables": variables, "features": features}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            user_id = data.get("data", {}).get("user", {}).get("result", {}).get("rest_id", "")
            if user_id:
                return user_id
    except Exception as e:
        log(f"  User lookup error: {e}")
    return None


def parse_tweets_from_response(data: dict, account: str) -> tuple[list[dict], Optional[str]]:
    """Extract tweets and next cursor from GraphQL response."""
    tweets = []
    next_cursor = None

    try:
        timeline = data.get("data", {}).get("user", {}).get("result", {}).get("timeline_v2", {}).get("timeline", {})
        instructions = timeline.get("instructions", [])

        for instruction in instructions:
            inst_type = instruction.get("type", "")

            if inst_type == "TimelineAddEntries":
                entries = instruction.get("entries", [])
                for entry in entries:
                    content = entry.get("content", {})
                    entry_type = content.get("entryType", "")

                    if entry_type == "TimelineTimelineCursor":
                        cursor_type = content.get("cursorType", "")
                        if cursor_type == "Bottom":
                            next_cursor = content.get("value", "")
                        continue

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

                    tweets.append({
                        "tweet_id": tid,
                        "username": user_data.get("screen_name", account),
                        "display_name": user_data.get("name", ""),
                        "text": legacy.get("full_text", legacy.get("text", "")),
                        "created_at": legacy.get("created_at", ""),
                        "likes": legacy.get("favorite_count", 0),
                        "replies": legacy.get("reply_count", 0),
                        "retweets": legacy.get("retweet_count", 0),
                        "quotes": legacy.get("quote_count", 0),
                        "is_reply": bool(legacy.get("in_reply_to_status_id_str")),
                        "is_retweet": bool(legacy.get("retweeted_status_result")),
                        "source": "graphql_api",
                    })

            elif inst_type == "TimelineReplaceEntry":
                entry = instruction.get("entry", {})
                content = entry.get("content", {})
                if content.get("cursorType", "") == "Bottom":
                    next_cursor = content.get("value", "")

    except Exception as e:
        log(f"  Parse error: {e}")

    return tweets, next_cursor


def run_api_crawler(account: str, max_batches: int = MAX_BATCHES):
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
        log(f"🆕 Fresh crawl @{account}")

    cookies = get_cookies()
    log(f"🍪 Cookies: {len(cookies)}")

    if not cookies:
        log("❌ No cookies found! Please log into x.com in Firefox first.")
        return

    # Build headers exactly like Firefox does
    bearer_token = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": f"https://x.com/{account}",
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "X-Twitter-Client-Language": "en",
        "X-Twitter-Active-User": "yes",
        "Origin": "https://x.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Connection": "keep-alive",
    }

    # Add critical cookies to headers
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers["Cookie"] = cookie_str

    # Get user ID
    log("🔍 Looking up user ID...")
    user_id = get_user_id(account, cookies, headers)
    if not user_id:
        log("❌ Could not get user ID. Check cookies.")
        return
    log(f"✅ User ID: {user_id}")

    # Crawl loop
    seen = set(existing.keys())
    tweets = dict(existing)
    cursor: Optional[str] = None
    no_new_streak = 0
    last_save = len(tweets)
    reached_oldest = False

    for batch in range(1, max_batches + 1):
        if reached_oldest:
            break

        # Build request
        variables = {
            "userId": user_id,
            "count": BATCH_SIZE,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor

        features = {
            "rweb_video_screen_enabled": False,
            "rweb_cashtags_enabled": True,
            "profile_label_improvements_pcf_label_in_post_enabled": True,
            "responsive_web_profile_redirect_enabled": False,
            "rweb_tipjar_consumption_enabled": False,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "tweetypie_unmention_optimization_enabled": True,
            "vibe_api_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": False,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "interactive_text_enabled": True,
            "responsive_web_text_conversations_enabled": False,
            "longform_notetweets_rich_text_read_enabled": False,
            "longform_notetweets_inline_media_enabled": False,
            "responsive_web_enhance_cards_enabled": False,
        }

        field_toggles = {"withArticlePlainText": False}

        url = "https://api.x.com/graphql/Ob0lCmufQqqLTwh_Wck5XA/UserTweets"
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(features),
            "fieldToggles": json.dumps(field_toggles),
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 429:
                log("⏸️ Rate limited. Waiting 60s...")
                time.sleep(60)
                continue
            if resp.status_code != 200:
                log(f"⚠️ HTTP {resp.status_code}: {resp.text[:200]}")
                no_new_streak += 1
                if no_new_streak >= NO_NEW_THRESHOLD:
                    break
                time.sleep(5)
                continue

            data = resp.json()
            new_tweets, next_cursor = parse_tweets_from_response(data, account)

            batch_new = 0
            for t in new_tweets:
                tid = t["tweet_id"]
                if oldest_known_id and tid == oldest_known_id:
                    log(f"🎯 REACHED oldest known tweet {tid}")
                    reached_oldest = True
                    break
                if tid not in seen:
                    seen.add(tid)
                    tweets[tid] = t
                    batch_new += 1

            log(f"  batch {batch:4d}: +{batch_new:3d} new | total {len(tweets):5d} | cursor={next_cursor[:30] if next_cursor else 'NONE'}...")

            if batch_new == 0:
                no_new_streak += 1
                if no_new_streak >= NO_NEW_THRESHOLD:
                    log(f"⏹️ No new tweets for {NO_NEW_THRESHOLD} batches")
                    break
            else:
                no_new_streak = 0

            # Save periodically
            if len(tweets) - last_save >= 100:
                _save(output_file, tweets)
                last_save = len(tweets)
                log(f"  💾 Saved ({len(tweets)} total)")

            # Next cursor
            if not next_cursor:
                log("⏹️ No more cursor. End of timeline.")
                break
            cursor = next_cursor

            # Human-like delay between batches
            time.sleep(random.uniform(2, 5))

            # Longer pause every 50 batches
            if batch % 50 == 0:
                pause = random.uniform(15, 30)
                log(f"  ☕ Long pause {pause:.1f}s")
                time.sleep(pause)

        except Exception as e:
            log(f"  Request error: {e}")
            no_new_streak += 1
            if no_new_streak >= NO_NEW_THRESHOLD:
                break
            time.sleep(5)

    # Final save
    _save(output_file, tweets)
    _save_last4years(account, tweets)
    log(f"✅ DONE! @{account}: {len(tweets)} tweets | {len(tweets) - len(existing)} new | {batch} batches")


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
    log(f"📅 Last 4y file: {len(recent)} tweets")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", "-a", required=True)
    parser.add_argument("--max-batches", "-m", type=int, default=MAX_BATCHES)
    args = parser.parse_args()

    def handler(sig, frame):
        print("\n⚠️ Interrupted!")
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

    run_api_crawler(args.account, max_batches=args.max_batches)


if __name__ == "__main__":
    main()
