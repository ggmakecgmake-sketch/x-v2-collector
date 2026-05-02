#!/usr/bin/env python3
"""
Selenium + Firefox + Injected Cookies — Full Timeline Scraper

- Reads cookies from your running Firefox
- Starts a NEW browser with those cookies injected
- Scrolls X timeline and extracts all tweets
"""

import json
import re
import sqlite3
import shutil
import tempfile
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


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
    source: str = "selenium_firefox"


def get_firefox_cookies() -> list[dict]:
    """Read X cookies from running Firefox profile."""
    import glob
    profiles = glob.glob(str(Path("~/.config/mozilla/firefox/*.default-release*").expanduser()))
    profiles += glob.glob(str(Path("~/.mozilla/firefox/*.default-release*").expanduser()))
    candidates = [p for p in profiles if Path(p).exists() and (Path(p) / "cookies.sqlite").exists()]
    if not candidates:
        raise RuntimeError("No Firefox profile found")
    candidates.sort(key=lambda p: (Path(p) / "cookies.sqlite").stat().st_mtime, reverse=True)
    profile = Path(candidates[0])
    print(f"[+] Using Firefox profile: {profile}")

    # Copy DB (locked by running Firefox)
    db_src = profile / "cookies.sqlite"
    db_tmp = Path(tempfile.gettempdir()) / f"ff_cookies_{int(time.time())}.sqlite"
    shutil.copy2(db_src, db_tmp)
    wal = db_src.parent / "cookies.sqlite-wal"
    if wal.exists():
        shutil.copy2(wal, str(db_tmp) + "-wal")

    try:
        conn = sqlite3.connect(str(db_tmp))
        cur = conn.cursor()
        cur.execute(
            "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE host LIKE '%x.com%' ORDER BY name"
        )
        rows = cur.fetchall()
        conn.close()

        cookies = []
        for name, value, host, path, expiry, secure, httponly, samesite in rows:
            # Firefox stores expiry in seconds (Unix epoch) for session cookies
            # and milliseconds for persistent cookies — normalize to seconds
            expires = None
            if expiry is not None:
                if expiry > 4000000000:  # likely milliseconds
                    expires = int(expiry / 1000)
                else:
                    expires = int(expiry)
                if expires < 0:
                    expires = None

            cookies.append({
                "name": name,
                "value": value,
                "domain": host,
                "path": path or "/",
                "secure": bool(secure),
                "httpOnly": bool(httponly),
                "expiry": expires,
                "sameSite": str(samesite or ""),
            })
        return cookies
    finally:
        db_tmp.unlink(missing_ok=True)


def parse_article(article, default_user: str) -> Tweet | None:
    """Parse a tweet article element (Selenium WebElement)."""
    try:
        # Tweet ID from status link
        tweet_id = ""
        links = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
        for a in links:
            href = a.get_attribute("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if m:
                tweet_id = m.group(1)
                break
        if not tweet_id:
            return None

        # Username from link
        username = default_user
        for a in links:
            href = a.get_attribute("href") or ""
            um = re.match(r"https?://x\.com/([A-Za-z0-9_]+)$", href)
            if um:
                username = um.group(1)
                break

        # Display name
        display_name = ""
        try:
            name_el = article.find_element(By.CSS_SELECTOR, "a[data-testid='User-Name'] span")
            display_name = (name_el.text or "").strip()
        except Exception:
            pass

        # Text
        text = ""
        try:
            text_el = article.find_element(By.CSS_SELECTOR, "div[data-testid='tweetText']")
            text = (text_el.text or "").strip()
        except Exception:
            pass

        # Date
        created_at = ""
        try:
            time_el = article.find_element(By.CSS_SELECTOR, "time")
            created_at = time_el.get_attribute("datetime") or ""
        except Exception:
            pass

        # Engagement counts via aria-label
        likes = 0
        replies = 0
        retweets = 0
        try:
            like_btn = article.find_element(By.CSS_SELECTOR, "button[data-testid='like']")
            label = like_btn.get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                likes = int(nums[0].replace(",", ""))
        except Exception:
            pass
        try:
            reply_btn = article.find_element(By.CSS_SELECTOR, "button[data-testid='reply']")
            label = reply_btn.get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                replies = int(nums[0].replace(",", ""))
        except Exception:
            pass
        try:
            rt_btn = article.find_element(By.CSS_SELECTOR, "button[data-testid='retweet']")
            label = rt_btn.get_attribute("aria-label") or ""
            nums = re.findall(r"[\d,]+", label)
            if nums:
                retweets = int(nums[0].replace(",", ""))
        except Exception:
            pass

        # Reply / retweet detection
        is_reply = False
        try:
            article.find_element(By.CSS_SELECTOR, "[data-testid='socialContext']")
            is_reply = True
        except Exception:
            pass

        is_retweet = False
        try:
            article.find_element(By.XPATH, ".//span[contains(text(), 'reposted')]")
            is_retweet = True
        except Exception:
            pass

        return Tweet(
            tweet_id=tweet_id,
            username=username,
            display_name=display_name,
            text=text,
            created_at=created_at,
            likes=likes,
            replies=replies,
            retweets=retweets,
            quotes=0,
            is_reply=is_reply,
            is_retweet=is_retweet,
        )
    except Exception:
        return None


def save_tweets(tweets: list[Tweet], screen_name: str, out_dir: Path = Path("data/selenium_results")):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    raw = out_dir / f"{screen_name}_all_{ts}.json"
    with open(raw, "w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in tweets], f, ensure_ascii=False, indent=2)
    print(f"[+] Saved {len(tweets)} tweets to {raw}")

    # Last 4 years
    cutoff = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 4)
    recent = []
    for t in tweets:
        try:
            dt = datetime.fromisoformat(t.created_at.replace("Z", "+00:00"))
            if dt >= cutoff:
                recent.append(t)
        except Exception:
            continue
    rec = out_dir / f"{screen_name}_last4years_{ts}.json"
    with open(rec, "w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in recent], f, ensure_ascii=False, indent=2)
    print(f"[+] Saved {len(recent)} recent tweets to {rec}")
    return raw, rec


def collect_user(screen_name: str, cookies: list[dict], max_scrolls: int = 300, headless: bool = True):
    print(f"\n[==>] Collecting @{screen_name} via Selenium + Firefox")

    options = FirefoxOptions()
    options.headless = headless

    driver = webdriver.Firefox(options=options)
    try:
        # Go to X.com first to set domain for cookies
        print("[+] Loading x.com to inject cookies...")
        driver.get("https://x.com")
        time.sleep(3)

        # Inject cookies
        for c in cookies:
            try:
                cookie_dict = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c["path"],
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                }
                if c.get("expiry"):
                    cookie_dict["expiry"] = c["expiry"]
                driver.add_cookie(cookie_dict)
            except Exception as e:
                pass  # Some cookies may fail, that's OK

        print(f"[+] Navigating to https://x.com/{screen_name}")
        driver.get(f"https://x.com/{screen_name}")

        # Wait for tweets to load
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article[data-testid='tweet']"))
            )
            print("[+] Timeline loaded")
        except Exception as e:
            print(f"[!] No tweets found within 30s: {e}")
            driver.save_screenshot(f"data/debug_{screen_name}.png")
            return []

        seen_ids = set()
        all_tweets: list[Tweet] = []
        no_new = 0

        for scroll in range(1, max_scrolls + 1):
            articles = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            new_this_scroll = 0

            for art in articles:
                tweet = parse_article(art, screen_name)
                if tweet and tweet.tweet_id and tweet.tweet_id not in seen_ids:
                    seen_ids.add(tweet.tweet_id)
                    all_tweets.append(tweet)
                    new_this_scroll += 1

            print(f"[+] Scroll {scroll}/{max_scrolls}: +{new_this_scroll} new | total {len(all_tweets)} | streak={no_new}")

            if new_this_scroll == 0:
                no_new += 1
                if no_new >= 5:
                    print("[+] No new tweets — end of timeline")
                    break
            else:
                no_new = 0

            # Scroll down
            driver.execute_script("window.scrollBy(0, 3000)")
            time.sleep(random.uniform(1.5, 3.0))

        print(f"[==>] Total: {len(all_tweets)} tweets")
        return all_tweets
    finally:
        driver.quit()


def main():
    print("=" * 60)
    print("Selenium + Firefox + Cookies — Full Timeline Scraper")
    print("=" * 60)

    # Step 1: Extract cookies
    print("\n[1/3] Reading cookies from Firefox...")
    cookies = get_firefox_cookies()
    print(f"[+] Got {len(cookies)} cookies")

    # Step 2: Collect
    print("\n[2/3] Scraping timelines...")
    targets = ["financialjuice"]  # Start with one to verify it works
    for target in targets:
        tweets = collect_user(target, cookies, max_scrolls=200, headless=True)
        if tweets:
            save_tweets(tweets, target)
        print()

    # Step 3: Summary
    print("[3/3] Done!")


if __name__ == "__main__":
    main()
