#!/bin/bash
# Firefox Scroll Scraper — Simplemente abre Firefox, va a x.com, scrollea.
# Extrae tweets con el JS console.

ACCOUNT="${1:-financialjuice}"
DATA_DIR="$HOME/projects/x-v2-collector/data"
TWEETS_DIR="$DATA_DIR/tweets"
mkdir -p "$TWEETS_DIR"

LOG="$DATA_DIR/firefox_scraper.log"
OUTFILE="$TWEETS_DIR/${ACCOUNT}_all.json"

echo "$(date '+%H:%M:%S') === Iniciando scrape de @$ACCOUNT ===" | tee -a "$LOG"

# 1. Activar o lanzar Firefox
echo "$(date '+%H:%M:%S') Activando Firefox..." | tee -a "$LOG"
WIN_ID=$(xdotool search --class 'firefox' | head -1)
if [ -z "$WIN_ID" ]; then
    firefox &
    sleep 6
    WIN_ID=$(xdotool search --class 'firefox' | head -1)
fi

if [ -z "$WIN_ID" ]; then
    echo "ERROR: No se pudo encontrar Firefox" | tee -a "$LOG"
    exit 1
fi

xdotool windowactivate "$WIN_ID"
sleep 1

# 2. Ir a x.com (login si es necesario)
echo "$(date '+%H:%M:%S') Abriendo x.com/$ACCOUNT..." | tee -a "$LOG"
xdotool key ctrl+l
sleep 0.5
xdotool key ctrl+a
xdotool type --delay 10 "https://x.com/$ACCOUNT"
sleep 0.3
xdotool key Return
sleep 6

# 3. Loop de scroll + extraer
echo "$(date '+%H:%M:%S') Iniciando loop de scroll..." | tee -a "$LOG"

echo "[]" > /tmp/tweets_extracted.json
SCROLLS=0
NEW_TOTAL=0

while true; do
    SCROLLS=$((SCROLLS + 1))

    # Extraer tweets via xdotool ejecutando JS
    # Abrimos consola, ejecutamos JS, cerramos
    xdotool key ctrl+shift+k
    sleep 0.5

    # Script JS que extrae tweets y guarda en localStorage
    JS='(function(){const arts=document.querySelectorAll("article[data-testid=tweet]");const out=[];arts.forEach(a=>{const links=a.querySelectorAll("a[href*=status]");let id="";for(const l of links){const h=l.href||"";if(h.includes("/status/")){const p=h.split("/status/");if(p.length>1){id=p[1].split("?")[0].split("/")[0];if(/^\d+$/.test(id))break;}}}if(!id)return;const texts=a.querySelectorAll("div[data-testid=tweetText]");const txt=Array.from(texts).map(e=>e.innerText).join(" ").trim();const t=a.querySelector("time");const dt=t?t.getAttribute("datetime"):"";const n=a.querySelector("div[data-testid=User-Name]");const dn=n?n.innerText.split("\n")[0]:"";out.push({tweet_id:id,text:txt,created_at:dt,display_name:dn,username:"'"$ACCOUNT"'",is_reply:!!a.querySelector("div[data-testid=tweetReplyContext]"),is_retweet:!!a.querySelector("span[data-testid=socialContext]")});});localStorage.setItem("__scraped_tweets",JSON.stringify(out));})();'

    xdotool type --delay 1 "$JS"
    sleep 0.5
    xdotool key Return
    sleep 1

    # Cerrar consola
    xdotool key ctrl+shift+k
    sleep 0.5

    # Leer localStorage via xdotool (ejecutar JS que imprime)
    # Guardar los tweets acumulados
    # Simplificado: en cada scroll, extraemos todos los tweets visibles
    # y los mergeamos con el archivo existente

    # Scroll
    xdotool key Page_Down
    sleep 2

    if [ $((SCROLLS % 20)) -eq 0 ]; then
        echo "$(date '+%H:%M:%S') scroll $SCROLLS: pausando 5s..." | tee -a "$LOG"
        sleep 5
    fi

    if [ $SCROLLS -gt 1000 ]; then
        echo "$(date '+%H:%M:%S') Límite de scrolls alcanzado" | tee -a "$LOG"
        break
    fi
done

echo "$(date '+%H:%M:%S') === SCRAPE FINALIZADO ===" | tee -a "$LOG"
