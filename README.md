# Jtzin Bot API + Userbot V9.0

Projeto de administração para Telegram com dois processos independentes no Termux: um **bot comum da Bot API**, dedicado à moderação local e global, e um **Userbot Telethon**, destinado exclusivamente à conta proprietária JT Cacique.

> **Segurança:** o token do Bot API nunca deve ser publicado, versionado ou enviado em mensagens. Como o token anterior foi exposto, ele deve ser revogado no `@BotFather` e substituído por um token novo antes da operação.

## Arquitetura

| Processo | Arquivo | Prefixo | Acesso | Banco |
|---|---|---|---|---|
| Bot comum | `bot.py` | `.` | Somente os dois `OWNER_IDS`; administradores comuns não recebem respostas | `data/bot_api.db` |
| Userbot | `bot_v2.py` | `.` | Somente o `OWNER_ID` configurado | `data/bot.db` |

O Bot API e o Userbot não compartilham sessão, token, banco nem estado de autorização. O Userbot não possui mais subproprietários ou autorização delegada; os comandos `.autorizar`, `.desautorizar` e `.listauth` não fazem parte da superfície operacional da V9.0.

Para que comandos com ponto enviados em grupos sejam recebidos pelo Bot API, desative o Privacy Mode no `@BotFather` com `/setprivacy`, selecione o bot e escolha `Disable`. A confirmação pode ser verificada pelo campo `can_read_all_group_messages=true` no método `getMe`. Isso não habilita comandos slash nem o menu nativo.

## Funções do Bot API

O bot comum oferece moderação local e global e responde somente a comandos iniciados por `.`, além de reply à mensagem do alvo ou ID/username previamente conhecido pelo bot. O menu nativo de comandos do Telegram permanece desativado para evitar respostas a comandos slash enviados por membros.

| Comando | Função | Permissão |
|---|---|---|
| `.blacklist` | Cadastra o alvo na blacklist local do grupo e apaga as mensagens dele enquanto o bot estiver ativo | Somente `OWNER_IDS` |
| `.unblacklist` | Remove o alvo da blacklist local do grupo | Somente `OWNER_IDS` |
| `.banperm` | Bane permanentemente o alvo no grupo atual | Somente `OWNER_IDS` |
| `.unbanperm` | Retira o banimento permanente do alvo no grupo atual | Somente `OWNER_IDS` |
| `.allban` | Registra o alvo na blacklist global e tenta bani-lo em todos os grupos conhecidos | Somente um dos `OWNER_IDS` |
| `.unallban` | Remove o alvo da lista global e tenta desbaní-lo nos grupos conhecidos | Somente um dos `OWNER_IDS` |
| `.latency` | Mede uma chamada real à API do Telegram | Somente `OWNER_IDS` |
| `.divulgar 30m on` | Cria uma nova republicação de texto, foto ou vídeo respondido no grupo atual | Somente `OWNER_IDS` |
| `.divulgar list` | Lista as agendas ativas e seus IDs | Somente `OWNER_IDS` |
| `.divulgar off ID` | Desliga uma agenda específica pelo ID | Somente `OWNER_IDS` |
| `.divulgar off all` | Desliga todas as agendas do grupo | Somente `OWNER_IDS` |

O banco mantém a lista por chat e o Bot API possui cache próprio. **Somente os dois owners configurados em `OWNER_IDS` recebem respostas e executam comandos; administradores comuns não têm acesso ao dispatcher.** O bot precisa estar presente e ter permissões administrativas para apagar mensagens ou restringir usuários; os comandos globais só podem alcançar grupos nos quais ele esteja presente e autorizado.

O comando `.divulgar 30m on` deve ser enviado em resposta a uma mensagem de texto, foto ou vídeo. Cada ativação cria uma nova agenda independente, identificada por `schedule_id`, permitindo várias divulgações no mesmo grupo — por exemplo, uma a cada 20 minutos e outra a cada 30 minutos. O bot republica o texto ou a mídia com sua legenda a cada intervalo entre 30 segundos e 30 dias, com limite de 32 agendas por grupo. Use `.divulgar list` para consultar os IDs, `.divulgar off ID` para cancelar uma específica e `.divulgar off all` para cancelar todas. Se houver apenas uma agenda, `.divulgar off` também a desliga; com várias, o comando mostra a lista e evita cancelamento acidental. Os agendamentos são salvos no banco e restaurados após reinícios, desde que o bot continue no grupo e possa enviar mensagens e mídias. O owner `6822870889` (`@OnlyExaltarei`) recebe no privado a confirmação da ativação, o primeiro horário, cada confirmação de envio com o próximo horário, o desligamento e alertas de falha com limitação para evitar spam de notificações.

As respostas e os comandos são removidos automaticamente após alguns segundos. O bot é deliberadamente independente: não contém o restante dos recursos do Userbot, não aceita autorização de terceiros e não possui subproprietários.

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

O arquivo `.env.bot` deve conter o token do BotFather e os dois proprietários do Bot API:

```dotenv
BOT_TOKEN=COLE_UM_TOKEN_NOVO_DO_BOTFATHER
OWNER_IDS=ID1,ID2
```

Nunca substitua os valores de exemplo por credenciais dentro de arquivos versionados. `.env`, `.env.bot`, sessões, bancos, logs e arquivos de pesquisa são ignorados pelo Git.

## Inicialização atual: somente Bot API

Enquanto o Userbot estiver desligado, use exclusivamente o supervisor Bot API-only:

```bash
cd ~/mth-admin
chmod +x watchdog_bot_only.sh update_bot_only.sh
termux-wake-lock
tmux kill-session -t jtzin 2>/dev/null || true
tmux new-session -d -s jtzin './watchdog_bot_only.sh'
```

O `watchdog_bot_only.sh` inicia apenas `bot.py`, grava o log em `logs/bot_api.log` e reinicia o Bot API com backoff progressivo. O `bot.py` também mantém tentativas de bootstrap do polling para recuperar falhas transitórias de DNS ou rede; após uma queda do processo, o watchdog reinicia automaticamente. O `watchdog_all.sh` só deve ser usado quando o Userbot for reativado deliberadamente.

Para sair da sessão sem encerrar os processos, use `Ctrl+B` e depois `D`. Para retornar:

```bash
tmux attach -t jtzin
```

## Atualização Bot API-only

```bash
cd ~/mth-admin && \
tmux kill-session -t jtzin 2>/dev/null || true && \
./update_bot_only.sh && \
termux-wake-lock && \
tmux new-session -d -s jtzin './watchdog_bot_only.sh'
```

O `update_bot_only.sh` instala as dependências, compila somente `bot.py`, preserva o `.env.bot` local e deixa apenas o Bot API pronto para o supervisor. Ele não inicia nem migra o Userbot.

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
