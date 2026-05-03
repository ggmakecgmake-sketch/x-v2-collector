#!/usr/bin/env python3
"""
Firefox xdotool Scraper — Controls a real Firefox via xdotool.
Exactly like a human: opens Firefox, types URL, scrolls, extracts.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TWEETS_DIR = DATA_DIR / "tweets"
TWEETS_DIR.mkdir(parents=True, exist_ok=True)

DISPLAY = os.getenv("DISPLAY", ":0")


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(DATA_DIR / "xdotool_scraper.log", "a") as f:
        f.write(line + "\n")


def run(cmd: str) -> str:
    """Run a shell command and return stdout."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, env={**os.environ, "DISPLAY": DISPLAY})
    return result.stdout.strip()


def xdotool(cmd: str) -> str:
    return run(f"xdotool {cmd}")


def activate_firefox():
    """Activate Firefox window."""
    log("Activating Firefox...")
    # Find Firefox window
    win_id = xdotool("search --class 'firefox' | head -1")
    if not win_id:
        log("Firefox not found, launching...")
        subprocess.Popen(["firefox"], env={**os.environ, "DISPLAY": DISPLAY}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        win_id = xdotool("search --class 'firefox' | head -1")
    if win_id:
        xdotool(f"windowactivate {win_id}")
        xdotool(f"windowfocus {win_id}")
        time.sleep(1)
    return win_id


def navigate_to(url: str):
    """Navigate Firefox to URL."""
    log(f"Navigating to {url}...")
    # Focus address bar
    xdotool("key ctrl+l")
    time.sleep(0.5)
    # Select all + type URL
    xdotool("key ctrl+a")
    xdotool(f"type --delay 10 '{url}'")
    time.sleep(0.5)
    xdotool("key Return")
    time.sleep(5)


def inject_cookie_extract_script():
    """Inject JS to extract tweets from page."""
    # Open web console
    xdotool("key ctrl+shift+k")
    time.sleep(1)
    # Type JS to extract tweets and log as JSON
    js = """
    (function(){
        const articles = document.querySelectorAll("article[data-testid='tweet']");
        const tweets = [];
        articles.forEach(art => {
            const links = art.querySelectorAll("a[href*='/status/']");
            let tweetId = "";
            for (const link of links) {
                const href = link.href || "";
                if (href.includes("/status/")) {
                    const parts = href.split("/status/");
                    if (parts.length > 1) {
                        tweetId = parts[1].split("?")[0].split("/")[0];
                        if (/^\\d+$/.test(tweetId)) break;
                    }
                }
            }
            if (!tweetId) return;
            const textEls = art.querySelectorAll("div[data-testid='tweetText']");
            const text = Array.from(textEls).map(e => e.innerText).join(" ").trim();
            const timeEl = art.querySelector("time");
            const createdAt = timeEl ? timeEl.getAttribute("datetime") : "";
            const nameEl = art.querySelector("div[data-testid='User-Name']");
            const displayName = nameEl ? nameEl.innerText.split("\\n")[0] : "";
            const isReply = !!art.querySelector("div[data-testid='tweetReplyContext']");
            const isRetweet = !!art.querySelector("span[data-testid='socialContext']");
            tweets.push({tweet_id: tweetId, text, created_at: createdAt, display_name: displayName, is_reply: isReply, is_retweet: isRetweet});
        });
        console.log("TWEET_EXTRACT:" + JSON.stringify(tweets));
    })();
    """
    # Type JS line by line (simplified, we use xdotool type)
    xdotool(f"type --delay 5 '{js}'")
    time.sleep(0.5)
    xdotool("key Return")
    time.sleep(2)
    # Close console
    xdotool("key ctrl+shift+k")


def scroll_page():
    """Scroll down one page."""
    xdotool("key Page_Down")
    time.sleep(2)


def run_xdotool_scraper(account: str, max_scrolls: int = 1000):
    output_file = TWEETS_DIR / f"{account}_all.json"

    existing = {}
    if output_file.exists():
        try:
            raw = json.loads(output_file.read_text(encoding="utf-8"))
            for t in raw:
                existing[t.get("tweet_id", "")] = t
            log(f"Resuming @{account}: {len(existing)} tweets")
        except Exception:
            pass

    seen = set(existing.keys())
    tweets = dict(existing)

    # Activate Firefox
    activate_firefox()

    # Navigate to x.com
    navigate_to("https://x.com")
    time.sleep(3)

    # Navigate to account
    navigate_to(f"https://x.com/{account}")
    time.sleep(5)

    log(f"Scrolling @{account} timeline...")

    for scroll_num in range(1, max_scrolls + 1):
        # Extract tweets via browser console
        inject_cookie_extract_script()

        # TODO: capture console output... this is hard with xdotool
        # For now, use a simpler approach: save page source and parse

        scroll_page()

        if scroll_num % 50 == 0:
            log(f"  scroll {scroll_num}: {len(tweets)} tweets so far")

    # Save
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(list(tweets.values()), f, ensure_ascii=False, indent=2)
    log(f"Done! {len(tweets)} tweets")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", "-a", required=True)
    parser.add_argument("--max-scrolls", "-m", type=int, default=1000)
    args = parser.parse_args()
    run_xdotool_scraper(args.account, args.max_scrolls)
