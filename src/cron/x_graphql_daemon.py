#!/usr/bin/env python3
"""
X GraphQL Scraper — Daemon Cron Runner
=======================================
Ejecuta el scraper cada 30 minutos indefinidamente.
NO se detiene. SIEMPRE reintenta. NUNCA rinde.
"""

import sys
import time
import random
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.parent.absolute()
SCRAPER_SCRIPT = BASE_DIR / "src" / "engine" / "x_graphql_scraper.py"
PYTHON = BASE_DIR / "venv" / "bin" / "python"
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

INTERVAL_MINUTES = 30
MIN_SLEEP = INTERVAL_MINUTES * 60

running = True


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "cron_daemon.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def signal_handler(sig, frame):
    global running
    log("[!] SIGTERM/SIGINT received — shutting down gracefully")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def run_scraper() -> bool:
    """Ejecuta el scraper. Retorna True si tuvo éxito."""
    log("[▶] Starting scraper run...")
    try:
        result = subprocess.run(
            [str(PYTHON), str(SCRAPER_SCRIPT)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max por run
        )
        
        # Log stdout
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                log(f"  [out] {line}")
        
        # Log stderr
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                log(f"  [err] {line}")
        
        success = result.returncode == 0 and "found=" in result.stdout
        if success:
            log("[✓] Scraper completed successfully")
        else:
            log(f"[!] Scraper failed (exit={result.returncode})")
        
        return success
    except subprocess.TimeoutExpired:
        log("[!] Scraper timed out after 5 minutes")
        return False
    except Exception as e:
        log(f"[!] Scraper exception: {e}")
        return False


def main():
    log("=" * 60)
    log("X GraphQL Cron Daemon STARTED")
    log(f"Interval: {INTERVAL_MINUTES} minutes")
    log(f"Targets: financialjuice, Deltaone")
    log(f"Log: {LOG_DIR / 'cron_daemon.log'}")
    log("=" * 60)

    while running:
        start = time.time()
        
        success = run_scraper()
        
        if not success:
            log("[!] Run failed — will retry in 5 minutes")
            retry_sleep = 5 * 60
        else:
            elapsed = time.time() - start
            remaining = max(MIN_SLEEP - elapsed, 0)
            jitter = random.uniform(0, 60)
            retry_sleep = remaining + jitter
        
        log(f"[⏳] Sleeping {retry_sleep / 60:.1f} minutes until next run...")
        
        # Sleep in small chunks to respond to signals
        slept = 0
        while slept < retry_sleep and running:
            chunk = min(10, retry_sleep - slept)
            time.sleep(chunk)
            slept += chunk

    log("[✗] Daemon stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()
