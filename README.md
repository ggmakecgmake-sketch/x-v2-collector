# X-v2 Collector

> Standalone X/Twitter scraper — no API key needed.  
> Hybrid scraper: `twikit` (primary) + `Playwright` (fallback).

This is a **drop-in replacement** for the paid X API v2 collector. It scrapes timelines of tracked accounts and publishes tweets to the **same Redis Stream** (`tweets:raw`) that the existing v1 collector uses.

---

## How it works

1. **Login** to X with a burner account (or reuse saved cookies)
2. **Poll** each target account every **45–55 seconds**
3. **Dedup** tweets via in-memory cache (last 200 per account)
4. **Publish** to Redis Stream `tweets:raw` → compatible with existing AI Processor
5. **Expose** a health endpoint at `:8001/health`
6. **Fallback**: if `twikit` fails 3 times in a row → switch to `Playwright` (real browser)

---

## Quick start

### Local (no Docker)

```bash
# 1. Install
cd x-v2-collector
cp .env.example .env
# Edit .env with your TWITTER_USERNAME, TWITTER_PASSWORD, and REDIS_HOST

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Run
python -m src.main
```

### Docker Compose

```yaml
x-v2-collector:
  image: ghcr.io/ggmakecgmake-sketch/x-v2-collector:latest
  environment:
    - REDIS_HOST=redis
    - TWITTER_USERNAME=${TWITTER_USERNAME}
    - TWITTER_PASSWORD=${TWITTER_PASSWORD}
  volumes:
    - x_v2_cookies:/app/data
  ports:
    - "8001:8001"
```

---

## Architecture

```
┌─────────────────────────────┐
│      x-v2-collector         │
│  ┌─────────┐  ┌──────────┐  │
│  │ Twikit  │  │Playwright│  │
│  │(primary)│──│(fallback)│  │
│  └────┬────┘  └────┬─────┘  │
│       │            │         │
│  ┌────┴────────────┴────┐    │
│  │  Deduplicator +      │    │
│  │  Rate Limiter        │    │
│  └──────────┬───────────┘     │
│             │                  │
│      ┌──────▼──────┐          │
│      │ Redis       │          │
│      │ tweets:raw  │          │
│      └─────────────┘          │
│  ┌────────┐                   │
│  │:8001   │ /health /metrics  │
│  └────────┘                   │
└─────────────────────────────┘
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TWITTER_USERNAME` | — | Burner account username |
| `TWITTER_PASSWORD` | — | Burner account password |
| `TWITTER_EMAIL` | — | Account email (for 2FA/phone checks) |
| `X_ACCOUNTS_TO_TRACK` | `Deltaone,financialjuice` | Comma-separated usernames |
| `REDIS_HOST` | `localhost` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `POLL_INTERVAL_MIN` | `45` | Minimum seconds between polls |
| `POLL_INTERVAL_MAX` | `55` | Maximum seconds between polls |
| `TWIKIT_FAILURE_THRESHOLD` | `3` | Failures before Playwright fallback |
| `HEALTH_PORT` | `8001` | Health API port |
| `COOKIES_PATH` | `/app/data/cookies.json` | Session persistence path |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Health endpoint

```bash
curl http://localhost:8001/health
```

```json
{
  "status": "healthy",
  "service": "X-v2 Collector",
  "version": "0.1.0",
  "current_engine": "twikit",
  "last_fetch": "2026-05-01T12:34:56.789012",
  "tweets_total": 42,
  "timestamp": "2026-05-01T12:35:00.123456"
}
```

---

## License

MIT / Proprietary for private trading use.

---

## Built for

The [`tweet-capture`](https://github.com/ggmakecgmake-sketch/tweet-capture) trading intelligence pipeline.
