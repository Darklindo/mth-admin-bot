#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

child_pid=""
stop_requested=0
restart_delay=5

forward_stop() {
    stop_requested=1
    if [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null; then
        kill -TERM "$child_pid" 2>/dev/null || true
    fi
}
trap forward_stop INT TERM

while true; do
    if [[ ! -x ".venv/bin/python" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ambiente .venv ausente. Execute update_bot.sh primeiro."
        exit 1
    fi

    started_at=$(date +%s)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando Jtzin Userbot..."
    .venv/bin/python -u bot_v2.py &
    child_pid=$!
    wait "$child_pid"
    exit_code=$?
    child_pid=""
    finished_at=$(date +%s)
    runtime=$((finished_at - started_at))

    if [[ "$stop_requested" -eq 1 || "$exit_code" -eq 130 || "$exit_code" -eq 143 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Userbot encerrado manualmente (código $exit_code)."
        exit "$exit_code"
    fi

    # Uma sessão estável reinicia com atraso curto; falhas repetidas aumentam
    # o atraso até 60 s para evitar consumo excessivo de CPU e RPCs.
    if (( runtime >= 60 )); then
        restart_delay=5
    else
        restart_delay=$((restart_delay * 2))
        (( restart_delay > 60 )) && restart_delay=60
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Userbot saiu com código $exit_code após ${runtime}s. Reiniciando em ${restart_delay}s..."
    sleep "$restart_delay"
done
