"""
Twikit + Firefox Cookies — Full Timeline Scraper
Extracts live cookies from your running Firefox and feeds them into
twikit for authenticated timeline scraping with full pagination.
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
    source: str = "twikit_firefox"


class FirefoxCookieExtractor:
    """Extracts X auth cookies from Firefox profile."""

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
            for c in candidates:
                if "default-release" in c.name:
                    self.profile_path = c
                    return
            self.profile_path = candidates[0]
            return
        raise RuntimeError("No Firefox profile with cookies.sqlite found")

    def _copy_db(self, db_name: str) -> Path:
        src = self.profile_path / db_name
        if not src.exists():
            raise FileNotFoundError(f"{src} not found")
        dst = Path(tempfile.gettempdir()) / f"{db_name}_copy_{int(time.time())}"
        shutil.copy2(src, dst)
        wal = src.parent / f"{db_name}-wal"
        if wal.exists():
            shutil.copy2(wal, dst.parent / f"{dst.name}-wal")
        return dst

    def get_x_cookies(self) -> dict:
        db_path = self._copy_db("cookies.sqlite")
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT name, value, host
                FROM moz_cookies
                WHERE host LIKE '%x.com%'
                ORDER BY name
                """
            )
            cookies = {}
            for name, value, host in cursor.fetchall():
                cookies[name] = value
            conn.close()
            return cookies
        finally:
            db_path.unlink(missing_ok=True)

    def save_for_twikit(self, output_path: Path = Path("data/firefox_cookies.json")):
        cookies = self.get_x_cookies()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Twikit expects {name: value} dict
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(cookies)} cookies to {output_path}")
        return output_path


class TwikitFirefoxCollector:
    """Uses twikit with Firefox cookies to scrape full timelines."""

    def __init__(self, cookies_path: Path):
        self.cookies_path = cookies_path
        self.client = Client(language="en-US")
        self._load_cookies()

    def _load_cookies(self):
        """Load cookies into twikit client."""
        with open(self.cookies_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        # Twikit expects {name: value} dict
        self.client.set_cookies(cookies)
        print(f"[+] Loaded {len(cookies)} cookies into twikit")

    async def fetch_user_tweets(self, screen_name: str, max_pages: int = 200) -> list[Tweet]:
        """Fetch all tweets using twikit's paginated get_user_tweets."""
        print(f"\n[==>] Fetching @{screen_name} via twikit + Firefox cookies")
        all_tweets: list[Tweet] = []
        seen_ids = set()
        cursor = None
        pages = 0

        while pages < max_pages:
            pages += 1
            try:
                if cursor:
                    tweets_page = await self.client.get_user_tweets(
                        screen_name, "Tweets", cursor=cursor
                    )
                else:
                    # Need to resolve user_id first
                    user = await self.client.get_user_by_screen_name(screen_name)
                    if not user:
                        print(f"[!] User @{screen_name} not found")
                        break
                    tweets_page = await self.client.get_user_tweets(user.id, "Tweets")

                if not tweets_page:
                    print(f"[+] No more tweets (empty page {pages})")
                    break

                new_count = 0
                for t in tweets_page:
                    tid = str(getattr(t, "id", ""))
                    if not tid or tid in seen_ids:
                        continue
                    seen_ids.add(tid)

                    tweet = Tweet(
                        tweet_id=tid,
                        username=getattr(t, "user", {}).get("screen_name", screen_name) if isinstance(getattr(t, "user", {}), dict) else screen_name,
                        display_name=getattr(t, "user", {}).get("name", "") if isinstance(getattr(t, "user", {}), dict) else "",
                        text=getattr(t, "text", ""),
                        created_at=getattr(t, "created_at", ""),
                        likes=getattr(t, "favorite_count", 0),
                        replies=getattr(t, "reply_count", 0),
                        retweets=getattr(t, "retweet_count", 0),
                        quotes=getattr(t, "quote_count", 0),
                        is_reply=getattr(t, "in_reply_to", None) is not None,
                        is_retweet=bool(getattr(t, "retweeted_status", None)),
                    )
                    all_tweets.append(tweet)
                    new_count += 1

                print(f"[+] Page {pages}: +{new_count} tweets | total: {len(all_tweets)}")

                # Get next cursor
                cursor = getattr(tweets_page, "next_cursor", None)
                if not cursor:
                    print(f"[+] No cursor — end of timeline")
                    break

                # Rate limit
                await self.client.sleep(random.uniform(3.0, 7.0))

            except TooManyRequests:
                wait = random.uniform(60, 120)
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
                print(f"[!] Error on page {pages}: {e}")
                break

        print(f"[==>] Total for @{screen_name}: {len(all_tweets)} tweets")
        return all_tweets

    def save(self, tweets: list[Tweet], screen_name: str, output_dir: Path = Path("data/twikit_results")):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        raw = output_dir / f"{screen_name}_all_{ts}.json"
        with open(raw, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in tweets], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(tweets)} tweets to {raw}")

        # Filter last 4 years
        cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
        recent = []
        for t in tweets:
            try:
                dt = datetime.strptime(t.created_at, "%a %b %d %H:%M:%S +0000 %Y")
                if dt.replace(tzinfo=timezone.utc) >= cutoff:
                    recent.append(t)
            except Exception:
                continue
        rec = output_dir / f"{screen_name}_last4years_{ts}.json"
        with open(rec, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in recent], f, ensure_ascii=False, indent=2)
        print(f"[+] Saved {len(recent)} recent tweets to {rec}")
        return raw, rec


async def main():
    print("=" * 60)
    print("Twikit + Firefox Cookie Full Timeline Scraper")
    print("=" * 60)

    # Step 1: Extract cookies
    print("\n[1/3] Extracting cookies from Firefox...")
    extractor = FirefoxCookieExtractor()
    cookies_path = extractor.save_for_twikit()

    # Step 2: Collect
    print("\n[2/3] Starting twikit collection with your session...")
    collector = TwikitFirefoxCollector(cookies_path)

    targets = ["financialjuice", "Deltaone"]
    for target in targets:
        tweets = await collector.fetch_user_tweets(target, max_pages=200)
        if tweets:
            collector.save(tweets, target)
        print()

    print("\n[3/3] Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
