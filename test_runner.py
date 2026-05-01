"""
Test runner for x-v2-collector.
Attempts scraping WITHOUT login to test what X allows anonymously.
"""
import asyncio
import json
import os
from datetime import datetime

# Set test env
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ["REDIS_HOST"] = "localhost"

import structlog
structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer()],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    logger_factory=structlog.PrintLoggerFactory(),
)

from src.engine.playwright_engine import PlaywrightEngine
from src.config import settings

async def test_playwright_anonymous():
    """Test Playwright in anonymous mode."""
    print("=" * 60)
    print("TESTING: Playwright ANONYMOUS mode")
    print("Target accounts:", settings.x_accounts_to_track)
    print("=" * 60)

    engine = PlaywrightEngine(headless=True, anonymous=True)
    all_tweets = []

    try:
        await engine.start()
        print("✅ Browser started (anonymous)")
        print(f"   Logged in: {engine.logged_in}")
    except Exception as exc:
        print(f"❌ Browser startup failed: {exc}")
        return []

    for account in settings.x_accounts_to_track:
        print(f"\n--- Fetching @{account} ---")
        try:
            tweets = await engine.fetch_timeline(account, count=10)
            print(f"✅ Found {len(tweets)} tweets")
            for t in tweets:
                print(f"   [{t.tweet_id[:20]}] {t.text[:100]}...")
            all_tweets.extend(tweets)
        except Exception as exc:
            print(f"❌ Failed: {exc}")

    # Try search
    print("\n--- Test: Search for 'Bitcoin' (anonymous) ---")
    try:
        tweets = await engine.search_anonymous("Bitcoin", count=10)
        print(f"✅ Found {len(tweets)} search results")
        for t in tweets:
            print(f"   [{t.author_username}] {t.text[:80]}...")
        all_tweets.extend(tweets)
    except Exception as exc:
        print(f"❌ Search failed: {exc}")

    await engine.stop()
    return all_tweets


def save_results(tweets, label=""):
    if not tweets:
        print("\n⚠️  No tweets to save.")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.expanduser(f"~/projects/x-v2-collector/data/scrape_test_{label}_{ts}.json")

    # Convert to dicts
    data = [t.__dict__ for t in tweets]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Saved {len(tweets)} tweets to {path}")


async def main():
    tweets = await test_playwright_anonymous()

    print("\n" + "=" * 60)
    print(f"TOTAL TWEETS CAPTURED: {len(tweets)}")
    print("=" * 60)

    save_results(tweets, label="anon")

    if len(tweets) == 0:
        print("\n📋 NOTES:")
        print("   - X blocks most anonymous timeline access now.")
        print("   - You need: TWITTER_USERNAME + TWITTER_PASSWORD for authenticated scraping.")
        print("   - Some search queries work without login (limited).")
        print("   - To see what works, check screenshots at /tmp/x_v2_debug_*.png")


if __name__ == "__main__":
    asyncio.run(main())
