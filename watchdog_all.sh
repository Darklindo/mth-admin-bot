#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
mkdir -p logs

userbot_pid=""
botapi_pid=""
stop_requested=0
userbot_delay=5
botapi_delay=5

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

stop_child() {
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
    log "Encerrando Bot API e Userbot..."
    stop_child "$userbot_pid"
    stop_child "$botapi_pid"
    wait "$userbot_pid" 2>/dev/null || true
    wait "$botapi_pid" 2>/dev/null || true
    exit 0
}
trap shutdown INT TERM

start_userbot() {
    if [[ ! -x ".venv/bin/python" ]]; then
        log "Ambiente .venv ausente; execute update_bot.sh."
        return 1
    fi
    log "Iniciando Userbot Telethon..."
    .venv/bin/python -u bot_v2.py >>logs/userbot.log 2>&1 &
    userbot_pid=$!
}

start_botapi() {
    if [[ ! -x ".venv/bin/python" ]]; then
        log "Ambiente .venv ausente; Bot API aguardará update_bot.sh."
        return 1
    fi
    if [[ ! -f ".env.bot" ]]; then
        log "Bot API aguardando .env.bot com token novo do @BotFather."
        return 1
    fi
    log "Iniciando Bot API..."
    .venv/bin/python -u bot.py >>logs/bot_api.log 2>&1 &
    botapi_pid=$!
}

if ! start_userbot; then
    exit 1
fi
start_botapi || true

while [[ "$stop_requested" -eq 0 ]]; do
    if [[ -n "$userbot_pid" ]] && ! kill -0 "$userbot_pid" 2>/dev/null; then
        userbot_code=0
        wait "$userbot_pid" 2>/dev/null || userbot_code=$?
        userbot_pid=""
        if (( userbot_code == 130 || userbot_code == 143 )); then
            break
        fi
        log "Userbot encerrou com código ${userbot_code}; reinício em ${userbot_delay}s."
        sleep "$userbot_delay"
        (( userbot_delay < 60 )) && userbot_delay=$((userbot_delay * 2))
        start_userbot || break
    else
        userbot_delay=5
    fi

    if [[ -n "$botapi_pid" ]] && ! kill -0 "$botapi_pid" 2>/dev/null; then
        botapi_code=0
        wait "$botapi_pid" 2>/dev/null || botapi_code=$?
        botapi_pid=""
        log "Bot API encerrou com código ${botapi_code}; reinício em ${botapi_delay}s."
        sleep "$botapi_delay"
        (( botapi_delay < 60 )) && botapi_delay=$((botapi_delay * 2))
        start_botapi || true
    elif [[ -z "$botapi_pid" ]]; then
        start_botapi || true
        botapi_delay=5
    else
        botapi_delay=5
    fi
    sleep 2
done
