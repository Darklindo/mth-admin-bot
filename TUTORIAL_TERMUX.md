# Jtzin Userbot — Tutorial comercial para Termux

## 1. O que esta distribuição contém

Esta distribuição contém o Userbot ativo baseado em **Telethon**, o script de migração SQLite, o watchdog de reinício, o atualizador automático, os assets locais do comando `.exu`, o template de ambiente e a documentação operacional.

A source distribuída não contém `API_HASH`, `API_ID`, IDs de proprietários, sessão do Telegram, banco de dados, backups de perfil, tokens ou arquivos de logs. Cada comprador deve configurar as próprias credenciais e a própria conta.

> Nunca compartilhe o arquivo `.env`, qualquer arquivo `*.session`, `data/bot.db` ou um backup de perfil.

## 2. Requisitos

O ambiente recomendado é o Termux atualizado em um dispositivo Android com conexão estável à internet. O pacote utiliza Python 3, Telethon, `python-dotenv`, SQLite, `tmux` e Bash.

| Recurso | Finalidade |
|---|---|
| Python 3 | Execução do Userbot |
| Git | Atualização da source |
| tmux | Manter o processo ativo no terminal |
| Termux:API não é obrigatório | O Userbot não depende de notificações externas |
| Conta Telegram própria | Autenticação MTProto do Userbot |

## 3. Obter credenciais próprias

Acesse [my.telegram.org](https://my.telegram.org), entre com a sua conta e crie ou consulte uma aplicação para obter `API_ID` e `API_HASH`. Essas credenciais são pessoais e não devem ser reutilizadas entre clientes sem autorização.

O `OWNER_ID` é o ID numérico da conta que será usada como proprietária absoluta. O modo mais simples para descobrir o ID é iniciar a sessão e usar `.id` em uma mensagem da própria conta, ou consultar um bot confiável de identificação antes de configurar o Userbot.

## 4. Instalação limpa

No Termux, execute:

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
nano .env
```

Preencha o `.env` com valores próprios:

```dotenv
API_ID=SEU_API_ID
API_HASH=SEU_API_HASH
OWNER_ID=SEU_ID_NUMERICO
SECOND_OWNER_ID=0
THIRD_OWNER_ID=0
```

`SECOND_OWNER_ID` e `THIRD_OWNER_ID` são opcionais. Mantenha `0` quando não houver subproprietário. Não coloque aspas, espaços extras ou o símbolo `@` nos IDs numéricos.

## 5. Primeira autenticação

Com o ambiente virtual ativado, execute:

```bash
cd ~/mth-admin
source .venv/bin/activate
python migrate_db.py
python bot_v2.py
```

Na primeira execução, o Telethon solicitará o número de telefone, o código de login enviado pelo Telegram e, se existir, a senha da verificação em duas etapas. A sessão será gravada localmente em um arquivo `*.session`. Não envie esse arquivo para outra pessoa: ele pode permitir acesso à conta autenticada.

Depois de confirmar que o Userbot entrou online, interrompa-o com `Ctrl+C` e inicie pelo watchdog:

```bash
termux-wake-lock
tmux new-session -d -s mthadmin './watchdog.sh'
tmux attach -t mthadmin
```

Para sair da visualização sem encerrar o Userbot, pressione `Ctrl+B` e depois `D`. Para retornar:

```bash
tmux attach -t mthadmin
```

## 6. Atualização segura

Use o atualizador para baixar a versão publicada, instalar dependências, executar a migração e ajustar permissões:

```bash
cd ~/mth-admin
(tmux kill-session -t mthadmin 2>/dev/null || true)
git pull --ff-only origin master
chmod +x update_bot.sh watchdog.sh
./update_bot.sh
termux-wake-lock
tmux new-session -d -s mthadmin './watchdog.sh'
sleep 2
tmux attach -t mthadmin
```

O `--ff-only` impede que uma atualização automática crie um merge inesperado. Se houver alterações locais, faça backup delas antes de executar o pull.

## 7. Diagnóstico básico

Após iniciar, envie os seguintes comandos em uma conversa permitida:

```text
.status
.health
.latency
.help
```

`.status` mostra o estado operacional, `.health` verifica conexão, sessão, banco e permissões, `.latency` separa idade do update de latência E2E, e `.help` exibe o guia interno.

## 8. Segurança operacional

O Userbot atua como uma conta de usuário e não como um Bot API tradicional. Para banir, silenciar, apagar ou restringir mensagens, a conta precisa possuir as permissões correspondentes no chat. Usuários autorizados podem utilizar comandos conforme a configuração, mas não ganham imunidade automaticamente.

Use `.salvar` antes de qualquer clonagem de perfil e mantenha o backup apenas no dispositivo. O comando `.clonar` pode copiar nome, bio e foto; o username só é tentado quando a confirmação explícita `--tag --confirmar` é fornecida.

Se `API_HASH`, sessão ou qualquer token for exposto, interrompa o Userbot e substitua ou revogue a credencial antes de continuar. Não publique screenshots do `.env`, do terminal de login ou do diretório `data/`.

## 9. Estrutura dos arquivos

| Arquivo ou pasta | Uso |
|---|---|
| `bot_v2.py` | Source ativa do Userbot |
| `migrate_db.py` | Criação e migração idempotente do SQLite |
| `requirements.txt` | Dependências Python |
| `.env.example` | Modelo seguro de configuração |
| `.env` | Configuração privada criada pelo comprador |
| `watchdog.sh` | Reinício após falhas inesperadas |
| `update_bot.sh` | Atualização e instalação |
| `data/` | Banco local e backups privados |
| `assets/exu/` | Imagens JPEG e stickers WebP do `.exu` |
| `TUTORIAL_TERMUX.md` | Este tutorial |
| `COMANDOS.md` | Catálogo operacional |

## 10. Limitações conhecidas

O Userbot não pode recuperar uma mensagem Telegram apagada; o Antiblack apenas republica mensagens recentes quando recebe o evento e ainda possui a cópia temporária. O envio de mídia, banimento ou edição de perfil pode ser recusado pelo Telegram quando faltarem permissões, quando houver FloodWait ou quando a conta estiver limitada.
