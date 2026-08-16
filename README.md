# Jtzin Bot API + Userbot V9.0

Projeto de administração para Telegram com dois processos independentes no Termux: um **bot comum da Bot API**, limitado a três funções, e um **Userbot Telethon**, destinado exclusivamente à conta proprietária JT Cacique.

> **Segurança:** o token do Bot API nunca deve ser publicado, versionado ou enviado em mensagens. Como o token anterior foi exposto, ele deve ser revogado no `@BotFather` e substituído por um token novo antes da operação.

## Arquitetura

| Processo | Arquivo | Prefixo | Acesso | Banco |
|---|---|---|---|---|
| Bot comum | `bot.py` | `/` | Administradores dos grupos; `.allban` exige o proprietário | `data/bot_api.db` |
| Userbot | `bot_v2.py` | `.` | Somente o `OWNER_ID` configurado | `data/bot.db` |

O Bot API e o Userbot não compartilham sessão, token, banco nem estado de autorização. O Userbot não possui mais subproprietários ou autorização delegada; os comandos `.autorizar`, `.desautorizar` e `.listauth` não fazem parte da superfície operacional da V9.0.

## Funções do Bot API

O bot comum possui somente estas funções:

| Comando | Função | Permissão |
|---|---|---|
| `/blacklist` | Cadastra o alvo na blacklist local do grupo e apaga as mensagens dele enquanto o bot estiver ativo | Administrador do grupo |
| `/banperm` | Bane permanentemente o alvo no grupo atual | Administrador do grupo |
| `/allban` | Registra o alvo na blacklist global e tenta bani-lo em todos os grupos conhecidos | Somente `OWNER_ID` |

O alvo pode ser informado respondendo à mensagem dele ou usando um ID numérico ou username previamente conhecido pelo bot. O banco mantém a lista por chat e o Bot API possui cache próprio. O bot precisa estar presente e ter permissões administrativas para apagar mensagens ou restringir usuários; o `/allban` só pode alcançar grupos nos quais ele esteja presente e autorizado.

As respostas e os comandos são removidos automaticamente após alguns segundos. O bot é deliberadamente mínimo: não contém o restante dos recursos do Userbot, não aceita autorização de terceiros e não possui subproprietários.

## Funções do Userbot

O Userbot continua contendo a implementação avançada de moderação, diagnósticos, AntiBlack, AntiLink, AntiSpam, purge, perfil e demais recursos históricos. Entretanto, **somente o ID definido em `OWNER_ID` pode executar comandos**. A conta autenticada pelo arquivo de sessão deve ser a conta JT Cacique correspondente ao proprietário.

Os comandos do Userbot usam o prefixo `.`. Os sete comandos que mantêm o prefixo `.jt` para evitar conflito com o Group Help são `.jtban`, `.jtmute`, `.jtdel`, `.jtdelwarn`, `.jtpurge`, `.jtpurgeall` e `.jtwarn`.

A autorização para enviar links em grupos, quando habilitada pelo proprietário, é uma configuração de antilink e não concede acesso aos comandos do Userbot.

## Instalação no Termux

```bash
pkg update -y
pkg install python git tmux -y
git clone https://github.com/Darklindo/mth-admin-bot.git mth-admin
cd mth-admin
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
cp .env.bot.example .env.bot
nano .env.bot
python3 migrate_db.py
```

O arquivo `.env` do Userbot deve conter somente as credenciais MTProto e o proprietário único:

```dotenv
API_ID=SEU_API_ID
API_HASH=SEU_API_HASH
OWNER_ID=SEU_ID_JT_CACIQUE
```

O arquivo `.env.bot` deve conter um token novo do BotFather e o mesmo proprietário:

```dotenv
BOT_TOKEN=COLE_UM_TOKEN_NOVO_DO_BOTFATHER
OWNER_ID=SEU_ID_JT_CACIQUE
```

Nunca substitua os valores de exemplo por credenciais dentro de arquivos versionados. `.env`, `.env.bot`, sessões, bancos, logs e arquivos de pesquisa são ignorados pelo Git.

## Inicialização dos dois processos

```bash
cd ~/mth-admin
chmod +x update_bot.sh watchdog.sh watchdog_all.sh
termux-wake-lock
tmux new-session -d -s jtzin './watchdog_all.sh'
tmux attach -t jtzin
```

O `watchdog_all.sh` inicia o Bot API e o Userbot como processos separados, grava logs independentes em `logs/bot_api.log` e `logs/userbot.log` e aplica backoff de reinício individual. Se `.env.bot` ainda não existir, o Userbot pode iniciar enquanto o Bot API aguarda a configuração segura.

Para sair da sessão sem encerrar os processos, use `Ctrl+B` e depois `D`. Para retornar:

```bash
tmux attach -t jtzin
```

## Atualização

```bash
cd ~/mth-admin && \
(tmux kill-session -t jtzin 2>/dev/null || true) && \
git pull --ff-only origin master && \
chmod +x update_bot.sh watchdog.sh watchdog_all.sh && \
./update_bot.sh && \
termux-wake-lock && \
tmux new-session -d -s jtzin './watchdog_all.sh' && \
sleep 2 && \
tmux attach -t jtzin
```

O script instala as dependências, compila `bot.py`, `bot_v2.py` e `migrate_db.py`, executa a migração idempotente do Userbot e deixa os dois serviços prontos para o supervisor.

## Diagnóstico e segurança

Para observar os logs:

```bash
tail -f ~/mth-admin/logs/bot_api.log
tail -f ~/mth-admin/logs/userbot.log
```

Para interromper os processos:

```bash
tmux kill-session -t jtzin
```

O Bot API não deve receber o token pela linha de comando, e o Userbot não deve compartilhar seu arquivo `.session`. Se qualquer token, `API_HASH`, sessão ou banco for exposto, revogue ou substitua a credencial imediatamente.

## Documentação adicional

- [Tutorial completo para Termux](TUTORIAL_TERMUX.md)
- [Catálogo de comandos](COMANDOS.md)
