#!/usr/bin/env python3
"""
Twikit + Firefox Cookies — Full Authenticated Timeline Collector

Uses cookies extracted from your running Firefox session (logged in to X)
to authenticate twikit, then downloads COMPLETE timelines with pagination.
"""

import json
import sqlite3
import shutil
import tempfile
import time
import random
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict

from twikit import Client
from twikit.errors import TooManyRequests, Forbidden, BadRequest


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
    source: str = "twikit_authenticated"


def get_firefox_cookies(x_only: bool = True) -> dict:
    """Extract cookies from Firefox profile."""
    import glob
    profiles = glob.glob(str(Path("~/.config/mozilla/firefox/*.default-release*").expanduser()))
    profiles += glob.glob(str(Path("~/.mozilla/firefox/*.default-release*").expanduser()))
    candidates = [Path(p) for p in profiles if (Path(p) / "cookies.sqlite").exists()]
    if not candidates:
        raise RuntimeError("No Firefox .default-release profile found")
    candidates.sort(key=lambda p: (p / "cookies.sqlite").stat().st_mtime, reverse=True)
    profile = candidates[0]
    print(f"[+] Firefox profile: {profile}")

    db_src = profile / "cookies.sqlite"
    db_tmp = Path(tempfile.gettempdir()) / f"ff_cookies_{int(time.time())}.sqlite"
    shutil.copy2(db_src, db_tmp)
    wal = db_src.parent / "cookies.sqlite-wal"
    if wal.exists():
        shutil.copy2(wal, str(db_tmp) + "-wal")

    try:
        conn = sqlite3.connect(str(db_tmp))
        cur = conn.cursor()
        if x_only:
            cur.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ? ORDER BY name",
                ("%x.com%",),
            )
        else:
            cur.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ? OR host LIKE ? ORDER BY name",
                ("%x.com%", "%twitter.com%"),
            )
        cookies = {n: v for n, v in cur.fetchall()}
        conn.close()
        print(f"[+] Read {len(cookies)} cookies")
        return cookies
    finally:
        db_tmp.unlink(missing_ok=True)


def save_cookies(cookies: dict, path: Path = Path("data/firefox_cookies.json")):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    return path


class AuthenticatedCollector:
    def __init__(self, cookies: dict, language: str = "en-US"):
        self.client = Client(language=language)
        self.client.set_cookies(cookies)
        print("[+] Cookies loaded into twikit client")

    async def collect_full_timeline(
        self,
        screen_name: str,
        max_pages: int = 200,
        tweet_type: str = "Tweets",
    ) -> list[Tweet]:
        print(f"\n[==>] Collecting @{screen_name} (type={tweet_type})")

        # Resolve user_id
        try:
            user = await self.client.get_user_by_screen_name(screen_name)
            if not user:
                print(f"[!] User @{screen_name} not found")
                return []
            user_id = user.id
            print(f"[+] User ID: {user_id}")
        except Exception as e:
            print(f"[!] Failed to resolve user: {e}")
            return []

        all_tweets: list[Tweet] = []
        seen_ids = set()
        cursor = None
        pages = 0
        empty_pages = 0

        while pages < max_pages and empty_pages < 3:
            pages += 1
            try:
                if cursor:
                    page = await self.client.get_user_tweets(user_id, tweet_type=tweet_type, cursor=cursor)
                else:
                    page = await self.client.get_user_tweets(user_id, tweet_type=tweet_type)

                if not page:
                    empty_pages += 1
                    print(f"  Page {pages}: EMPTY (streak={empty_pages})")
                    await self.client.sleep(random.uniform(3, 6))
                    continue

                new_count = 0
                for idx, t in enumerate(page):
                    tid = str(t.id)
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)

                    tweet = Tweet(
                        tweet_id=tid,
                        username=getattr(t.user, "screen_name", screen_name),
                        display_name=getattr(t.user, "name", ""),
                        text=t.text or "",
                        created_at=t.created_at or "",
                        likes=t.favorite_count or 0,
                        replies=t.reply_count or 0,
                        retweets=t.retweet_count or 0,
                        quotes=t.quote_count or 0,
                        is_reply=t.in_reply_to is not None,
                        is_retweet=t.retweeted_tweet is not None,
                    )
                    all_tweets.append(tweet)
                    new_count += 1

                print(f"  Page {pages}: +{new_count} new | total={len(all_tweets)}")

                # Get next cursor
                cursor = getattr(page, "next_cursor", None)
                if not cursor:
                    print(f"[+] No cursor — end of timeline")
                    break

                await self.client.sleep(random.uniform(2.0, 5.0))

            except TooManyRequests:
                wait = random.uniform(60, 180)
                print(f"[!] Rate limited — sleeping {wait:.0f}s...")
                await self.client.sleep(wait)
                continue
            except Forbidden as e:
                print(f"[!] Forbidden (session expired?): {e}")
                break
            except BadRequest as e:
                print(f"[!] Bad request: {e}")
                break
            except Exception as e:
                print(f"[!] Error: {e}")
                break

        print(f"[==>] @{screen_name}: {len(all_tweets)} tweets collected")
        return all_tweets

    def save_results(
        self,
        tweets: list[Tweet],
        screen_name: str,
        out_dir: Path = Path("data/twikit_auth_results"),
    ):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # All
        raw = out_dir / f"{screen_name}_all_{ts}.json"
        with open(raw, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in tweets], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(tweets)} tweets to {raw}")

        # Last 4 years
        cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
        recent = []
        for t in tweets:
            try:
                dt = datetime.strptime(t.created_at, "%a %b %d %H:%M:%S +0000 %Y")
                if dt.replace(tzinfo=timezone.utc) >= cutoff:
                    recent.append(t)
            except Exception:
                continue
        rec = out_dir / f"{screen_name}_last4years_{ts}.json"
        with open(rec, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in recent], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(recent)} recent tweets to {rec}")
        return raw, rec


async def main():
    print("=" * 60)
    print("Twikit + Firefox Cookies — Full Timeline Collector")
    print("=" * 60)

    # Step 1: Extract cookies
    print("\n[1/3] Extracting cookies from Firefox...")
    cookies = get_firefox_cookies()
    save_cookies(cookies)

    # Verify critical cookies
    critical = ["auth_token", "ct0", "twid", "kdt"]
    missing = [c for c in critical if c not in cookies]
    if missing:
        print(f"[!] MISSING critical cookies: {missing}")
        print("[!] Make sure you are LOGGED IN to x.com in Firefox.")
        return
    print("[+] All critical cookies present")

    # Step 2: Collect
    print("\n[2/3] Starting collection...")
    collector = AuthenticatedCollector(cookies)

    targets = ["financialjuice", "Deltaone"]
    for target in targets:
        tweets = await collector.collect_full_timeline(target, max_pages=200)
        if tweets:
            collector.save_results(tweets, target)
        print()

    print("[3/3] Done!")


if __name__ == "__main__":
    asyncio.run(main())
