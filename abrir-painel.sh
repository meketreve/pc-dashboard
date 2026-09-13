#!/usr/bin/env bash
# Abre o Painel do PC em tela cheia no monitor configurado (config.json -> "monitor", padrao DP-0).
set -u
URL="http://127.0.0.1:8787"
OUTPUT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("monitor") or "DP-0")' \
         "$HOME/.config/pc-dashboard/config.json" 2>/dev/null || echo DP-0)
PROFILE="$HOME/.config/pc-dashboard/chrome"

systemctl --user start pc-dashboard.service
for _ in $(seq 30); do curl -fs -o /dev/null "$URL/api/stats" && break; sleep 1; done

GEO=$(xrandr --query | awk -v o="$OUTPUT" '$1==o && $2=="connected" {for(i=3;i<=NF;i++) if ($i ~ /^[0-9]+x[0-9]+\+[0-9]+\+[0-9]+$/) {print $i; exit}}')
if [[ -z "$GEO" ]]; then
  notify-send "Painel do PC" "Monitor $OUTPUT não encontrado." 2>/dev/null
  exit 1
fi
W=${GEO%%x*}; REST=${GEO#*x}; H=${REST%%+*}; REST=${REST#*+}; X=${REST%%+*}; Y=${REST#*+}

# Ja aberto? so reposiciona. (Olha so a classe da janela: o titulo de outra janela pode ter "pc-dashboard".)
find_win() { wmctrl -lx | awk '$3 ~ /^[^ ]*pc-dashboard$/ {print $1; exit}'; }
if [[ -z "$(find_win)" ]]; then
  google-chrome --user-data-dir="$PROFILE" --class=pc-dashboard --app="$URL" \
    --window-position="$X,$Y" --window-size="$W,$H" \
    --no-first-run --no-default-browser-check --password-store=basic \
    --disable-features=Translate --noerrdialogs --disable-session-crashed-bubble \
    >/dev/null 2>&1 &
  for _ in $(seq 20); do [[ -n "$(find_win)" ]] && break; sleep 0.5; done
fi
WIN=$(find_win)
[[ -n "$WIN" ]] || exit 1
wmctrl -i -r "$WIN" -b remove,fullscreen,maximized_vert,maximized_horz
wmctrl -i -r "$WIN" -e "0,$X,$Y,$W,$H"
sleep 0.5
wmctrl -i -r "$WIN" -b add,fullscreen
