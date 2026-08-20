# Tutorial Termux — Jtzin Bot API + Userbot V9.0

## 1. O que esta distribuição contém

Esta distribuição contém dois processos independentes: o **Bot API** de moderação local/global e o **Userbot Telethon**, com os recursos avançados históricos. Eles usam credenciais, bancos e estados separados.

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

Para o Bot API, abra o `@BotFather`, crie um bot e copie o token diretamente para o arquivo privado `.env.bot`. Se o token já tiver sido compartilhado, revogue-o antes. Como os comandos do Bot API usam `.`, abra também `/setprivacy` no `@BotFather`, selecione `@Mhzinbot_bot` e escolha `Disable`; sem isso, mensagens pontuadas em grupos não chegam ao bot.

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
OWNER_IDS=ID1,ID2
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

## 6. Iniciar somente o Bot API

Enquanto o Userbot estiver desligado, use somente o supervisor abaixo:

```bash
cd ~/mth-admin
chmod +x watchdog_bot_only.sh update_bot_only.sh
termux-wake-lock
tmux kill-session -t jtzin 2>/dev/null || true
tmux new-session -d -s jtzin './watchdog_bot_only.sh'
```

O `watchdog_bot_only.sh` inicia `bot.py`, reinicia o Bot API com backoff progressivo e grava `logs/bot_api.log`. O polling tenta recuperar falhas transitórias de rede durante o bootstrap, enquanto as ações usam retries limitados com backoff e pools HTTP separados do polling. O `bot.py` atualiza `data/bot_api.heartbeat` a cada 15 segundos; se o event loop parar de atualizar esse arquivo por 180 segundos, o watchdog considera o processo travado e reinicia somente o Bot API. O `watchdog_all.sh` só deve ser usado quando o Userbot for reativado deliberadamente.

Para sair da visualização sem encerrar os serviços, pressione `Ctrl+B` e depois `D`. Para retornar:

```bash
tmux attach -t jtzin
```

## 7. Comandos do Bot API

O Bot API responde somente a comandos iniciados por `.`, e o menu nativo de comandos do Telegram permanece desativado:

```text
.blacklist
.blacklist list
.blacklist list 2
.unblacklist
.banperm
.unbanperm
.jtbn
.jtbn list
.jtbn list 2
.unjtbn
.lock
.unlock
.latency
.divulgar 30m on
.divulgar list
.divulgar off ID
.divulgar off all
.spam 10              # respondendo a uma mensagem
.spam 10 texto        # repetindo texto novo
.spam off             # cancelar execução atual
```

As listas aceitam uma página opcional, por exemplo `.blacklist list 2` e `.jtbn list 2`; o Bot API consulta somente a página solicitada no SQLite. Responda à mensagem do alvo ou informe um ID numérico. Usernames só podem ser resolvidos quando o bot já os conhece ou quando foram registrados anteriormente. **Todos os comandos do Bot API são exclusivos dos dois owners configurados em `OWNER_IDS`; administradores comuns não recebem respostas nem podem executar comandos.** O Bot API ainda precisa ter as permissões administrativas correspondentes para realizar as ações. `.jtbn` e `.unjtbn` também são exclusivos dos dois IDs e tentam operar nos chats conhecidos em que o bot tenha acesso.

Use `.lock` para fechar o envio de mensagens para membros comuns. Administradores e o dono do grupo continuam liberados, e o Bot API continua podendo enviar mensagens e administrar o chat. O bot salva as permissões padrão anteriores antes de aplicar o bloqueio. Use `.unlock` para restaurar exatamente essas permissões; repetir qualquer um dos comandos não reaplica alterações desnecessárias. O Bot API precisa ser administrador com permissão para restringir membros.

Para divulgar conteúdo periodicamente, responda a uma mensagem de texto, foto ou vídeo com `.divulgar 30m on`. Cada uso cria uma nova agenda independente no mesmo grupo, identificada por um `schedule_id`; são permitidas até 32 agendas por grupo. O bot republica o texto ou a mídia com a legenda original a cada intervalo entre 30 segundos e 30 dias. Use `.divulgar list` para consultar os IDs, `.divulgar off ID` para desligar uma agenda específica e `.divulgar off all` para desligar todas. Quando existe apenas uma agenda, `.divulgar off` também funciona; quando existem várias, ele apenas mostra a lista para evitar cancelamento acidental. Os agendamentos ficam persistidos no banco do Bot API e são restaurados depois de reinícios. O bot precisa continuar no grupo e ter permissão para enviar mensagens, fotos e vídeos. O owner `6822870889` (`@OnlyExaltarei`) recebe mensagens privadas com a ativação, o ID e primeiro horário, cada envio realizado e o próximo envio, além de alertas controlados quando houver falha. As notificações são executadas de forma independente: se o privado falhar, o worker continua publicando no grupo e tenta novamente conforme o ciclo normal.

Para usar o spam controlado, responda à mensagem desejada e envie `.spam 10`. O Bot API copia até 100 vezes a mensagem respondida, aceitando texto, foto, vídeo, GIF, sticker, documento, áudio e voz quando forem copiáveis pelo Bot API. Para texto novo, use `.spam 10 meu texto`. O limite é de 1 a 100, existe no máximo uma execução por grupo e o intervalo entre cópias evita uma rajada descontrolada. Use `.spam off` para cancelar o worker atual. Falhas permanentes, falta de permissão e limites do Telegram interrompem a execução com segurança, sem retry infinito nem tarefas órfãs.

## 8. Comandos do Userbot

O Userbot usa o prefixo `.` e só aceita comandos enviados pelo `OWNER_ID`. Não há mais subproprietários nem autorização de terceiros. Os comandos `.autorizar`, `.desautorizar` e `.listauth` foram removidos.

A autorização de links, quando usada pelo proprietário, continua sendo uma configuração específica do AntiLink e não concede acesso ao Userbot. Os comandos que mantêm `.jt` por conflito com o Group Help são `.jtban`, `.jtmute`, `.jtdel`, `.jtdelwarn`, `.jtpurge`, `.jtpurgeall` e `.jtwarn`.

## 9. Atualização segura do Bot API-only

```bash
cd ~/mth-admin
tmux kill-session -t jtzin 2>/dev/null || true
./update_bot_only.sh
termux-wake-lock
tmux new-session -d -s jtzin './watchdog_bot_only.sh'
sleep 2
tmux list-sessions
```

O `update_bot_only.sh` instala dependências, compila somente `bot.py`, preserva o `.env.bot` local e não inicia o Userbot. O atualizador interrompe o fluxo quando existem alterações locais relevantes, para evitar sobrescrever trabalho.

## 10. Diagnóstico e logs

Logs do Bot API e Userbot:

```bash
tail -f ~/mth-admin/logs/bot_api.log
tail -f ~/mth-admin/logs/userbot.log
```

O Bot API oferece os comandos de moderação, diagnóstico e divulgação listados acima no modo dot-only; todos são exclusivos dos proprietários configurados. O `.latency` exibe a chamada real à API, idade do update, fila local, tempos de exclusão, duração dos comandos, retries de flood/rede, falhas recuperadas do polling e o último erro observado. Use essa separação para diferenciar latência do Telegram de atraso local do Termux. O Userbot oferece `.status`, `.health` e `.latency` quando for reativado. Para interromper o processo atual do Bot API:

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
| `watchdog_all.sh` | Supervisor dos dois processos, somente quando o Userbot for reativado |
| `watchdog_bot_only.sh` | Supervisor atual, somente do Bot API |
| `update_bot.sh` | Atualização dos dois processos |
| `update_bot_only.sh` | Atualização atual somente do Bot API |
| `data/` | Bancos e dados privados |
| `logs/` | Logs locais ignorados pelo Git |
| `assets/exu/` | Assets do comando `.exu` |

## 12. Segurança operacional

Nunca publique tokens, `API_HASH`, sessões, bancos, backups ou logs com dados privados. O Bot API e o Userbot não devem compartilhar tokens ou arquivos de sessão. Se uma credencial for exposta, revogue-a ou substitua-a antes de continuar.

O Telegram pode recusar banimentos, exclusões e edições quando faltarem permissões, houver FloodWait, limitações da conta ou restrições específicas do chat. O `.jtbn` não consegue operar em chats que o Bot API não acessa.
