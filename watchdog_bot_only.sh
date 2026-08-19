#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
mkdir -p logs
umask 077

LOCK_FILE="${HOME}/.cache/jtzin-bot-only.watchdog.pid"
mkdir -p "$(dirname "$LOCK_FILE")"
if [[ -f "$LOCK_FILE" ]]; then
    old_pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        printf '[%s] Supervisor Bot API-only já está ativo (pid=%s).\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$old_pid" >&2
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
printf '%s\n' "$$" >"$LOCK_FILE"

stop_requested=0
botapi_pid=""
delay=5
max_delay=60
stable_after=30
heartbeat_file="${SCRIPT_DIR}/data/bot_api.heartbeat"
heartbeat_stale_after=180
botapi_started_at=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

cleanup_lock() {
    if [[ -f "$LOCK_FILE" ]] && [[ "$(cat "$LOCK_FILE" 2>/dev/null || true)" == "$$" ]]; then
        rm -f "$LOCK_FILE"
    fi
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

heartbeat_is_healthy() {
    [[ -f "$heartbeat_file" ]] || return 1
    local modified now age
    modified="$(stat -c '%Y' "$heartbeat_file" 2>/dev/null || printf '0')"
    now="$(date +%s)"
    age=$((now - modified))
    (( age >= 0 && age <= heartbeat_stale_after ))
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
trap cleanup_lock EXIT

start_botapi() {
    if [[ ! -x ".venv/bin/python" ]]; then
        log "Ambiente .venv ausente; execute update_bot_only.sh."
        return 1
    fi
    if [[ ! -f ".env.bot" ]]; then
        log "Arquivo .env.bot ausente; nova tentativa em ${delay}s."
        return 1
    fi
    log "Iniciando somente o Bot API; Userbot permanece desligado."
    PYTHONUNBUFFERED=1 .venv/bin/python -u bot.py >>logs/bot_api.log 2>&1 &
    botapi_pid=$!
    return 0
}

while [[ "$stop_requested" -eq 0 ]]; do
    if [[ -z "$botapi_pid" ]]; then
        if start_botapi; then
            botapi_started_at=$(date +%s)
            log "Bot API iniciado (pid=${botapi_pid}); reconexão automática ativa."
            delay=5
            continue
        fi
        sleep "$delay"
        delay=$((delay * 2))
        (( delay > max_delay )) && delay=$max_delay
        continue
    fi

    if ! kill -0 "$botapi_pid" 2>/dev/null; then
        code=0
        wait "$botapi_pid" 2>/dev/null || code=$?
        botapi_pid=""
        if (( code == 130 || code == 143 )); then
            break
        fi
        now=$(date +%s)
        uptime=$((now - botapi_started_at))
        if (( uptime >= stable_after )); then
            delay=5
        else
            delay=$((delay * 2))
            (( delay > max_delay )) && delay=$max_delay
        fi
        log "Bot API encerrou com código ${code} após ${uptime}s; reinício em ${delay}s."
        sleep "$delay"
        continue
    fi

    now=$(date +%s)
    uptime=$((now - botapi_started_at))
    if (( uptime >= heartbeat_stale_after )) && ! heartbeat_is_healthy; then
        log "Heartbeat do Bot API está ausente ou obsoleto há mais de ${heartbeat_stale_after}s; reinício preventivo."
        stop_bot "$botapi_pid"
        wait "$botapi_pid" 2>/dev/null || true
        botapi_pid=""
        delay=5
        sleep "$delay"
        continue
    fi

    sleep 2
done
