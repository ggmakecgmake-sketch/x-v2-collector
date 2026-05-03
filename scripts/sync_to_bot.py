#!/usr/bin/env python3
"""
Sync tweets from x-v2-collector to noticias-de-x PostgreSQL.

Usage:
    . venv/bin/activate && python scripts/sync_to_bot.py --account financialjuice

Requires: psycopg2-binary
    pip install psycopg2-binary python-dotenv
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("ERROR: pip install psycopg2-binary")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
data_dir = BASE_DIR / "data" / "tweets"

# ── PostgreSQL config (del bot noticias-de-x) ─────────────────
# Lee .env del bot si existe
BOT_DIR = Path.home() / "projects" / "claude-code" / "noticias-de-x"
env_file = BOT_DIR / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": os.getenv("POSTGRES_PORT", "5432"),
    "dbname": os.getenv("POSTGRES_DB", "trading_bot"),
    "user": os.getenv("POSTGRES_USER", "trading_bot"),
    "password": os.getenv("POSTGRES_PASSWORD", "trading_bot_secret"),
}


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def parse_twitter_date(date_str: str) -> Optional[datetime]:
    """Parse Twitter date string to datetime."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S +0000 %Y")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            # ISO format fallback
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt
        except ValueError:
            return None


def sync_account(account: str, dry_run: bool = False) -> dict:
    """Sync tweets for one account from JSON files to PostgreSQL."""
    all_file = data_dir / f"{account}_all.json"
    if not all_file.exists():
        print(f"⚠️ No data file for @{account}: {all_file}")
        return {"synced": 0, "skipped": 0, "errors": 0}

    with open(all_file, encoding="utf-8") as f:
        tweets = json.load(f)

    print(f"📦 Loaded {len(tweets)} tweets from {all_file}")

    if dry_run:
        print(f"🔍 DRY RUN: Would process {len(tweets)} tweets")
        return {"synced": 0, "skipped": 0, "errors": 0, "dry_run": True}

    conn = get_db_connection()
    cur = conn.cursor()

    synced = 0
    skipped = 0
    errors = 0
    batch = []

    for tweet in tweets:
        tweet_id = tweet.get("tweet_id", "")
        if not tweet_id or tweet_id == "0":
            skipped += 1
            continue

        created_at = parse_twitter_date(tweet.get("created_at", ""))
        if not created_at:
            skipped += 1
            continue

        engagement = {
            "likes": tweet.get("likes", 0),
            "replies": tweet.get("replies", 0),
            "retweets": tweet.get("retweets", 0),
            "quotes": tweet.get("quotes", 0),
        }

        batch.append((
            str(tweet_id),
            "",  # author_id (not available from syndication)
            tweet.get("username", account),
            tweet.get("text", ""),
            created_at,
            json.dumps(engagement),
            json.dumps(tweet),  # raw_data
        ))

    if batch:
        try:
            execute_values(
                cur,
                """
                INSERT INTO tweets (tweet_id, author_id, author_username, text, created_at, engagement, raw_data)
                VALUES %s
                ON CONFLICT (tweet_id) DO NOTHING
                """,
                batch,
                page_size=1000,
            )
            conn.commit()
            synced = cur.rowcount
            print(f"✅ Synced {synced} new tweets to PostgreSQL")
        except Exception as e:
            conn.rollback()
            print(f"❌ Sync error: {e}")
            errors = len(batch)
        finally:
            cur.close()
            conn.close()

    skipped += len(tweets) - len(batch)
    return {
        "account": account,
        "total": len(tweets),
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Sync x-v2-collector tweets to noticias-de-x PostgreSQL")
    parser.add_argument("--account", "-a", default="all", help="Account to sync or 'all'")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    # Detect accounts from data dir
    accounts = []
    if args.account == "all":
        for f in data_dir.glob("*_all.json"):
            accounts.append(f.stem.replace("_all", ""))
    else:
        accounts = [args.account]

    if not accounts:
        print("No accounts found. Run deep_scrape first.")
        sys.exit(1)

    print(f"🔄 Syncing {len(accounts)} account(s): {', '.join(accounts)}")
    print(f"🗄️  DB: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")

    for acc in accounts:
        print(f"\n{'='*50}")
        print(f"📡 @{acc}")
        print(f"{'='*50}")
        result = sync_account(acc, dry_run=args.dry_run)
        print(f"   Total: {result.get('total', 0)} | Synced: {result.get('synced', 0)} | Skipped: {result.get('skipped', 0)} | Errors: {result.get('errors', 0)}")


if __name__ == "__main__":
    main()
