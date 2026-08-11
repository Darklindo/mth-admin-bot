#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Iniciando atualização do Jtzin Userbot..."
git pull --ff-only origin master

if [[ ! -x ".venv/bin/python" ]]; then
    echo "Criando ambiente virtual..."
    python -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python migrate_db.py
chmod +x watchdog.sh update_bot.sh

echo "Atualização concluída com sucesso."
echo "Para iniciar no tmux:"
echo "tmux kill-session -t mthadmin 2>/dev/null || true; tmux new-session -d -s mthadmin './watchdog.sh'; tmux attach -t mthadmin"
