# Tutorial Termux — Jtzin Bot API + Userbot V9.0

## 1. O que esta distribuição contém

Esta distribuição contém dois processos independentes: o **Bot API** mínimo, com `blacklist`, `banperm` e `allban`, e o **Userbot Telethon**, com os recursos avançados históricos. Eles usam credenciais, bancos e estados separados.

A source não contém `API_HASH`, sessão do Telegram, bancos, backups de perfil, token real do Bot API ou logs. Cada instalação deve usar credenciais próprias.

> Como tokens enviados em mensagens podem ser comprometidos, revogue imediatamente qualquer token exposto no `@BotFather` e gere outro. Nunca coloque um token real em arquivos versionados.

## 2. Requisitos

Use o Termux atualizado em um dispositivo Android com conexão estável. O pacote utiliza Python 3, Telethon, `python-telegram-bot`, `python-dotenv`, SQLite, `tmux` e Bash.

| Recurso | Finalidade |
|---|---|
| Python 3 | Executar os dois processos |
| Git | Baixar atualizações |
| tmux | Manter o supervisor ativo |
| Conta Telegram própria | Autenticação MTProto do Userbot |
| Bot criado no @BotFather | Credencial do Bot API |
| Permissões administrativas | Banir e apagar mensagens nos chats |

## 3. Credenciais

Para o Userbot, acesse [my.telegram.org](https://my.telegram.org), entre com a conta que será autenticada e obtenha `API_ID` e `API_HASH`. O `OWNER_ID` deve ser o ID numérico da conta **JT Cacique** que utilizará a sessão do Userbot.

Para o Bot API, abra o `@BotFather`, crie um bot e copie o token diretamente para o arquivo privado `.env.bot`. Se o token já tiver sido compartilhado, revogue-o antes.

## 4. Instalação limpa

No Termux:

```bash
pkg update -y
pkg upgrade -y
pkg install python git tmux -y
git clone https://github.com/Darklindo/mth-admin-bot.git mth-admin
cd ~/mth-admin
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
cp .env.bot.example .env.bot
nano .env
nano .env.bot
```

O `.env` do Userbot deve conter:

```dotenv
API_ID=SEU_API_ID
API_HASH=SEU_API_HASH
OWNER_ID=SEU_ID_JT_CACIQUE
```

O `.env.bot` deve conter:

```dotenv
BOT_TOKEN=TOKEN_NOVO_GERADO_PELO_BOTFATHER
OWNER_ID=SEU_ID_JT_CACIQUE
```

Não use `@` nos IDs, não coloque espaços extras e não compartilhe esses arquivos. Os campos `SECOND_OWNER_ID` e `THIRD_OWNER_ID` não existem mais na V9.0.

## 5. Primeira autenticação do Userbot

Execute a migração e inicie temporariamente o Userbot:

```bash
cd ~/mth-admin
source .venv/bin/activate
python migrate_db.py
python bot_v2.py
```

Na primeira execução, o Telethon solicitará telefone, código do Telegram e, se necessário, a senha da verificação em duas etapas. A sessão será salva localmente em `*.session`. Interrompa com `Ctrl+C` após confirmar que a sessão entrou online.

O Bot API não exige login interativo: ele usará o token privado do `.env.bot` quando o supervisor for iniciado.

## 6. Iniciar Bot API e Userbot juntos

```bash
cd ~/mth-admin
chmod +x update_bot.sh watchdog.sh watchdog_all.sh
termux-wake-lock
tmux new-session -d -s jtzin './watchdog_all.sh'
tmux attach -t jtzin
```

O `watchdog_all.sh` inicia `bot.py` e `bot_v2.py` separadamente, reinicia cada processo com backoff próprio e grava os logs em arquivos distintos. Se o `.env.bot` estiver ausente, o Userbot pode iniciar e o Bot API ficará aguardando a configuração.

Para sair da visualização sem encerrar os serviços, pressione `Ctrl+B` e depois `D`. Para retornar:

```bash
tmux attach -t jtzin
```

## 7. Comandos do Bot API

O Bot API usa comandos tradicionais com `/`:

```text
/blacklist
/banperm
/allban
```

Responda à mensagem do alvo ou informe um ID numérico. Usernames só podem ser resolvidos quando o bot já os conhece ou quando foram registrados anteriormente. O bot precisa ser administrador do grupo com permissões adequadas.

`/blacklist` é local ao grupo atual. `/banperm` também afeta somente o grupo atual. `/allban` é global e só pode ser executado pelo `OWNER_ID`; ele tenta aplicar banimento nos chats conhecidos em que o bot tenha acesso.

## 8. Comandos do Userbot

O Userbot usa o prefixo `.` e só aceita comandos enviados pelo `OWNER_ID`. Não há mais subproprietários nem autorização de terceiros. Os comandos `.autorizar`, `.desautorizar` e `.listauth` foram removidos.

A autorização de links, quando usada pelo proprietário, continua sendo uma configuração específica do AntiLink e não concede acesso ao Userbot. Os comandos que mantêm `.jt` por conflito com o Group Help são `.jtban`, `.jtmute`, `.jtdel`, `.jtdelwarn`, `.jtpurge`, `.jtpurgeall` e `.jtwarn`.

## 9. Atualização segura

```bash
cd ~/mth-admin
(tmux kill-session -t jtzin 2>/dev/null || true)
git pull --ff-only origin master
chmod +x update_bot.sh watchdog.sh watchdog_all.sh
./update_bot.sh
termux-wake-lock
tmux new-session -d -s jtzin './watchdog_all.sh'
sleep 2
tmux attach -t jtzin
```

O atualizador interrompe o fluxo quando há alterações locais, instala dependências, compila `bot.py`, `bot_v2.py` e `migrate_db.py`, executa a migração idempotente e prepara os scripts de supervisão.

## 10. Diagnóstico e logs

Logs do Bot API e Userbot:

```bash
tail -f ~/mth-admin/logs/bot_api.log
tail -f ~/mth-admin/logs/userbot.log
```

O Userbot oferece `.status`, `.health` e `.latency`. Para interromper os dois processos:

```bash
tmux kill-session -t jtzin
```

## 11. Estrutura dos arquivos

| Arquivo ou pasta | Uso |
|---|---|
| `bot.py` | Bot API mínimo |
| `bot_v2.py` | Userbot Telethon |
| `migrate_db.py` | Migração idempotente do banco do Userbot |
| `requirements.txt` | Dependências dos dois processos |
| `.env.example` | Modelo público do Userbot |
| `.env.bot.example` | Modelo público do Bot API |
| `.env` / `.env.bot` | Credenciais privadas |
| `watchdog.sh` | Supervisor individual legado do Userbot |
| `watchdog_all.sh` | Supervisor dos dois processos |
| `update_bot.sh` | Atualização e validação |
| `data/` | Bancos e dados privados |
| `logs/` | Logs locais ignorados pelo Git |
| `assets/exu/` | Assets do comando `.exu` |

## 12. Segurança operacional

Nunca publique tokens, `API_HASH`, sessões, bancos, backups ou logs com dados privados. O Bot API e o Userbot não devem compartilhar tokens ou arquivos de sessão. Se uma credencial for exposta, revogue-a ou substitua-a antes de continuar.

O Telegram pode recusar banimentos, exclusões e edições quando faltarem permissões, houver FloodWait, limitações da conta ou restrições específicas do chat. O `/allban` não consegue operar em chats que o Bot API não acessa.
