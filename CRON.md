# 🐦 X Cron Collector — Operación

## ¿Qué hace?

Ejecuta cada **5 minutos** y garantiza que todas las cuentas configuradas estén **completamente descargadas**.

| Modo | Cuándo se usa | Qué hace |
|------|---------------|----------|
| **DEEP** | Cuenta nueva o incompleta | Selenium + Chrome scroll infinito hasta el final del timeline |
| **FAST** | Cuenta ya completa | Syndication endpoint (~100 tweets recientes) para novedades |

## Requisitos

1. **Firefox abierto** con sesión de X iniciada
2. **Google Chrome** instalado (para Selenium headless)
3. Python 3.11 + venv activado

## Instalación rápida

```bash
cd ~/projects/x-v2-collector
source venv/bin/activate

# Instalar Chrome driver automáticamente (webdriver-manager lo hace)
pip install -r requirements.txt

# Probar una ejecución manual
python src/cron_collector.py
```

## Configurar cron (cada 5 minutos)

```bash
# Editar crontab
crontab -e

# Agregar esta línea:
*/5 * * * * cd /home/cristian/projects/x-v2-collector && /home/cristian/projects/x-v2-collector/venv/bin/python src/cron_collector.py >> /home/cristian/projects/x-v2-collector/data/cron.log 2>&1
```

## Añadir una nueva cuenta

Editar `src/cron_collector.py` línea 42:

```python
ACCOUNTS = ["financialjuice", "Deltaone", "NUEVA_CUENTA"]
```

Al siguiente cron run (máx 5 min) empieza la colección de la nueva cuenta en modo **DEEP**.

## Archivos generados

| Archivo | Descripción |
|---------|-------------|
| `data/tweets/{account}_all.json` | Tweets totales acumulados |
| `data/tweets/{account}_last4years.json` | Solo últimos 4 años |
| `data/collection_state.json` | Estado: completo/incompleto, errores, último run |
| `data/cookies_cache.json` | Cache de cookies (2 min TTL) |
| `data/cron.log` | Log de ejecuciones |

## Comportamiento del estado

```json
{
  "financialjuice": {
    "screen_name": "financialjuice",
    "is_complete": false,      // ← true cuando se alcanza el final del timeline
    "total_collected": 2345,
    "last_tweet_id": "123...",
    "last_run": "2026-05-02T01:30:00+00:00",
    "deep_runs": 3,            // ← cuántas veces ha corrido en modo DEEP
    "error_count": 0,
    "errors": []
  }
}
```

## Cómo funciona la transición DEEP → FAST

1. **Primera ejecución** → Modo DEEP. Scroll 120 veces. Si hay más tweets quedan para la próxima corrida.
2. **Siguientes ejecuciones** → Modo DEEP continúa donde quedó (scroll acumulativo).
3. **Cuando un scroll devuelve 0 tweets nuevos** → Se marca `is_complete = true`.
4. **Próxima ejecución** → Modo FAST (syndication). Solo detecta novedades.
5. **Si FAST detecta tweets nuevos** → Vuelve a DEEP hasta completar otra vez.

## Monitoreo

```bash
# Ver estado actual
cat data/collection_state.json | python -m json.tool

# Ver tweets acumulados
ls -lh data/tweets/

# Ver último log
tail -f data/cron.log
```

## Troubleshooting

| Síntoma | Causa | Solución |
|---------|-------|----------|
| "Missing cookies" | Firefox no tiene sesión de X | Inicia sesión en x.com en Firefox |
| "Selenium error" | Chrome no está instalado | `sudo apt install google-chrome-stable` |
| "Rate limited" | Demasiados requests | Espera 15-30 min, el cron retoma solo |
| `is_complete` nunca cambia | Timeline muy largo | Aumentar `MAX_SCROLLS_PER_RUN` (línea 44) |

## Resetear una cuenta (forzar re-descarga)

```bash
# Eliminar tweets y estado de una cuenta
rm data/tweets/financialjuice_all.json
rm data/tweets/financialjuice_last4years.json

# Editar state.json y poner is_complete=false para esa cuenta
# O simplemente borrar la entrada de collection_state.json
```

La próxima ejecución hará DEEP de nuevo desde cero.
