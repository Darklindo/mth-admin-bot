# Auditoria inicial do Jtzin Userbot

## Achados críticos identificados

1. `bot_v2.py` usa `logging.basicConfig` e `logging.getLogger`, mas não importa o módulo `logging`; isso causa `NameError` na inicialização.
2. `migrate_db.py` usa `sqlite3.connect` e `sqlite3` em várias linhas, mas não importa `sqlite3`; a migração falha imediatamente.
3. `requirements.txt` contém apenas `python-dotenv`, embora o código importe Telethon. A instalação em um ambiente novo pode falhar por dependência ausente.
4. `Database.execute()` retorna `None` após erros, mas vários métodos chamam `.fetchone()` ou `.fetchall()` diretamente no resultado. Isso pode gerar `AttributeError` secundário e esconder o erro original.
5. Diversos padrões de comandos ainda aceitam qualquer texto após o nome (`^\\.start`, `^\\.kick`, `^\\.banperm`, etc.); foi corrigido anteriormente apenas `.ban`, mas a auditoria deve padronizar todos os comandos para evitar prefixos e disparos indevidos.
6. O filtro global ignora qualquer mensagem cujo texto começa com `.`, inclusive mensagens de usuários punidos; isso pode permitir que mensagens de comando de usuários em blacklist não sejam apagadas.
7. O `.purgeme` e o `.purge` ainda usam valores padrão fora da regra solicitada de 5 a 100 e não rejeitam explicitamente valores menores que 5.
8. O `.purge` percorre somente 300 mensagens e o `.purgeme` também; isso limita a busca quando o chat é movimentado e não garante encontrar a quantidade pedida.
9. O `.msg` trata uma mensagem respondida como objeto diretamente em `send_message`, sem validar mídia ou conteúdo, podendo falhar dependendo do tipo de mensagem.
10. O `cmd_logs` usa `log.get(...)` após converter rows para dict em `get_latest_logs`, o que é válido atualmente, mas o tratamento de schema legado em `add_deleted_log` é frágil e deve ser consolidado.
11. O `detected_spies` usa `user_id` como chave primária; o mesmo usuário em chats diferentes sobrescreve o registro anterior.
12. `antiblack_tracker` registra somente texto e não preserva mídia, portanto o modo Fênix não consegue repostar fotos, vídeos, GIFs, stickers ou arquivos.

## Escopo da próxima etapa

Executar verificações automáticas de sintaxe, imports, regex de handlers, acesso ao SQLite, tratamento de exceções e fluxos de purge; depois corrigir os problemas sem remover comandos existentes.

## Data da auditoria

2026-08-11

## Autor

Manus AI

## Observação

Os arquivos analisados são `bot_v2.py`, `migrate_db.py` e `requirements.txt`. Nenhuma alteração foi aplicada nesta etapa; este documento registra apenas achados confirmados e hipóteses a validar.

## Achados adicionais confirmados

13. O repositório mantém `bot.py`, um bot legado baseado em `python-telegram-bot`, além de `bot_v2.py`, o Userbot Telethon. Isso pode causar confusão no deploy se o operador iniciar o arquivo errado.
14. `requirements.txt` não declara `telethon` nem `python-telegram-bot`, embora os dois módulos os importem; o script `update_bot.sh` instala apenas o que está declarado.
15. `update_bot.sh` usa `git pull` e continua o fluxo sem `set -e`; uma falha de atualização, instalação ou migração pode ser ignorada e o operador pode iniciar código incompleto.
16. `update_bot.sh` informa iniciar via `./watchdog.sh`, mas não inicia o processo; o comando é apenas impresso para execução manual.
17. A validação de sintaxe (`py_compile`) passou nos arquivos Python analisados. A migração em banco temporário também executou corretamente; o comando final de inspeção via CLI não pôde ser usado porque o binário `sqlite3` não está instalado no sandbox, não por falha da migração.
18. A análise dos handlers confirmou três pares de prefixos: `.ban`/`.banperm`, `.unban`/`.unbanperm` e `.purge`/`.purgeme`. Os dois últimos ainda não possuem delimitador explícito no regex; todos os handlers devem ser padronizados para `(?:\\s|$)`.

## Achados em testes e operação

19. `test_bot_logic.py` é um teste legado do framework `python-telegram-bot`: importa `ApplicationHandlerStop`, usa comandos com `/` e chama APIs (`set_setting`, `is_admin`, handlers) que não existem ou não correspondem ao `bot_v2.py` Telethon. Ele não é uma validação confiável do Userbot atual.
20. `test_reversal_logic.py` usa `sqlite3` sem import explícito, portanto falha ao executar isoladamente; além disso, não valida os handlers Telethon, apenas SQL simplificado.
21. `watchdog.sh` reinicia em loop após qualquer saída, inclusive encerramento intencional, e não verifica se `.venv/bin/activate` nem `bot_v2.py` existem antes de iniciar.
22. Existem scripts de auditoria temporários no diretório de trabalho; eles não devem ser publicados como parte do bot final.

## Resultados de execução

23. `test_reversal_logic.py` terminou com código 0, mas seu resultado é limitado a operações SQL e não cobre o Userbot.
24. `test_bot_logic.py` falhou com `ModuleNotFoundError: No module named 'telethon'` antes das correções de dependências; isso confirma que o ambiente novo não era reproduzível com o `requirements.txt` atual.
25. Após instalar Telethon apenas no ambiente de auditoria, o teste dedicado `test_userbot_audit.py` confirmou importação do módulo, criação do banco, autorização, resolução de username, logs e registro de espiões.

## Qualidade estática e segurança

26. Pyflakes não encontrou nomes indefinidos nos arquivos Python ativos, mas sinalizou imports não usados em `bot_v2.py`, que podem ser limpos.
27. O `.gitignore` protege `.env`, `data/` e ambientes virtuais, o que é adequado para evitar publicar credenciais e banco local.
28. `.env.example` ainda documenta apenas `BOT_TOKEN` e `OWNER_ID`, apesar do Userbot exigir `API_ID` e `API_HASH`; o onboarding do Termux fica inconsistente.
29. `bot_v2.py` contém valores padrão concretos para `API_ID` e `API_HASH`; a configuração deve exigir as variáveis no `.env`, sem fallback de credenciais reais.
30. Pyflakes confirmou que os scripts shell não devem ser passados ao analisador Python; não há erro de sintaxe Python adicional nos módulos analisados.

## Auditoria V6.6 — resultados consolidados

### Correções aplicadas

1. `bot_v2.py` agora importa `logging` explicitamente.
2. `requirements.txt` foi alinhado ao Userbot atual: `Telethon>=1.41,<2` e `python-dotenv`; a dependência legada do bot padrão foi removida.
3. `migrate_db.py` foi refeito para ser idempotente, habilitar WAL/busy timeout, criar índices e adicionar colunas ausentes sem apagar dados.
4. O `Database` agora só atualiza o cache quando a operação SQLite é concluída com sucesso.
5. `reply_or_edit` ganhou fallback sem HTML para evitar falhas de resposta por conteúdo inválido.
6. Os limites de `.purge` e `.purgeme` agora passam por um parser único, aceitam de 5 a 100 e percorrem até `MAX_HISTORY_SCAN` mensagens.
7. `.msg` agora transmite corretamente texto e mídia respondida; exceções silenciosas nos loops foram substituídas por logs de diagnóstico.
8. Datas antigas ou inválidas nos relatórios não derrubam os comandos; conteúdo e motivos são escapados antes do envio em HTML.
9. `watchdog.sh` e `update_bot.sh` agora usam o caminho do próprio script, validam o ambiente virtual e tratam encerramento manual.
10. `.gitignore` agora protege arquivos `*.session` e `*.session-journal` do Telethon.
11. Os padrões dos 30 handlers usam delimitador `(?:\\s|$)`; o verificador não encontrou conflito real entre `.ban`/`.banperm` ou `.purge`/`.purgeme`.
12. `.help`, logs e relatórios foram atualizados para V6.6 e linguagem profissional.

### Validações finais

- `py_compile` de `bot_v2.py` e `migrate_db.py`: aprovado.
- Importação real do módulo e registro dos handlers: aprovado; 34 handlers registrados.
- `test_userbot_audit.py`: aprovado.
- `test_reversal_logic.py`: aprovado.
- `bash -n` em `watchdog.sh` e `update_bot.sh`: aprovado.
- `pyflakes` em `bot_v2.py` e `migrate_db.py`: sem avisos.
- O antigo `test_bot_logic.py` permanece incompatível com a API Telethon atual, pois chama `global_security_filter` com a assinatura antiga do framework `python-telegram-bot`; isso é um teste legado, não um erro do Userbot V6.6.
