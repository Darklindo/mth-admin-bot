# Catálogo de comandos — Jtzin Bot API + Userbot V9.0

A V9.0 possui duas superfícies independentes. O **Bot API** usa comandos com `/` e também reconhece aliases com `.`. O **Userbot** usa comandos com `.` e só aceita mensagens do proprietário configurado em `OWNER_ID`.

## Bot API — moderação local e global

| Comando | Função | Acesso |
|---|---|---|
| `/blacklist` ou `.blacklist` | Registra o alvo na blacklist local do grupo atual e apaga mensagens dele enquanto o bot estiver ativo. | Administrador do grupo ou `OWNER_IDS` |
| `/unblacklist` ou `.unblacklist` | Remove o alvo da blacklist local do grupo atual. | Administrador do grupo ou `OWNER_IDS` |
| `/banperm` ou `.banperm` | Bane permanentemente o alvo somente no grupo atual. | Administrador do grupo ou `OWNER_IDS` |
| `/unbanperm` ou `.unbanperm` | Retira o banimento permanente do alvo no grupo atual. | Administrador do grupo ou `OWNER_IDS` |
| `/allban` ou `.allban` | Registra o alvo na lista global e tenta bani-lo em todos os grupos conhecidos do Bot API. | Somente um dos `OWNER_IDS` |
| `/unallban` ou `.unallban` | Remove o alvo da lista global e tenta desbaní-lo nos grupos conhecidos. | Somente um dos `OWNER_IDS` |
| `/latency` ou `.latency` | Mede uma chamada real à API do Telegram e informa o tempo total. | Administradores do grupo ou `OWNER_IDS` |

Todos os comandos aceitam reply à mensagem do alvo ou ID/username conhecido. Os proprietários podem usar a moderação local mesmo sem serem administradores do grupo, mas o Bot API precisa estar no grupo e possuir as permissões administrativas correspondentes. A função global não consegue operar em chats que o bot não conhece, não acessa ou onde não pode restringir membros. O banco do Bot API é separado do banco do Userbot.

## Userbot — prefixos

Todos os comandos do Userbot usam `.`. Para evitar conflito com o Group Help, somente estes sete usam `.jt`:

| Conflito | Forma correta |
|---|---|
| ban | `.jtban` |
| mute | `.jtmute` |
| del | `.jtdel` |
| delwarn | `.jtdelwarn` |
| purge | `.jtpurge` |
| purgeall | `.jtpurgeall` |
| warn | `.jtwarn` |

Todos os demais comandos permanecem com o prefixo normal, como `.kick`, `.lock`, `.infojt` e `.exu`.

> **Acesso:** na V9.0 não existem subproprietários nem autorização de terceiros. Os comandos `.autorizar`, `.desautorizar` e `.listauth` foram removidos. Somente o `OWNER_ID` usa o Userbot.

## Moderação local

| Comando | Função e exemplo |
|---|---|
| `.jtban` | Banimento temporário local. Exemplo: `.jtban 1h motivo`; exige duração explícita e pode usar `--purge N`. |
| `.banperm` | Banimento permanente local no chat atual. Exemplo: `.banperm @usuario motivo`. |
| `.jtmute` | Silencia temporariamente no chat atual. Exemplo: `.jtmute 30m motivo`. |
| `.kick` | Remove o usuário do chat sem banimento permanente. |
| `.unban` | Remove o banimento local. |
| `.unbanperm` | Remove o banimento permanente local. |
| `.unmute` | Remove o silêncio local. |
| `.blacklist` | Ativa blacklist local, com duração opcional, apagando mensagens do alvo no chat atual. |
| `.unblacklist` | Remove a blacklist local. |
| `.jtdel` | Apaga somente a mensagem respondida. |
| `.jtdelwarn` | Apaga a mensagem respondida e aplica advertência ao autor. |
| `.jtpurge` | Apaga mensagens recentes do alvo. Use `.jtpurge 10`; mensagens fixadas são protegidas por padrão. |
| `.purgeme` | Apaga mensagens recentes da própria conta. |
| `.jtpurgeall` | Apaga mensagens recentes em grande quantidade, respeitando limites de segurança. Use `--include-pinned` para incluir fixadas. |
| `.lock` | Fecha o envio para membros, mantendo administradores liberados e salvando snapshot. |
| `.unlock` | Reabre o chat e restaura o snapshot anterior. |

Ao responder à mensagem do alvo, o ID ou username pode ser omitido. A conta precisa ter as permissões administrativas exigidas pelo Telegram.

## Advertências, antispam e quarentena

| Comando | Função e exemplo |
|---|---|
| `.jtwarn` | Adverte o alvo. Exemplo: `.jtwarn @usuario motivo`. |
| `.warns` | Consulta advertências do alvo. |
| `.unwarn` | Remove uma advertência. |
| `.clearwarns` | Remove todas as advertências. |
| `.antispam on/off` | Ativa ou desativa antispam por pontuação, rajadas, links, mídia e duplicação. |
| `.quarantine on/off` | Ativa ou desativa quarentena para padrões fortes de spam. |
| `.pinned on/off` | Define a proteção de mensagens fixadas nos comandos de purge. |

O antispam usa múltiplos sinais e não deve punir por uma mensagem isolada. A quarentena deve priorizar contenção reversível quando a confiança do detector não for suficiente para uma punição definitiva.

## Links

| Comando | Função e exemplo |
|---|---|
| `.antilink on/off` | Permite links somente a administradores e usuários autorizados no chat. O modo fail-closed bloqueia quando o estado administrativo é desconhecido. |
| `.autorizarlink` | Autoriza um usuário específico a enviar links. Pode usar reply, ID ou username. |
| `.desautorizarlink` | Remove a autorização de links. |
| `.listlinkauth` | Lista usuários autorizados a enviar links no chat atual. |

A autorização de links é apenas uma configuração do AntiLink. Ela não concede acesso aos comandos do Userbot.

## Controle global do Userbot

| Comando | Função e exemplo |
|---|---|
| `.allban` | Banimento global nos chats compatíveis. Aceita duração e `--purge N`. |
| `.allblack` | Blacklist global; apaga mensagens do alvo nos chats registrados. |
| `.unallblack` | Remove a blacklist global. |
| `.shadow` | Shadow ban global com duração opcional, como `.shadow 7d`. |
| `.unshadow` | Remove o shadow ban global. |
| `.maintenance on/off` | Ativa ou desativa o modo de manutenção. |

Todos os comandos do Userbot são exclusivos do proprietário único configurado no `.env`. A execução ainda depende das permissões da conta em cada chat e das limitações do Telegram.

## AntiBlack, AntiSpy e diagnósticos

| Comando | Função e exemplo |
|---|---|
| `.antiblack on/off` | Ativa ou desativa o Modo Fênix por chat. |
| `.antiblack add` | Protege um usuário adicional por reply, ID ou username. |
| `.antiblack list` | Lista os usuários protegidos no chat. |
| `.unantiblack` | Remove um usuário da proteção. |
| `.listantiblack` | Alias para a listagem do AntiBlack. |
| `.antispy` | Analisa sinais de outros userbots ou atividade administrativa suspeita. |
| `.listspy` | Lista usuários monitorados pelo AntiSpy. |
| `.delspy` | Remove um usuário do monitoramento. |
| `.status` | Mostra o estado operacional resumido. |
| `.health` | Verifica sessão, conexão, banco e permissões. |
| `.latency` | Mostra latência RPC, E2E, idade do update e falhas. |
| `.logs` | Exibe logs de mensagens apagadas, quando disponíveis. |

## Perfil, utilitários e mídia

| Comando | Função e exemplo |
|---|---|
| `.salvar` | Salva nome, bio, username e foto atuais da conta. |
| `.clonar` | Copia nome, bio e foto do usuário-alvo por reply, ID ou username. |
| `.clonar --tag --confirmar @usuario` | Tenta copiar também o username com confirmação explícita. |
| `.restaurar` | Restaura o último backup criado por `.salvar`. |
| `.start` | Exibe informações iniciais do Userbot. |
| `.help` | Exibe o guia interno atualizado. |
| `.id` | Mostra o ID do autor respondido ou do alvo. |
| `.infojt` | Exibe informações detalhadas do usuário e o status correto no chat. |
| `.chats` | Lista chats registrados e o resumo operacional. |
| `.listdn` | Exibe punições locais e globais. |
| `.msg` | Envia texto ou mídia aos chats compatíveis. |
| `.exu` | Envia uma imagem aleatória de Exu, com fallback para sticker. |

## Regras de resposta e segurança

As respostas e as próprias mensagens de comando são programadas para exclusão automática conforme a configuração. Relatórios e diagnósticos podem permanecer por mais tempo para leitura. A conta precisa ter permissões administrativas reais; o Userbot não contorna as regras do Telegram.

Não publique tokens, `API_HASH`, sessões, bancos, backups ou logs privados. Se uma credencial for exposta, revogue-a ou substitua-a antes de continuar.
