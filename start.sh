#!/bin/bash
# Starts Offline Kit: the AI engine (Ollama) and the library/chat app, then
# opens it in the web browser. Closing this window stops everything.
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${OFFLINE_KIT_PORT:-8765}"
URL="http://127.0.0.1:$PORT"
export OLLAMA_HOST="127.0.0.1:11435" OLLAMA_MODELS="$KIT/models"
# Keep the model in memory between questions for 30 minutes.
export OLLAMA_KEEP_ALIVE="30m"

# Already running (for example, the icon was double-clicked twice)? Just show it,
# and leave the copy that's running alone.
if curl -fs --max-time 3 "$URL/api/status" >/dev/null 2>&1; then
  open "$URL/"
  printf 'Offline Kit is already running. It has been opened in your web browser.\n'
  printf 'You can close this window.\n'
  exit 0
fi

for f in venv/bin/python ollama/ollama kit/app/server.py; do
  if [ ! -e "$KIT/$f" ]; then
    printf 'Offline Kit is not completely installed (%s is missing).\n' "$f"
    printf 'Connect to the internet and run the installer again. See README.md in %s/kit\n' "$KIT"
    exit 1
  fi
done

clear
printf '\033[1mOffline Kit is starting...\033[0m\n\n'

# Keep the logs from growing forever.
for f in "$KIT/ollama.log" "$KIT/server.log"; do
  if [ -f "$f" ] && [ "$(wc -c <"$f")" -gt 5000000 ]; then mv -f "$f" "$f.old"; fi
done

PIDS=()
STOPPED=""
cleanup() {
  [ -n "$STOPPED" ] && return
  STOPPED=1
  printf '\nStopping Offline Kit...\n'
  [ "${#PIDS[@]}" -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null
  wait 2>/dev/null
  # Ollama's model worker can outlive it; make sure it's gone so it frees memory.
  pkill -f "$KIT/ollama/" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if ! curl -fs --max-time 3 "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  "$KIT/ollama/ollama" serve >>"$KIT/ollama.log" 2>&1 &
  PIDS+=($!)
fi

"$KIT/venv/bin/python" "$KIT/kit/app/server.py" --port "$PORT" 2>>"$KIT/server.log" &
SERVER_PID=$!
PIDS+=($SERVER_PID)

READY=""
for _ in $(seq 1 360); do
  if curl -fs --max-time 3 "$URL/api/status" >/dev/null 2>&1; then READY=1; break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 0.5
done

if [ -z "$READY" ]; then
  printf '\033[31mOffline Kit could not start.\033[0m The last lines of its log:\n\n'
  tail -n 15 "$KIT/server.log" 2>/dev/null
  printf '\nTry closing this window and opening Offline Kit again. If that does not help,\n'
  printf 'connect to the internet and run the installer again.\n'
  exit 1
fi

open "$URL/"
printf 'Offline Kit is running at \033[1m%s\033[0m\n\n' "$URL"
printf 'If the page did not open, type that address into your web browser.\n'
printf 'Leave this window open while you use it.\n'
printf 'To stop Offline Kit, close this window (or press Control-C).\n'
wait "$SERVER_PID"
