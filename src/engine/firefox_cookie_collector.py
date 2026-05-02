"""
Firefox Cookie Collector for X/Twitter
Extracts auth cookies from running Firefox profile and uses X's internal API
for full timeline extraction with pagination.
"""

import json
import sqlite3
import shutil
import tempfile
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

import requests


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
    source: str = "firefox_api"


class FirefoxCookieExtractor:
    """Extracts X auth cookies from Firefox profile."""

    # Common Firefox profile paths on Linux
    FIREFOX_PROFILES = [
        "~/.mozilla/firefox/*.default*",
        "~/.config/mozilla/firefox/*.default*",
        "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*.default*",
        "~/.local/share/mozilla/firefox/*.default*",
    ]

    def __init__(self):
        self.profile_path: Optional[Path] = None
        self._find_profile()

    def _find_profile(self):
        import glob
        candidates = []
        for pattern in self.FIREFOX_PROFILES:
            matches = glob.glob(Path(pattern).expanduser().as_posix())
            for m in matches:
                p = Path(m)
                if (p / "cookies.sqlite").exists():
                    candidates.append(p)
        if candidates:
            # Prefer .default-release (usually the active one)
            for c in candidates:
                if "default-release" in c.name:
                    self.profile_path = c
                    print(f"[+] Firefox profile found: {self.profile_path}")
                    return
            self.profile_path = candidates[0]
            print(f"[+] Firefox profile found: {self.profile_path}")
            return
        raise RuntimeError("No Firefox profile found with cookies.sqlite")

    def _copy_db(self, db_name: str) -> Path:
        """Copy locked SQLite db to temp location for reading."""
        src = self.profile_path / db_name
        if not src.exists():
            raise FileNotFoundError(f"{src} not found")
        dst = Path(tempfile.gettempdir()) / f"{db_name}_copy_{int(time.time())}"
        shutil.copy2(src, dst)
        # Also copy WAL if present
        wal = src.parent / f"{db_name}-wal"
        if wal.exists():
            shutil.copy2(wal, dst.parent / f"{dst.name}-wal")
        return dst

    def get_x_cookies(self) -> dict:
        """Extract X auth cookies from Firefox."""
        db_path = self._copy_db("cookies.sqlite")
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT name, value, host
                FROM moz_cookies
                WHERE host LIKE '%twitter%' OR host LIKE '%x.com%'
                ORDER BY host, name
                """
            )
            cookies = {}
            for name, value, host in cursor.fetchall():
                # Use x.com domain cookies preferentially
                if host == ".x.com" or host == "x.com":
                    cookies[name] = value
            conn.close()
            return cookies
        finally:
            db_path.unlink(missing_ok=True)


class XAPICollector:
    """Uses extracted Firefox cookies to call X internal API with pagination."""

    BASE_URL = "https://x.com/i/api/graphql"
    # Common UserTweets query IDs (X rotates these; we'll try multiple)
    USER_TWEETS_IDS = [
        "HJfU6E62-NAJ6I2OG2V_QQ",
        "L3V6JkA-FnKC7XraCIBiZA",
        "PIt4Ch9A0TlnnZleNKtHQw",
    ]

    def __init__(self, cookies: dict):
        self.cookies = cookies
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://x.com/",
            "X-Csrf-Token": cookies.get("ct0", ""),
            "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "Content-Type": "application/json",
        })
        self.session.cookies.update(cookies)
        self.all_tweets: list[Tweet] = []

    def _get_user_id(self, screen_name: str) -> Optional[str]:
        """Resolve screen_name to numeric user_id via UserByScreenName."""
        query_id = "G3KGOASz96MH-uzHeTpDMA"
        url = f"{self.BASE_URL}/{query_id}/UserByScreenName"
        variables = json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True})
        params = {
            "variables": variables,
            "features": json.dumps({
                "hidden_profile_likes_enabled": True,
                "hidden_profile_subscriptions_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
                "subscriptions_verification_info_is_identity_verified_enabled": True,
                "subscriptions_verification_info_verified_since_enabled": True,
                "highlights_tweets_tab_ui_enabled": True,
                "creator_subscriptions_tweet_preview_api_enabled": True,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "responsive_web_graphql_timeline_navigation_enabled": True,
            }),
        }
        try:
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                user = data.get("data", {}).get("user", {})
                if user and user.get("result"):
                    result = user["result"]
                    if "rest_id" in result:
                        return result["rest_id"]
                    elif "__typename" in result and result["__typename"] == "UserUnavailable":
                        print(f"[!] User @{screen_name} is unavailable/suspended")
                        return None
            elif resp.status_code == 403:
                print(f"[!] 403 Forbidden — cookies may be expired or rate-limited")
            else:
                print(f"[!] get_user_id status {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[!] get_user_id error: {e}")
        return None

    def _parse_tweet_entry(self, entry: dict, username: str) -> Optional[Tweet]:
        """Parse a single tweet entry from X API response."""
        try:
            content = entry.get("content", {})
            item_content = content.get("itemContent", {})
            if item_content.get("__typename") != "TimelineTweet":
                return None

            tweet_results = item_content.get("tweet_results", {})
            if not tweet_results or tweet_results.get("__typename") == "TweetUnavailable":
                return None

            result = tweet_results.get("result", {})
            if result.get("__typename") == "TweetUnavailable":
                return None

            # Handle retweets: get the original tweet
            legacy = result.get("legacy", {})
            core = result.get("core", {})
            user_results = core.get("user_results", {}).get("result", {})
            user_legacy = user_results.get("legacy", {})

            tweet_id = legacy.get("id_str", "")
            screen_name = user_legacy.get("screen_name", username)
            display_name = user_legacy.get("name", "")
            full_text = legacy.get("full_text", "")
            created_at_str = legacy.get("created_at", "")

            # Counts
            likes = legacy.get("favorite_count", 0)
            replies = legacy.get("reply_count", 0)
            retweets = legacy.get("retweet_count", 0)
            quotes = legacy.get("quote_count", 0)

            is_reply = bool(legacy.get("in_reply_to_status_id_str"))
            is_retweet = bool(legacy.get("retweeted_status_result"))

            return Tweet(
                tweet_id=tweet_id,
                username=screen_name,
                display_name=display_name,
                text=full_text,
                created_at=created_at_str,
                likes=likes,
                replies=replies,
                retweets=retweets,
                quotes=quotes,
                is_reply=is_reply,
                is_retweet=is_retweet,
            )
        except Exception as e:
            return None

    def _extract_tweets_from_response(self, data: dict, target_username: str) -> tuple[list[Tweet], Optional[str]]:
        """Extract tweets and next cursor from API response."""
        tweets = []
        next_cursor = None

        try:
            instructions = (data.get("data", {})
                           .get("user", {})
                           .get("result", {})
                           .get("timeline_v2", {})
                           .get("timeline", {})
                           .get("instructions", []))

            for instruction in instructions:
                if instruction.get("type") == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                    for entry in entries:
                        entry_id = entry.get("entryId", "")
                        if entry_id.startswith("tweet-"):
                            tweet = self._parse_tweet_entry(entry, target_username)
                            if tweet:
                                tweets.append(tweet)
                        elif entry_id.startswith("cursor-bottom-"):
                            content = entry.get("content", {})
                            if content.get("__typename") == "TimelineTimelineCursor":
                                next_cursor = content.get("value")
        except Exception as e:
            print(f"[!] Error extracting tweets: {e}")

        return tweets, next_cursor

    def fetch_user_tweets(self, screen_name: str, max_pages: int = 50) -> list[Tweet]:
        """Fetch all tweets for a user using cursor pagination."""
        print(f"\n[==> Fetching tweets for @{screen_name}")

        user_id = self._get_user_id(screen_name)
        if not user_id:
            print(f"[!] Could not resolve user_id for @{screen_name}")
            return []
        print(f"[+] Resolved user_id: {user_id}")

        all_tweets: list[Tweet] = []
        cursor: Optional[str] = None
        pages = 0

        # Try each query ID until one works
        working_query_id = None
        for qid in self.USER_TWEETS_IDS:
            test_url = f"{self.BASE_URL}/{qid}/UserTweets"
            variables = json.dumps({
                "userId": user_id,
                "count": 40,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            })
            params = {
                "variables": variables,
                "features": json.dumps({
                    "responsive_web_graphql_exclude_directive_enabled": True,
                    "verified_phone_label_enabled": False,
                    "creator_subscriptions_tweet_preview_api_enabled": True,
                    "responsive_web_graphql_timeline_navigation_enabled": True,
                    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                    "tweetypie_unmention_optimization_enabled": True,
                    "responsive_web_edit_tweet_api_enabled": True,
                    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                    "view_counts_everywhere_api_enabled": True,
                    "longform_notetweets_consumption_enabled": True,
                    "responsive_web_twitter_article_tweet_consumption_enabled": False,
                    "tweet_awards_web_tipping_enabled": False,
                    "freedom_of_speech_not_reach_fetch_enabled": True,
                    "standardized_nudges_misinfo": True,
                    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                    "rweb_video_timestamps_enabled": True,
                    "longform_notetweets_rich_text_read_enabled": True,
                    "longform_notetweets_inline_media_enabled": True,
                    "responsive_web_media_download_video_enabled": False,
                    "responsive_web_enhance_cards_enabled": False,
                }),
            }
            try:
                resp = self.session.get(test_url, params=params, timeout=30)
                if resp.status_code == 200 and "timeline" in resp.text:
                    working_query_id = qid
                    print(f"[+] Working query ID found: {qid}")
                    break
                else:
                    print(f"[-] Query ID {qid} failed: {resp.status_code}")
            except Exception as e:
                print(f"[-] Query ID {qid} error: {e}")

        if not working_query_id:
            print("[!] No working query ID found. Cookies may be expired.")
            return []

        url = f"{self.BASE_URL}/{working_query_id}/UserTweets"

        while pages < max_pages:
            pages += 1
            variables_dict = {
                "userId": user_id,
                "count": 40,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            }
            if cursor:
                variables_dict["cursor"] = cursor

            params = {
                "variables": json.dumps(variables_dict),
                "features": json.dumps({
                    "responsive_web_graphql_exclude_directive_enabled": True,
                    "verified_phone_label_enabled": False,
                    "creator_subscriptions_tweet_preview_api_enabled": True,
                    "responsive_web_graphql_timeline_navigation_enabled": True,
                    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                    "tweetypie_unmention_optimization_enabled": True,
                    "responsive_web_edit_tweet_api_enabled": True,
                    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                    "view_counts_everywhere_api_enabled": True,
                    "longform_notetweets_consumption_enabled": True,
                    "responsive_web_twitter_article_tweet_consumption_enabled": False,
                    "tweet_awards_web_tipping_enabled": False,
                    "freedom_of_speech_not_reach_fetch_enabled": True,
                    "standardized_nudges_misinfo": True,
                    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                    "rweb_video_timestamps_enabled": True,
                    "longform_notetweets_rich_text_read_enabled": True,
                    "longform_notetweets_inline_media_enabled": True,
                    "responsive_web_media_download_video_enabled": False,
                    "responsive_web_enhance_cards_enabled": False,
                }),
            }

            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("x-rate-limit-reset", time.time() + 900)) - int(time.time())
                    wait = max(retry_after, 60)
                    print(f"[!] Rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                elif resp.status_code == 403:
                    print(f"[!] 403 Forbidden — session expired?")
                    break
                elif resp.status_code != 200:
                    print(f"[!] HTTP {resp.status_code}: {resp.text[:200]}")
                    break

                data = resp.json()
                tweets, next_cursor = self._extract_tweets_from_response(data, screen_name)

                if not tweets and not next_cursor:
                    print(f"[+] No more tweets (page {pages})")
                    break

                new_count = 0
                for t in tweets:
                    if t.tweet_id not in {x.tweet_id for x in all_tweets}:
                        all_tweets.append(t)
                        new_count += 1

                print(f"[+] Page {pages}: +{new_count} tweets (total: {len(all_tweets)}) | cursor: {next_cursor[:30] if next_cursor else 'None'}...")

                if not next_cursor:
                    break

                cursor = next_cursor

                # Rate limit internal delay
                delay = random.uniform(2.0, 5.0)
                time.sleep(delay)

            except Exception as e:
                print(f"[!] Error on page {pages}: {e}")
                break

        print(f"[==> Total tweets fetched for @{screen_name}: {len(all_tweets)}")
        return all_tweets

    def save_tweets(self, tweets: list[Tweet], screen_name: str, output_dir: Path = Path("data/firefox_results")):
        """Save tweets to JSON files."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Full raw dump
        raw_path = output_dir / f"{screen_name}_all_{timestamp}.json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in tweets], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(tweets)} tweets to {raw_path}")

        # Last 4 years filter
        cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
        recent = []
        for t in tweets:
            try:
                # Parse Twitter date format: "Mon Oct 24 12:34:56 +0000 2022"
                dt = datetime.strptime(t.created_at, "%a %b %d %H:%M:%S +0000 %Y")
                if dt.replace(tzinfo=timezone.utc) >= cutoff:
                    recent.append(t)
            except Exception:
                continue

        recent_path = output_dir / f"{screen_name}_last4years_{timestamp}.json"
        with open(recent_path, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in recent], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(recent)} recent tweets to {recent_path}")

        return raw_path, recent_path


def main():
    print("=" * 60)
    print("X/Twitter Firefox Cookie Collector")
    print("Extracts cookies from your logged-in Firefox and downloads")
    print("the full timeline via X's internal GraphQL API.")
    print("=" * 60)

    # 1. Extract cookies
    print("\n[1/3] Extracting cookies from Firefox...")
    extractor = FirefoxCookieExtractor()
    cookies = extractor.get_x_cookies()

    required = ["auth_token", "ct0", "twid"]
    missing = [c for c in required if c not in cookies]
    if missing:
        print(f"[!] Missing critical cookies: {missing}")
        print("[!] Make sure you are logged into x.com in Firefox.")
        return

    print(f"[+] Found cookies: {list(cookies.keys())}")

    # 2. Collect tweets
    print("\n[2/3] Starting collection...")
    collector = XAPICollector(cookies)

    targets = ["financialjuice", "Deltaone"]
    for target in targets:
        tweets = collector.fetch_user_tweets(target, max_pages=100)
        if tweets:
            collector.save_tweets(tweets, target)
        print()

    # 3. Summary
    print("\n[3/3] Done!")
    print(f"Total accounts processed: {len(targets)}")


if __name__ == "__main__":
    main()
