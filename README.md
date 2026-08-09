# MTH Admin Bot

Bot de administração para Telegram, pensado para rodar no Termux.

## Recursos

- Anti-spam com limite configurável no código
- `/ban` e `/banperm`
- `/unban`
- `/kick`
- `/mute` e `/unmute`
- `/warn`
- `/blacklist` — apaga automaticamente novas mensagens de usuários bloqueados
- `/unblacklist`
- `/blacklistlist`
- `/antispam on|off`
- `/divulgar texto` — somente o OWNER_ID; envia aos grupos/canais que o bot registrou
- `/chats` — somente o dono
- SQLite para persistência
- Logs e tratamento de erros
- `.env` para manter o token fora do código

## Importante

O Bot API não fornece uma forma de listar todos os grupos/canais onde o bot está. Por isso, o projeto registra chats quando recebe atualizações deles, principalmente quando o bot entra/sai ou quando há atividade.

Para moderação, o bot precisa ser administrador e receber as permissões necessárias para apagar mensagens e restringir/banir usuários.

Para o anti-spam funcionar em grupos, o bot precisa receber as mensagens. Se o Privacy Mode estiver impedindo a entrega das mensagens necessárias, ajuste essa configuração no BotFather.

## Termux

```bash
pkg update -y
pkg install python git tmux -y

git clone SEU_REPOSITORIO
cd mth_admin_bot

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python bot.py
```

Para manter rodando:

```bash
termux-wake-lock
tmux new -s mthbot
source .venv/bin/activate
python bot.py
```

Para sair do tmux sem parar o bot:

`CTRL+B` e depois `D`

Para voltar:

```bash
tmux attach -t mthbot
```

## Segurança

O token do bot é uma credencial secreta. Nunca publique o token no código, GitHub, prints ou mensagens. Se um token já foi exposto, revogue-o no BotFather e gere outro antes de colocar o bot em produção.
