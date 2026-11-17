#!/bin/bash
# Starts Offline Kit: the AI engine (Ollama) and the library/chat app, then
# opens it in the web browser. Closing this window stops everything.
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${OFFLINE_KIT_PORT:-8765}"
export OLLAMA_HOST="127.0.0.1:11435" OLLAMA_MODELS="$KIT/models"
# Keep the model in memory between questions for 30 minutes.
export OLLAMA_KEEP_ALIVE="30m"

clear
printf '\033[1mOffline Kit is starting...\033[0m\n\n'

PIDS=()
cleanup() {
  printf '\nStopping Offline Kit...\n'
  kill "${PIDS[@]}" 2>/dev/null
  wait 2>/dev/null
  # Ollama's model worker can outlive it; make sure it's gone so it frees memory.
  pkill -f "$KIT/ollama/" 2>/dev/null
}
trap cleanup EXIT INT TERM HUP

if ! curl -fs "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  "$KIT/ollama/ollama" serve >>"$KIT/ollama.log" 2>&1 &
  PIDS+=($!)
fi

"$KIT/venv/bin/python" "$KIT/kit/app/server.py" --port "$PORT" 2>>"$KIT/server.log" &
SERVER_PID=$!
PIDS+=($SERVER_PID)

for _ in $(seq 1 120); do
  curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 && break
  sleep 0.5
done

open "http://127.0.0.1:$PORT/"
printf 'Offline Kit is running at \033[1mhttp://127.0.0.1:%s\033[0m\n\n' "$PORT"
printf 'Leave this window open while you use it.\n'
printf 'To stop Offline Kit, close this window (or press Control-C).\n'
caffeinate -i -w "$SERVER_PID" &
wait "$SERVER_PID"
