#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

while true; do
    if [[ ! -x ".venv/bin/python" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ambiente .venv ausente. Execute update_bot.sh primeiro."
        exit 1
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando Jtzin Userbot..."
    .venv/bin/python -u bot_v2.py
    exit_code=$?

    # Ctrl+C/SIGTERM são encerramentos intencionais, não devem gerar loop.
    if [[ "$exit_code" -eq 130 || "$exit_code" -eq 143 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Userbot encerrado manualmente (código $exit_code)."
        exit "$exit_code"
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Userbot caiu com código $exit_code. Reiniciando em 5 segundos..."
    sleep 5
done
