#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Iniciando atualização do Jtzin Userbot..."

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Há alterações locais não commitadas. A atualização foi interrompida para evitar sobrescrever trabalho local."
    git status --short
    exit 2
fi

if [[ ! -f "requirements.txt" || ! -f "migrate_db.py" || ! -f "bot_v2.py" ]]; then
    echo "Arquivos essenciais ausentes; atualização abortada por segurança."
    exit 3
fi

git pull --ff-only origin master

PYTHON_BIN="${PYTHON_BIN:-python}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Interpretador Python não encontrado: $PYTHON_BIN"
    exit 4
fi

if [[ ! -x ".venv/bin/python" ]]; then
    echo "Criando ambiente virtual..."
    "$PYTHON_BIN" -m venv .venv
fi

PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/python -m pip install --upgrade pip
PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/python -m pip install --requirement requirements.txt
.venv/bin/python -m py_compile bot_v2.py migrate_db.py
.venv/bin/python migrate_db.py
chmod +x watchdog.sh update_bot.sh

echo "Atualização concluída com sucesso."
echo "Para iniciar no tmux:"
echo "tmux kill-session -t mthadmin 2>/dev/null || true; tmux new-session -d -s mthadmin './watchdog.sh'; tmux attach -t mthadmin"
