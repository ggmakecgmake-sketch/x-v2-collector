"""
Full test + backfill runner using syndication endpoint (NO login required).

Usage:
    python test_syndication_runner.py

Saves tweets to data/syndication_results/ as JSON.
"""
import asyncio
import json
import os
from datetime import datetime, timezone

os.environ.setdefault("LOG_LEVEL", "INFO")

import structlog
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    logger_factory=structlog.PrintLoggerFactory(),
)

from src.engine.syndication_engine import SyndicationEngine
from src.config import settings


def save_to_json(tweets, screen_name, label=""):
    """Save tweets to JSON file with timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dirp = os.path.expanduser("~/projects/x-v2-collector/data/syndication_results")
    os.makedirs(dirp, exist_ok=True)

    path = os.path.join(dirp, f"{screen_name}_{label}_{ts}.json")

    # Convert Tweet objects to dicts
    data = {
        "meta": {
            "source": "syndication",
            "screen_name": screen_name,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "total_tweets": len(tweets),
            "app": "x-v2-collector",
            "version": "0.1.0",
        },
        "tweets": [],
    }

    for t in tweets:
        data["tweets"].append({
            "tweet_id": t.tweet_id,
            "author_username": t.author_username,
            "author_id": t.author_id,
            "text": t.text,
            "created_at": t.created_at,
            "engagement": t.engagement,
            "source_engine": t.source_engine,
            "raw_data": t.raw_data,
        })

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"💾 Saved {len(tweets)} tweets to {path}")
    return path


async def main():
    print("=" * 65)
    print("X-v2 COLLECTOR — SYNDICATION TEST + BACKFILL")
    print("=" * 65)
    print(f"Target accounts: {settings.x_accounts_to_track}")
    print(f"Mode: ANONYMOUS (no login)")
    print(f"Endpoint: Syndication (syndication.twitter.com)")
    print("-" * 65)

    engine = SyndicationEngine()
    await engine.start()

    all_results = {}
    total_tweets = 0
    oldest_per_account = {}
    newest_per_account = {}

    # Calculate date 4 years ago
    four_years_ago = datetime.now(timezone.utc)
    try:
        four_years_ago = four_years_ago.replace(year=four_years_ago.year - 4)
    except ValueError:
        # Feb 29
        four_years_ago = four_years_ago.replace(year=four_years_ago.year - 4, day=four_years_ago.day - 1)
    four_years_iso = four_years_ago.isoformat()
    print(f"\n🗓️  Target period: Last 4 years (since {four_years_iso[:10]})")

    for account in settings.x_accounts_to_track:
        print(f"\n🔍 Scaping @{account}...")
        try:
            tweets = engine.fetch_timeline(account)
            all_results[account] = tweets

            total_tweets += len(tweets)

            # Find date range
            dates = [
                datetime.fromisoformat(t.created_at)
                for t in tweets if t.created_at
            ]
            if dates:
                oldest = min(dates)
                newest = max(dates)
                oldest_per_account[account] = oldest
                newest_per_account[account] = newest

            # Filter to last 4 years
            recent_tweets = []
            for t in tweets:
                if t.created_at:
                    dt = datetime.fromisoformat(t.created_at)
                    if dt >= four_years_ago:
                        recent_tweets.append(t)

            # Save raw (all available)
            raw_path = save_to_json(tweets, account, "raw")

            # Save filtered (last 4 years only)
            recent_path = save_to_json(recent_tweets, account, "last4years")

            print(f"   ✅ Total available: {len(tweets)} tweets")
            print(f"   📅 Date range: {oldest.strftime('%Y-%m-%d')} → {newest.strftime('%Y-%m-%d')}")
            print(f"   🗓️  Within 4 years: {len(recent_tweets)} tweets")
            print(f"   💾 Saved to: {raw_path}")
            print(f"   💾 Filtered: {recent_path}")

            # Print top 3
            for i, t in enumerate(tweets[:3]):
                text = t.text[:60] if t.text else ""
                created = (t.created_at or "")[:16] if t.created_at else ""
                likes = t.engagement.get("favorite_count", 0)
                print(f"      [{i+1}] {created} | ❤️{likes} | {text}...")

        except Exception as exc:
            print(f"   ❌ Failed: {exc}")
            all_results[account] = []

    await engine.stop()

    # Summary
    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    print(f"Total tweets fetched: {total_tweets}")

    for account, tweets in all_results.items():
        recent_count = 0
        for t in tweets:
            if t.created_at:
                dt = datetime.fromisoformat(t.created_at)
                if dt >= four_years_ago:
                    recent_count += 1
        print(f"\n  @{account}:")
        print(f"    Available: {len(tweets)} tweets")
        print(f"    Last 4 years: {recent_count} tweets")

        if account in oldest_per_account and account in newest_per_account:
            print(f"    Date range: {oldest_per_account[account].strftime('%Y-%m-%d')} → {newest_per_account[account].strftime('%Y-%m-%d')}")

    # Data directory
    data_dir = os.path.expanduser("~/projects/x-v2-collector/data/syndication_results")
    print(f"\n💾 All data saved to: {data_dir}")

    if total_tweets == 0:
        print("\n⚠️  No tweets found. Possible issues:")
        print("   - Rate limited (wait a few minutes)")
        print("   - Syndication endpoint changed")
        print("   - Accounts are private or restricted")


if __name__ == "__main__":
    asyncio.run(main())
