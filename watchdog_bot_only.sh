#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
mkdir -p logs

stop_requested=0
botapi_pid=""
delay=5

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

stop_bot() {
    local pid="${1:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$pid" 2>/dev/null || true
    fi
}

shutdown() {
    stop_requested=1
    log "Encerrando somente o Bot API..."
    stop_bot "$botapi_pid"
    if [[ -n "$botapi_pid" ]]; then
        wait "$botapi_pid" 2>/dev/null || true
    fi
    exit 0
}
trap shutdown INT TERM

start_botapi() {
    if [[ ! -x ".venv/bin/python" ]]; then
        log "Ambiente .venv ausente; execute update_bot.sh."
        return 1
    fi
    if [[ ! -f ".env.bot" ]]; then
        log "Arquivo .env.bot ausente; nova tentativa em ${delay}s."
        return 1
    fi
    log "Iniciando somente o Bot API; Userbot permanece desligado."
    .venv/bin/python -u bot.py >>logs/bot_api.log 2>&1 &
    botapi_pid=$!
    return 0
}

while [[ "$stop_requested" -eq 0 ]]; do
    if [[ -z "$botapi_pid" ]]; then
        if start_botapi; then
            delay=5
        else
            sleep "$delay"
            (( delay < 60 )) && delay=$((delay * 2))
        fi
    elif ! kill -0 "$botapi_pid" 2>/dev/null; then
        code=0
        wait "$botapi_pid" 2>/dev/null || code=$?
        botapi_pid=""
        if (( code == 130 || code == 143 )); then
            break
        fi
        log "Bot API encerrou com código ${code}; reinício em ${delay}s."
        sleep "$delay"
        (( delay < 60 )) && delay=$((delay * 2))
    else
        delay=5
    fi
    sleep 2
done
