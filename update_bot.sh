#!/bin/bash

echo "[+] Iniciando atualização para MTH Admin Bot V2..."

# 1. Ativar ambiente virtual
if [ -d ".venv" ]; then
    source .venv/bin/activate
else
    echo "[!] Ambiente virtual não encontrado. Criando..."
    python -m venv .venv
    source .venv/bin/activate
fi

# 2. Instalar novas dependências (se houver)
pip install python-telegram-bot==22.8 python-dotenv

# 3. Rodar migração do banco de dados
echo "[+] Migrando banco de dados..."
python migrate_db.py

# 4. Substituir o bot antigo pelo novo (opcional, mantendo backup)
if [ -f "bot.py" ]; then
    mv bot.py bot_v1_backup.py
fi
cp bot_v2.py bot.py

echo "[+] Atualização concluída com sucesso!"
echo "[+] Agora você pode rodar o bot normalmente com: python bot.py"
