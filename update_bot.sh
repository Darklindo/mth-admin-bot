#!/bin/bash

echo "🚀 Iniciando Atualização MTH ADMIN BOT V3.0 (Versão Blindada)..."

# Puxar atualizações do Git
git pull origin master

# Garantir que o ambiente virtual existe
if [ ! -d ".venv" ]; then
    echo "📦 Criando ambiente virtual..."
    python -m venv .venv
fi

# Instalar dependências
source .venv/bin/activate
pip install -r requirements.txt

# Rodar migração
python migrate_db.py

# Dar permissão ao watchdog
chmod +x watchdog.sh

echo "✅ Atualização concluída com sucesso!"
echo "🛡️ Para iniciar o bot com AUTO-RESTART no tmux, use:"
echo "tmux kill-session -t mthadmin 2>/dev/null; tmux new-session -d -s mthadmin './watchdog.sh'"
