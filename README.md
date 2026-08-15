# Jtzin Userbot

Userbot profissional de administração para Telegram, desenvolvido com **Telethon** e preparado para execução no Termux. Os comandos usam o prefixo `.` e são processados pela conta de usuário autenticada no MTProto; este projeto não usa `BOT_TOKEN` nem o Bot API.

## Entrada correta

O processo atual deve ser iniciado por `bot_v2.py`, preferencialmente pelo `watchdog.sh`. O arquivo `bot.py` é uma implementação legada baseada no Bot API e não deve ser usado para executar o Userbot atual.

```bash
python3 migrate_db.py
python3 bot_v2.py
```

Para operação persistente no Termux:

```bash
termux-wake-lock
tmux new-session -d -s mthadmin './watchdog.sh'
tmux attach -t mthadmin
```

Para sair do tmux sem encerrar o processo, use `Ctrl+B` e depois `D`. Para retornar às logs, execute `tmux attach -t mthadmin`.

## Instalação

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
python3 migrate_db.py
```

O arquivo `.env` precisa conter `API_ID`, `API_HASH` e `OWNER_ID`. Os IDs `SECOND_OWNER_ID` e `THIRD_OWNER_ID` podem ser configurados para os subproprietários. Nunca publique o `.env`, o arquivo `.session` ou o banco de dados.

## Atualização no Termux

```bash
cd ~/mth-admin && (tmux kill-session -t mthadmin 2>/dev/null || true) && git pull --ff-only origin master && chmod +x update_bot.sh watchdog.sh && ./update_bot.sh && termux-wake-lock && tmux new-session -d -s mthadmin './watchdog.sh' && sleep 2 && tmux attach -t mthadmin
```

O script de atualização interrompe o fluxo em caso de erro, recria o ambiente virtual quando necessário, instala as dependências e executa a migração SQLite antes da inicialização.

## Recursos principais

O Userbot oferece moderação local e global, blacklist, shadow ban, banimentos temporários, sistema de advertências, antispam com pontuação, quarentena, antilink, autorização permanente ou temporária, proteção de mensagens fixadas, purge, relatórios, logs, diagnóstico e modo de manutenção. A documentação operacional completa é exibida pelo comando `.help` depois da autenticação.

Exemplos de autorização temporária:

```text
.autorizar 10s @usuario
.autorizar 30m @usuario
.autorizar 10h 123456789
.autorizar 10d
.autorizar @usuario
.listauth
.desautorizar @usuario
```

Quando o comando é respondido à mensagem de alguém, o alvo pode ser omitido. Sem duração, a autorização é permanente. O prazo mínimo é de 10 segundos; durações aceitas incluem segundos (`s`), minutos (`m`), horas (`h`), dias (`d`) e semanas (`w`).

## Permissões e operação

Para excluir mensagens, restringir usuários e aplicar banimentos, a conta precisa ter as permissões administrativas correspondentes em cada grupo ou canal. A imunidade dos proprietários é aplicada antes das punições, enquanto usuários autorizados recebem acesso aos comandos sem ganhar imunidade automática.

O banco SQLite fica em `data/bot.db`, utiliza WAL e possui migrações idempotentes. O arquivo `migrate_db.py` pode ser executado novamente sem apagar os registros existentes.

## Documentação comercial

- [Tutorial completo para Termux](TUTORIAL_TERMUX.md)
- [Catálogo completo de comandos V8.0](COMANDOS.md)

O comando `.exu` utiliza assets locais e possui fallback automático para stickers. Os comandos de perfil `.salvar`, `.clonar` e `.restaurar` são exclusivos ao proprietário.

## Segurança

As credenciais MTProto são sensíveis. Não publique `API_HASH`, sessões do Telethon, tokens, banco local ou logs que contenham dados privados. Se uma credencial for exposta, revogue-a ou substitua-a antes de continuar usando o ambiente.
