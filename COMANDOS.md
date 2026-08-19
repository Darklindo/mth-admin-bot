# Catálogo de comandos — Jtzin Bot API + Userbot V9.0

A V9.0 possui duas superfícies independentes. O **Bot API** usa somente comandos com `.`. O **Userbot** também usa comandos com `.` e só aceita mensagens do proprietário configurado em `OWNER_ID`.

## Bot API — moderação local e global

| Comando | Função | Acesso |
|---|---|---|
| `.blacklist` | Registra o alvo na blacklist local do grupo atual e apaga mensagens dele enquanto o bot estiver ativo. | Somente `OWNER_IDS` |
| `.unblacklist` | Remove o alvo da blacklist local do grupo atual. | Somente `OWNER_IDS` |
| `.banperm` | Bane permanentemente o alvo somente no grupo atual. | Somente `OWNER_IDS` |
| `.unbanperm` | Retira o banimento permanente do alvo no grupo atual. | Somente `OWNER_IDS` |
| `.allban` | Registra o alvo na lista global e tenta bani-lo em todos os grupos conhecidos do Bot API. | Somente um dos `OWNER_IDS` |
| `.unallban` | Remove o alvo da lista global e tenta desbaní-lo nos grupos conhecidos. | Somente um dos `OWNER_IDS` |
| `.latency` | Mede uma chamada real à API e informa idade do update, fila local, exclusões, duração dos comandos, retries, falhas de polling e último erro observado. | Somente `OWNER_IDS` |
| `.divulgar 30m on` | Cria uma nova republicação periódica de uma mensagem de texto, foto ou vídeo respondido no grupo atual. Aceita intervalos de `30s` a `30d`. | Somente `OWNER_IDS` |
| `.divulgar list` | Lista as divulgações ativas do grupo e seus `schedule_id`. | Somente `OWNER_IDS` |
| `.divulgar off ID` | Desliga somente a divulgação identificada pelo `schedule_id`. | Somente `OWNER_IDS` |
| `.divulgar off all` | Desliga todas as divulgações do grupo; sem `ID`, `.divulgar off` só desliga diretamente quando há uma única agenda. | Somente `OWNER_IDS` |

Todos os comandos aceitam reply à mensagem do alvo ou ID/username conhecido, mas **somente os dois owners configurados em `OWNER_IDS` recebem respostas e executam comandos**. Administradores comuns não têm acesso ao dispatcher do bot. O Bot API ainda precisa estar no grupo e possuir as permissões administrativas correspondentes para realizar as ações. A função global não consegue operar em chats que o bot não conhece, não acessa ou onde não pode restringir membros. O banco do Bot API é separado do banco do Userbot.

As ações da Bot API usam retries limitados com backoff para falhas transitórias e `RetryAfter`, sem transformar erros permanentes de permissão ou conteúdo em loops infinitos. O polling possui conexão própria, e o watchdog monitora o processo e o arquivo `data/bot_api.heartbeat`; se o event loop travar e o heartbeat ficar obsoleto, somente o Bot API é reiniciado. O Userbot permanece desligado.

### Divulgação periódica

Para ativar, responda à mensagem que será divulgada — texto, foto ou vídeo — usando `.divulgar 30m on`. Cada ativação cria uma nova agenda independente; portanto, o mesmo grupo pode ter, por exemplo, uma publicação a cada 20 minutos e outra a cada 30 minutos. O texto de uma mensagem de texto é republicado como texto; a legenda de uma foto ou vídeo é preservada como legenda. O intervalo permitido vai de 30 segundos a 30 dias, com limite de 32 agendas por grupo. Use `.divulgar list` para consultar os IDs, `.divulgar off ID` para cancelar apenas uma agenda e `.divulgar off all` para cancelar todas. O comando `.divulgar off` sem ID desliga diretamente quando existe apenas uma agenda e, quando há várias, mostra a lista sem cancelar nada. Os agendamentos ficam salvos no banco do Bot API, são restaurados após reinícios e recebem notificações privadas para `6822870889` (`@OnlyExaltarei`) na ativação, em cada envio, no desligamento e em falhas com limitação de repetição.

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
