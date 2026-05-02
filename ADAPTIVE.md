# 🔄 Adaptive Cron Collector — Auto-healing X Scraper

## Filosofía

**Si algo no funciona, cambiar. Si sigue sin funcionar, cambiar otra vez. Hasta que funcione.**

Cada 5 minutos el sistema audita sus resultados y **auto-adapta** la estrategia de scraping.

## Estrategias (rotación automática)

| # | Estrategia | Qué hace | Cuándo se prueba |
|---|-----------|---------|-----------------|
| A | **syndication** | Endpoint público `syndication.twitter.com`, sin browser | Primera siempre (rápido, ~100 tweets) |
| B | **requests_html** | HTTP directo con cookies a `x.com/{user}`, parse HTML | Si syndication da 0 |
| C | **selenium_cookies** | Chrome headless + cookies de Firefox inyectadas | Si requests da 0 (2 runs seguidos) |
| D | **playwright_stealth** | Playwright headless + cookies + stealth | Si selenium da 0 (3 runs seguidos) |

## Reglas de rotación

```
1. NUEVA CUENTA → Prueba A (syndication)
2. Si A da 0 tweets → Prueba B (requests_html) en siguiente run
3. Si B da 0 tweets → Prueba C (selenium) en siguiente run  
4. Si C da 0 tweets → Prueba D (playwright) en siguiente run
5. Si D da 0 tweets → Vuelve a A con log de "stuck for 6 runs"
6. Si ALGUNA da >0 tweets pero 0 nuevos → Marca COMPLETE
```

## Estado persistente (`data/adaptive_state.json`)

```json
{
  "financialjuice": {
    "screen_name": "financialjuice",
    "is_complete": false,
    "total_collected": 156,
    "last_tweet_id": "1234567890",
    "last_run": "2026-05-02T03:00:00+00:00",
    "last_strategy": "selenium_cookies",
    "strategy_history": ["syndication", "requests_html", "selenium_cookies"],
    "deep_runs": 3,
    "consecutive_no_progress": 0,
    "error_count": 0,
    "errors": []
  }
}
```

| Campo | Significado |
|-------|-------------|
| `is_complete` | True = timeline completamente descargado |
| `last_strategy` | Qué estrategia usó en el último run |
| `strategy_history` | Últimas 20 estrategias probadas |
| `consecutive_no_progress` | Cuántas veces seguidas sin añadir tweets nuevos |
| `deep_runs` | Cuántas corridas en modo DEEP (no FAST) |

## Flujo completo

```
Every 5 min:
  ├─ Read Firefox cookies (cache 2 min)
  ├─ For each account:
  │   ├─ Audit: How many tweets do I have?
  │   ├─ Pick strategy based on history
  │   ├─ Execute strategy
  │   ├─ Merge with existing database
  │   ├─ Evaluate:
  │   │   ├─ Added > 0  → Reset counters, save
  │   │   ├─ Added = 0 but got data → Maybe COMPLETE
  │   │   └─ Added = 0, no data → Increment no_progress
  │   └─ If no_progress >= 6 → FORCE rotation next run
  └─ Save state + tweets
```

## Logs

```bash
# Ver log en vivo
tail -f ~/projects/x-v2-collector/data/adaptive_cron.log

# Ver últimas 50 líneas
tail -50 ~/projects/x-v2-collector/data/adaptive_cron.log
```

Formato de log:
```
[2026-05-02 03:00:00] Adaptive Cron Collector — 2026-05-02T03:00:00+00:00
[2026-05-02 03:00:02] Fresh cookies: 14
[2026-05-02 03:00:03] [@financialjuice] strategy=selenium_cookies | runs=3 | progress_streak=0 | total=156
[2026-05-02 03:00:45]     scroll  15: +12 new | total  168 | streak=0
[2026-05-02 03:00:50]   [+] Added 12 new tweets
[2026-05-02 03:00:50]   [+] Before: 156 | Added: 12 | Total: 168
[2026-05-02 03:00:51] [@Deltaone] strategy=syndication | runs=0 | progress_streak=2 | total=0
[2026-05-02 03:00:52]   [syndication] 0 tweets
[2026-05-02 03:00:52]   [!] No tweets from syndication (errors=3, no_progress=3)
```

## Añadir nueva cuenta

Editar `src/adaptive_cron_collector.py` línea 60:
```python
ACCOUNTS = ["financialjuice", "Deltaone", "NUEVA_CUENTA"]
```

La cuenta empieza en modo `syndication` en el siguiente run (máx 5 min).

## Resetear cuenta (forzar re-descarga)

```bash
rm ~/projects/x-v2-collector/data/tweets/{account}_all.json
rm ~/projects/x-v2-collector/data/tweets/{account}_last4years.json

# Editar state y poner is_complete=false
# O simplemente borrar la entrada de adaptive_state.json
```

## Troubleshooting

| Síntoma | Causa | Solución |
|---------|-------|----------|
| `Missing cookies` | Firefox no logueado en X | Abre x.com en Firefox e inicia sesión |
| `All strategies tried, 0 tweets` | X bloqueó la cuenta/IP | Espera 30-60 min, el cron sigue intentando |
| `selenium_cookies error` | Chrome no instalado | `sudo apt install google-chrome-stable` |
| `playwright_stealth error` | Playwright no disponible | No importa, el sistema rota a selenium |
| `stuck for 6 runs` | Nada funciona para esa cuenta | Revisa si la cuenta existe o es privada |
| Log crece muy rápido | Muchos runs con debug | `logrotate` o truncar: `> data/adaptive_cron.log` |

## Arquitectura

```
┌─────────────────────────────────────────┐
│         Cron (every 5 min)              │
│  ┌──────────────────────────────────┐   │
│  │  Firefox Cookie Extractor       │   │
│  │  (reads cookies.sqlite live)   │   │
│  └──────────────────────────────────┘   │
│                  ↓                        │
│  ┌──────────────────────────────────┐   │
│  │  Adaptive Strategy Picker       │   │
│  │  (audits state, picks next)      │   │
│  └──────────────────────────────────┘   │
│                  ↓                        │
│         ┌──────┬──────┬──────┐          │
│         │  A   │  B   │  C   │          │
│         │ synd │ reqs │ sel  │          │
│         │      │      │      │          │
│         └──────┴──────┴──────┘          │
│                  ↓                        │
│  ┌──────────────────────────────────┐   │
│  │  Tweet Database (JSON)          │   │
│  │  data/tweets/{account}_all.json │   │
│  └──────────────────────────────────┘   │
│                  ↓                        │
│  ┌──────────────────────────────────┐   │
│  │  State File (JSON)              │   │
│  │  data/adaptive_state.json       │   │
│  └──────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

## Crontab

```cron
# X/Twitter Adaptive Collector — self-healing every 5 min
*/5 * * * * cd /home/cristian/projects/x-v2-collector && /home/cristian/projects/x-v2-collector/venv/bin/python src/adaptive_cron_collector.py >> /home/cristian/projects/x-v2-collector/data/adaptive_cron.log 2>&1
```
