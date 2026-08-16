#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Iniciando atualização do Jtzin Bot API-only..."

# Arquivos locais privados não entram no diff; qualquer alteração pública relevante
# continua bloqueando o pull para evitar perda de trabalho.
if ! git diff --quiet -- bot.py watchdog_bot_only.sh requirements.txt .gitignore .env.bot.example || \
   ! git diff --cached --quiet -- bot.py watchdog_bot_only.sh requirements.txt .gitignore .env.bot.example; then
    echo "Há alterações locais nos arquivos do Bot API. Atualização interrompida por segurança."
    git status --short
    exit 2
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
.venv/bin/python -m py_compile bot.py
chmod +x watchdog_bot_only.sh update_bot_only.sh

if [[ ! -f ".env.bot" ]]; then
    echo "Aviso: .env.bot ainda não existe; crie-o localmente antes de iniciar o Bot API."
else
    chmod 600 .env.bot
fi

echo "Atualização Bot API-only concluída."
echo "Para iniciar: tmux kill-session -t jtzin 2>/dev/null || true; termux-wake-lock; tmux new-session -d -s jtzin './watchdog_bot_only.sh'"
