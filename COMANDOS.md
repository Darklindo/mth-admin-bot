# Jtzin Userbot — Catálogo de comandos V8.0

Todos os comandos usam o prefixo `.`. Para evitar conflito com o Group Help, apenas os sete comandos abaixo usam o prefixo `.jt`:

| Comando com conflito | Forma correta |
|---|---|
| ban | `.jtban` |
| mute | `.jtmute` |
| del | `.jtdel` |
| delwarn | `.jtdelwarn` |
| purge | `.jtpurge` |
| purgeall | `.jtpurgeall` |
| warn | `.jtwarn` |

Todos os demais comandos permanecem com o prefixo normal, como `.kick`, `.lock`, `.infojt` e `.exu`.

## Moderação local

| Comando | Função e exemplo |
|---|---|
| `.jtban` | Banimento temporário local. Exemplo: `.jtban 1h motivo`; exige duração explícita e pode usar `--purge N`. |
| `.banperm` | Banimento permanente local no chat atual. Exemplo: `.banperm @usuario motivo`. |
| `.jtmute` | Silencia temporariamente no chat atual. Exemplo: `.jtmute 30m motivo`. |
| `.kick` | Remove o usuário do chat sem banimento permanente. Exemplo: responda à mensagem com `.kick` ou use `.kick @usuario`. |
| `.unban` | Remove o banimento local. Pode ser usado respondendo, por ID ou por username. |
| `.unbanperm` | Remove o banimento permanente local. |
| `.unmute` | Remove o silêncio local. |
| `.blacklist` | Ativa blacklist local; mensagens recentes do alvo são apagadas neste chat. Aceita duração, como `.blacklist 1h`. |
| `.unblacklist` | Remove a blacklist local. |
| `.jtdel` | Apaga a mensagem respondida, sem aplicar punição. |
| `.jtdelwarn` | Apaga a mensagem respondida e aplica uma advertência ao autor. |
| `.jtpurge` | Apaga mensagens recentes do alvo. Use `.jtpurge 10`; protege mensagens fixadas por padrão. |
| `.purgeme` | Apaga mensagens recentes da própria conta. Use `.purgeme 10`. |
| `.jtpurgeall` | Apaga mensagens recentes em grande quantidade, com limite controlado. Use `--include-pinned` para incluir mensagens fixadas. |
| `.lock` | Fecha o envio para membros, mantendo administradores liberados. Guarda snapshot das permissões. |
| `.unlock` | Reabre o chat e restaura o snapshot de permissões anterior. |

Quando um comando de alvo for respondido a uma mensagem, o ID ou username pode ser omitido. A conta precisa ter permissões administrativas suficientes no chat.

## Advertências, antispam e quarentena

| Comando | Função e exemplo |
|---|---|
| `.jtwarn` | Adverte o alvo. Exemplo: `.jtwarn @usuario motivo`. |
| `.warns` | Consulta advertências do alvo no chat atual. |
| `.unwarn` | Remove uma advertência do alvo. |
| `.clearwarns` | Remove todas as advertências do alvo. |
| `.antispam on/off` | Ativa ou desativa a proteção antispam com pontuação, rajadas, links, mídia e duplicação. |
| `.quarantine on/off` | Ativa ou desativa a quarentena para padrões fortes de spam. |
| `.pinned on/off` | Define se mensagens fixadas ficam protegidas dos comandos de purge. |

O antispam utiliza múltiplos sinais e não deve punir um usuário por uma única mensagem isolada. Os limites podem ser ajustados pelas configurações existentes no banco local.

## Links

| Comando | Função e exemplo |
|---|---|
| `.antilink on/off` | Permite links somente a administradores e usuários autorizados no chat. |
| `.autorizarlink` | Autoriza um usuário específico a enviar links no chat. Pode ser usado por reply, ID ou username. |
| `.desautorizarlink` | Remove a autorização de links do alvo. |
| `.listlinkauth` | Lista usuários autorizados a enviar links no chat atual. |

## Controle global

| Comando | Função e exemplo |
|---|---|
| `.allban` | Banimento global em todos os chats compatíveis. Exclusivo aos proprietários. Aceita duração e `--purge N`. |
| `.allblack` | Blacklist global; apaga mensagens do alvo nos chats registrados. Exclusivo aos proprietários. |
| `.unallblack` | Remove a blacklist global. Exclusivo aos proprietários. |
| `.shadow` | Shadow ban global com duração opcional, como `.shadow 7d`. |
| `.unshadow` | Remove o shadow ban global. |
| `.maintenance on/off` | Ativa ou desativa o modo de manutenção. Exclusivo ao proprietário absoluto. |

Os comandos globais dependem dos chats registrados, das permissões da conta e das limitações de FloodWait do Telegram.

## Autorizações

| Comando | Função e exemplo |
|---|---|
| `.autorizar` | Autoriza um usuário a usar comandos. Aceita `10s`, `30m`, `10h`, `10d` e `1w`; sem duração, a autorização é permanente. |
| `.desautorizar` | Remove a autorização de um usuário. |
| `.listauth` | Lista autorizações ativas e suas expirações. |

Exemplos:

```text
.autorizar 30m @usuario
.autorizar 10d 123456789
.autorizar @usuario
.desautorizar @usuario
.listauth
```

Os proprietários absolutos são definidos somente no `.env`. Usuários autorizados recebem acesso aos comandos permitidos, mas não se tornam imunes a punições.

## Perfil da conta

| Comando | Função e exemplo |
|---|---|
| `.salvar` | Salva localmente nome, bio, username e foto atuais da conta. Deve ser usado antes de uma clonagem. Exclusivo ao proprietário. |
| `.clonar` | Copia nome, bio e foto do usuário-alvo. Use por reply, ID ou username. Exclusivo ao proprietário. |
| `.clonar --tag --confirmar @usuario` | Além dos campos anteriores, tenta aplicar o username se estiver disponível. A alteração é opcional e exige confirmação explícita. |
| `.restaurar` | Restaura o último backup criado por `.salvar`. Exclusivo ao proprietário. |

Exemplos:

```text
.salvar
.clonar @usuario
.clonar 123456789
.clonar --tag --confirmar @usuario
.restaurar
```

O backup fica em `data/` e não é enviado ao Telegram. Não apague o arquivo de backup enquanto ainda precisar restaurar o perfil.

## Segurança e diagnóstico

| Comando | Função e exemplo |
|---|---|
| `.antiblack on/off` | Modo Fênix por chat; tenta republicar mensagens recentes da própria conta quando recebe o evento de exclusão. |
| `.antispy` | Faz uma varredura de sinais de atividade administrativa suspeita no chat. |
| `.listspy` | Lista usuários monitorados pelo AntiSpy. |
| `.delspy` | Remove um usuário da lista de monitoramento. |
| `.status` | Mostra estado operacional resumido. |
| `.health` | Verifica conexão, sessão, banco e permissões do chat. |
| `.latency` | Mostra RPC de exclusão, latência E2E e idade do update. |
| `.logs` | Exibe logs de mensagens apagadas, quando autorizado. |

## Utilitários e mídia

| Comando | Função e exemplo |
|---|---|
| `.start` | Exibe a mensagem inicial e informações do Userbot. |
| `.help` | Exibe o guia interno de comandos. |
| `.id` | Mostra o ID do autor da mensagem respondida ou do alvo informado. |
| `.infojt` | Mostra informações detalhadas de um usuário por reply, ID ou username. |
| `.chats` | Lista chats registrados, com identificação e resumo operacional. |
| `.listdn` | Exibe punições locais/globais e respectivos estados. |
| `.msg` | Broadcast global de texto ou mídia, conforme as permissões do proprietário. |
| `.exu` | Envia uma imagem aleatória local de Exu. Se a imagem falhar, tenta um sticker WebP como fallback. |

O `.exu` usa os arquivos locais em `assets/exu/` e segue a política de autoexclusão padrão do Userbot.

## Regras de autoexclusão

As respostas dos comandos e as próprias mensagens de comando são programadas para exclusão automática conforme as constantes de configuração. Relatórios e diagnósticos usam uma janela maior para permitir leitura.

## Observações de permissão

A source usa uma conta de usuário autenticada no MTProto. Ela não contorna permissões do Telegram. A conta precisa ser administradora no grupo ou canal para excluir mensagens, aplicar restrições, banir usuários, alterar permissões ou enviar mídia em canais onde isso seja exigido.
