#!/bin/bash

# MTH ADMIN BOT - WATCHDOG SCRIPT
# Este script monitora o bot e o reinicia automaticamente em caso de queda.

while true; do
    echo "[$(date)] Iniciando MTH ADMIN BOT..."
    source .venv/bin/activate
    python bot_v2.py
    
    EXIT_CODE=$?
    echo "[$(date)] Bot encerrou com código $EXIT_CODE. Reiniciando em 5 segundos..."
    sleep 5
done
