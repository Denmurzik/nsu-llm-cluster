# LiteLLM Proxy как шлюз университетского LLM-кластера

Дата исследования: 2026-11-02. Источники: исходный код `BerriAI/litellm` на теге `v1.106.6` (прочитан через `gh api`), docs.litellm.ai, GitHub issues. Всё, что не подтверждено кодом/доками или живым запуском, помечено **UNVERIFIED**.

## 0. Версия для пина

- Последний GA на 2026-11-02: **`v1.106.6`** (опубликован 2026-10-28). Docker: `ghcr.io/berriai/litellm:v1.106.6` (тот же digest, что у тегов `latest`, `1.106.6`, `main-stable`; digest `sha256:37e2dafb...0b`).
- Тегов вида `vX.Y.Z-stable` в GitHub Releases больше нет (проверено выборкой releases/tags: ни одного). "Stable" теперь = патч-ветка `stable/1.106.x` (v1.106.6 - backport-релиз). Пинить точный тег `v1.106.6`, не `latest`/`main-stable`.
- Варианты образа: `Dockerfile` и `docker/Dockerfile.database` в v1.106.6 структурно одинаковы (оба: Wolfi, Python 3.13, node, Prisma CLI+engines в `/opt/prisma`, pgbouncer, ENTRYPOINT `docker/prod_entrypoint.sh`, `CMD ["--port","4000"]`). Отдельный `-database` тег не нужен: брать основной образ. Docs/deploy тоже про `-database` не упоминает.
- Pre-release линейка: 1.107/1.108.0-rc.1, 1.109.0-dev.x - не брать.
- Подпись образов: cosign, ключ из коммита `e82a3c6` (см. release notes) - при смене ключа в патчах проверять cosign.pub.
- pip-эквивалент: `litellm==1.106.6`, `litellm-proxy-extras==0.4.108`, `requires-python >=3.10,<3.15`.

## 1. Virtual keys

Все management-вызовы: заголовок `Authorization: Bearer $LITELLM_MASTER_KEY`. Все эндпоинты ниже работают в OSS без лицензии, кроме явно отмеченных.

### POST /key/generate
Тело (`GenerateKeyRequest`, поля из исходника `key_management_endpoints.py` / `_types.py`):
- `key` (опц.) - **свое значение ключа**: обязано начинаться с `sk-`, >=16 символов. Если не задано - генерируется `sk-` + `secrets.token_urlsafe(16)` (~22 символа после `sk-`; длина настраивается env `LENGTH_OF_LITELLM_GENERATED_KEY`, дефолт 16). Следствие: портал может сам генерировать ключ (например, с префиксом) и знать его заранее.
- `key_alias` (str, <=255; **уникален среди всех ключей**, иначе ошибка "Key with alias ... already exists"; может проверяться regex `litellm_settings.key_alias_pattern`) - использовать как `"{user_id}:{slug}"`.
- `user_id`, `team_id`, `models` (list имен model_name; пусто = все модели), `aliases`, `duration` (`"30s"|"30m"|"30h"|"30d"`, null = бессрочно), `max_budget` (USD, float >=0), `budget_duration` (`"30d"`, `"1mo"`; без него бюджет не сбрасывается), `rpm_limit`, `tpm_limit`, `max_parallel_requests`, `metadata` (произвольный dict), `blocked`, `permissions`, `allowed_routes`, `key_type` (`llm_api` | `management` | `read_only` | `default`), `model_rpm_limit`, `model_tpm_limit`, `budget_limits`, `soft_budget`, `config`, `guardrails`, `auto_rotate`+`rotation_interval`, `tpd_limit` (только для batch).
- Ответ (`GenerateKeyResponse`): `key` (сырой `sk-...`, **единственный раз**), `key_name` (маска вида `sk-...abcd`), `token` / `token_id` (sha256-хэш), `expires`, `user_id`, `key_alias`, `models`, `max_budget`, `metadata`, `created_at` и др.
- **Enterprise-only при /key/generate**: `tags` ("Only premium users can add tags to keys"), `model_max_budget` ("You must have an enterprise license"), `enforced_params`, `permissions.get_spend_routes`, model access group на wildcard-моделях. `/key/regenerate` - **Enterprise** ("Regenerating Virtual Keys is an Enterprise feature"). `auto_rotate` по доке требует лицензию (раздел Key Rotations помечен Enterprise).
- Рекомендация: `key_type: "llm_api"` (или `allowed_routes: ["llm_api_routes"]`), чтобы пользовательский ключ не мог дергать management-роуты (по умолчанию `default` разрешает часть self-service роутов типа `/key/generate`, `/user/info` для своего user_id). Точный набор default-роутов - UNVERIFIED, но `llm_api` безопаснее.
- `/key/generate` **не создает пользователя автоматически** (в `generate_key_helper_fn`: "do not auto-create users for /key/generate"). Сначала `/user/new`, иначе `/user/info?user_id=` вернет 404, а spend будет учитываться только по ключу.

### Ключ после создания
- В БД хранится только sha256-хэш (`LiteLLM_VerificationToken.token`) + `key_name` (маска). Сырой ключ **не восстановим**: ни `/key/info`, ни `/key/list` его не возвращают. Показывать пользователю один раз; либо портал сам хранит зашифрованную копию (если хочет "показать снова" - при использовании `key` собственного значения; решение продуктовое).
- `/key/info` принимает как сырой ключ, так и хэш (`token_id`); док рекомендует хэш, чтобы ключ не попадал в access-логи.

### GET /key/list
Query: `page` (>=1), `size` (1..100, дефолт 10), `user_id` (точное совпадение; `substring_matching=true` - только админ), `team_id`, `organization_id`, `key_hash`, `key_alias`, `search`, `return_full_object` (bool), `include_team_keys`, `include_created_by_keys`, `sort_by` (`user_id|created_at|spend`), `sort_order`, `expand` (`user`), `status` (`active|expired|revoked|deleted`), `expires` (`active|expired`), `project_id`, `access_group_id`, `agent_id`, `substring_matching`.
Ответ: `{"keys": [...], "total_count", "current_page", "total_pages"}`; без `return_full_object` - только список хэшей; с ним - объекты с `token` (хэш), `key_name`, `key_alias`, `spend`, `max_budget`, `expires`, `models`, `metadata`, `user_id`, `rpm_limit`, `tpm_limit`.
Пример: `GET /key/list?user_id=<nsu_id>&return_full_object=true&size=100`.

### GET /key/info?key=<hash|raw>
Ответ `{"key": ..., "info": {key_alias, spend, max_budget, budget_duration, budget_reset_at, expires, models, status ("active|expired|revoked|deleted"), user_id, metadata, ...}}`. Без параметра `key` - информация о ключе из Authorization (пользователь сам). Есть недокументированный `POST /v2/key/info` (`include_in_schema=False`) с `{"keys": [...]}`.
Заметка: `spend` в `/key/info` - только текущее бюджетное окно, если задан `budget_duration`.

### POST /key/delete
Тело: `{"keys": ["sk-...", "<hash>"]}` или `{"key_aliases": ["alias1"]}`. Ответ: `{"deleted_keys": [...]}`. Удаленные попадают в архив `LiteLLM_DeletedVerificationToken` (виден через `/key/list?status=deleted`).

### POST /key/update
Тело: `{"key": "<sk-...|hash>", ...поля}`. Опущенные поля не меняются, явный `null` очищает (например `duration: null`). Редактируемо: `models`, `max_budget`, `budget_duration`, `rpm_limit`, `tpm_limit`, `metadata`, `blocked`, `key_alias`, `budget_limits`, `temp_budget_increase`/`temp_budget_expiry` (последние - Enterprise), и т.д. Также `POST /key/block`, `POST /key/unblock`.

### Учет использования в OSS
Работает без лицензии (проверено по исходнику: premium-гейт только у отмеченных ниже):
- `GET /user/info?user_id=<id>` - строка пользователя (`spend`, `max_budget`, лимиты) + `keys[]` с `spend` по каждому ключу + teams. Основной эндпоинт для портала.
- `GET /key/info` - `spend` ключа.
- `GET /spend/logs?api_key=<hash>&user_id=&request_id=&start_date=&end_date=&summarize=true|false` - **DEPRECATED**, не пагинируется, макс. 10 000 последних строк (заголовок `x-litellm-spend-logs-truncated: true`).
- `GET /spend/logs/v2` - пагинация `page`/`page_size` (<=1000), фильтры `api_key`, `user_id`, `team_id`, `model`, `min_spend`, `max_spend`, `start_date`, `end_date`; `total` кэпится на 10 000 (`total_is_capped`).
- `GET /user/daily/activity?start_date=YYYY-MM-DD&end_date=&model=&api_key=&user_id=&page=&page_size=(<=1000)&timezone=` - агрегаты по дням/моделям/ключам (токены, spend, requests). Есть и `/team/daily/activity`, `/organization/daily/activity` (не проверял). Дневные записи не сбрасываются бюджетными ресетами - лучше всего для графиков в портале.
- `GET /spend/keys`, `GET /spend/users` - списки по spend.
- `/global/spend` (скрыт из схемы, beta), `/global/spend/logs`, `/global/spend/keys`, `/global/spend/models`, `/global/activity` - OSS, но beta/UI-oriented.
- **Enterprise (HTTP 403 "not_premium_user")**: `/global/spend/report`, `/key/spend/report`, `/user/spend/report`, `/team/spend/report`, `/organization/spend/report`, `/spend/tags` (tag-based spend), `/global/spend/tags`.
- Хранение: `maximum_spend_logs_retention_period: "30d"` (general_settings, дефолт null = не чистить), `maximum_spend_logs_retention_interval: "1d"`. `disable_spend_logs` (дефолт false) - отключает построчные логи; влияет ли на агрегаты spend ключа/пользователя - **UNVERIFIED**, не включать. Тексты промптов в spend logs по умолчанию не пишутся (`store_prompts_in_spend_logs` - UNVERIFIED дефолт, вроде false).
- Записи spend батчатся в БД: `proxy_batch_write_at` (дефолт 10 с) - данные в `/user/info` отстают на это время.

### Важно про деньги/токены
У локальных `hosted_vllm`/`openai` моделей без цены стоимость = 0, поэтому `max_budget` (USD) бесполезен. Для токенных квот задать в `litellm_params` модели `input_cost_per_token` и `output_cost_per_token` (например `0.000001` - тогда 1 USD = 1M токенов), а в ключе/пользователе `max_budget` + `budget_duration`. Токены в логах (`prompt_tokens`, `completion_tokens`) пишутся и при нулевой цене. Прямой токенной квоты в OSS нет (`tpd_limit` только для batch). Бюджет проверяется до запроса по накопленному spend, поэтому возможен перерасход на величину последних запросов.

## 2. Users / teams в OSS

- `POST /user/new` - OSS. Поля (`NewUserRequest`): `user_id`, `user_email`, `user_alias`, `user_role` (`proxy_admin | proxy_admin_viewer | internal_user | internal_user_viewer`), `max_budget`, `budget_duration`, `models` (`["no-default-models"]` блокирует все), `tpm_limit`, `rpm_limit`, `max_parallel_requests`, `teams`, `metadata`, `sso_user_id`, `organizations`, `auto_create_key` (дефолт **true** - ставить `false`, иначе создастся лишний ключ), `duration`/`key_alias` (для авто-ключа), `send_invite_email`. `password` в запросе запрещен (422). Дубликат `user_id`/`user_email` -> ошибка (делать get-or-create: `GET /user/info` -> 404 -> `/user/new`).
- `GET /user/info?user_id=` - OSS (см. выше). `POST /user/update`, `POST /user/delete`, `GET /user/list`, `POST /user/bulk_update` - есть в коде; premium-гейтов в `internal_user_endpoints.py` не нашел (grep `premium_user` пуст).
- Teams (`/team/new` и т.д.) - базово OSS, детально не проверял (**UNVERIFIED**); для нашего случая достаточно user_id + ключей.
- Enterprise-only, которые могут зацепить: per-model budgets (`model_max_budget` на ключе/пользователе), tag-based spend/budgets, `/key/regenerate` и авторотация, JWT auth, audit logs, team-based logging, SSO для Admin UI свыше 5 пользователей ("SSO is free for up to 5 users"), spend reports (`*/spend/report`), secret managers, custom branding, часть guardrails (`hide_secrets`, `openai_moderations`), `enforced_params`.
- Лимита на число пользователей/ключей в OSS нет: `max_users` присутствует только в airgapped-лицензии (`litellm_license.py::is_over_limit`). Ограничение "5 users" относится к SSO в Admin UI - нам не важно (UI отключаем, SSO делает портал).
- Лицензионно: пакет `litellm-enterprise` ставится вместе с `litellm[proxy]` и лежит в образе (`/app/enterprise`), но функции активируются только валидным `LITELLM_LICENSE`; без ключа вызов premium-фич -> 403/ValueError, остальное работает.

## 3. Динамические модели

### Эндпоинты
- `POST /model/new` - тело `{"model_name": "gemma-3-27b", "litellm_params": {"model": "hosted_vllm/<served-model-name>", "api_base": "http://<ip>:<port>/v1", "api_key": "<опц.>", "rpm": ..., "tpm": ..., "input_cost_per_token": ..., "output_cost_per_token": ..., "timeout": ..., "max_retries": ...}, "model_info": {"id": "<свой id>", "mode": "chat", ...}}`. Ответ содержит `model_id`. Без `STORE_MODEL_IN_DB` -> 500 `"Set 'STORE_MODEL_IN_DB='True'' in your env to enable this feature."`. `litellm_params.model` - единственное обязательное поле внутри `litellm_params`.
- `model_info.id`: если задать, станет `model_id` в БД (код `add_model_to_db`: `_data["model_id"] = model_params.model_info.id`) - детерминированный id вида `node-10.0.0.5-8000` позволяет удалять без поиска. Дубликат id - ошибка (PK). Принимает ли `Deployment` произвольный id без валидации формата - **UNVERIFIED** на живом запуске.
- `POST /model/delete` - тело `{"id": "<model_id>"}` (именно POST, не DELETE). Удаляет из БД и сразу из `llm_router` (`llm_router.delete_deployment`). 404 если id нет в БД. Только для моделей из БД: модели из config.yaml через API не удалить.
- `GET /model/info` (+ алиас `GET /v1/model/info`) - полный список, `api_key` маскируются; `?litellm_model_id=<id>` фильтр (UNVERIFIED параметр). В ответе `model_info.id`, `model_info.db_model` (true для добавленных через API).
- `GET /v2/model/info` - пагинированная версия: query `model`, `user_models_only`, `include_team_models`, `debug`, `page` (>=1), `size` (дефолт 50), `search`, `modelId`, `teamId`, `sortBy` (`model_name|created_at|updated_at|costs|status`), `sortOrder`. Требует БД.
- `POST /model/update` (старый, только `litellm_params`) и `PATCH /model/{model_id}/update` (litellm_params или model_info, `litellm_params_to_remove`). Также `POST /model/block`, `POST /model/unblock`.
- `GET /model_group/info` - сводка по группам (кол-во деплойментов, лимиты).

### STORE_MODEL_IN_DB
Нужен: env `STORE_MODEL_IN_DB=True` **или** `general_settings.store_model_in_db: true` (в коде `if store_model_in_db is True`). Нужен Postgres. Параметры моделей шифруются `LITELLM_SALT_KEY` (fallback - `LITELLM_MASTER_KEY`); смена ключа после добавления моделей делает креды нечитаемыми. Модели в БД переживают рестарт; многоинстансная синхронизация - опрос БД раз в `proxy_config_reload_interval_seconds` (дефолт 30 с, env `PROXY_CONFIG_RELOAD_INTERVAL_SECONDS`); инстанс, принявший `/model/new`, применяет сразу (`proxy_config.add_deployment`). Настройки, записанные из UI, перекрывают config.yaml (мы UI отключаем).

### Группы балансировки
Несколько деплойментов с одинаковым `model_name` = одна группа, клиент обращается по `model_name`. Для каждой ноды - отдельный `/model/new` с тем же `model_name` и разным `api_base`. Если нужно разное поведение, ставить `rpm`/`tpm`/`weight` в `litellm_params` (используются `simple-shuffle` по весам).

### Стратегии роутинга (`router_settings.routing_strategy`)
Таблица в `Router.routing_strategy` (код): `simple-shuffle` (дефолт, рекомендован докой для прода; учитывает `weight`/`rpm`/`tpm`), `least-busy` (меньше in-flight), `latency-based-routing` (`routing_strategy_args: {ttl: ..., lowest_latency_buffer: ...}`), `usage-based-routing` (не рекомендован докой: perf), `usage-based-routing-v2` (async, учитывает tpm/rpm), `cost-based-routing`.
**Redis не обязателен ни для одной**: роутер использует `DualCache(in-memory + опц. redis)` (код `router.py`: Redis подключается только при заданных `redis_host/redis_port` или `redis_url`). Redis нужен только при >1 инстансе LiteLLM (общие счетчики, cooldown, rate limits). Для одного инстанса - без Redis. Для LLM-кластера с разной нагрузкой разумно `least-busy` или `simple-shuffle`; `usage-based-routing*` опираются на `tpm/rpm` деплойментов, которых у нас нет - не использовать. `latency-based-routing` на стриминге меряет время ответа - может смещать трафик на ноды с короткими ответами (поведение **UNVERIFIED**).

### Мертвые ноды: cooldown, retries, health
1. **Passive cooldown** (`router_settings`): `cooldown_time` (дефолт 5 с), `allowed_fails` (если не задан - правила по умолчанию), `allowed_fails_policy`, `num_retries` (дефолт 2 - `DEFAULT_MAX_RETRIES`), `retry_policy`, `disable_cooldowns`, `fallbacks`, `timeout`. Дефолтные правила (код `cooldown_handlers.py`): 429, 401/402/404/408 -> cooldown; >50% ошибок за текущую минуту при >=5 запросах -> cooldown; 100% ошибок при достаточном трафике -> cooldown; прочие 4xx не вызывают.
2. **Риск 1 (критично)**: в `_is_cooldown_required` явно сказано `ignored_strings = ["APIConnectionError"]` - "don't cooldown on litellm api connection errors". Отказ соединения (процесс vLLM упал, хост жив) в LiteLLM отображается как `APIConnectionError` => **пассивный cooldown для connection refused, по прочтению кода, не срабатывает**. Запрос при этом ретраится (`num_retries`) и может попадать на ту же мертвую ноду снова. Реальное поведение - **UNVERIFIED**, обязательно проверить интеграционным тестом (убить vLLM на одной ноде, послать 50 запросов).
3. **Риск 2**: для группы с **одним** деплойментом cooldown по умолчанию отключен ("avoid cooldowns on single deployment model groups") - единственная нода без fallback будет получать все запросы даже когда мертва.
4. **Health-check-driven routing** (есть в v1.106.6: `enable_health_check_routing` в `proxy_server.py`/`router.py`) - лечит риск 1. `general_settings`: `background_health_checks: true`, `enable_health_check_routing: true`, `health_check_interval: 60` (дефолт 300), `health_check_staleness_threshold` (дефолт 600; ставить явно, ~ 3x interval), `health_check_ignore_transient_errors: true` (игнор 429/408), `background_health_check_model_groups: [...]`, `health_check_concurrency`, `health_check_details`. Поведение: первая же неудачная проверка исключает деплоймент из роутинга до следующего успешного цикла; если неживы все - фильтр обходится (трафик идет). Per-model в `model_info`: `health_check_timeout` (дефолт 60 с), `health_check_max_tokens`, `disable_background_health_check`, `mode: chat`. Health-проба шлет реальный мини chat-запрос на каждую ноду (расход GPU, мелкий) каждые `health_check_interval`.
5. `GET /health` (с ключом): запускает реальные пробы (без `background_health_checks`) либо отдает кэш; `?model=` / `?model_id=`. Для liveness самого прокси не использовать.
6. Стратегия отказоустойчивости для портала: (а) health routing включить; (б) портал сам делает `GET http://<node>:<port>/health` (vLLM) или `/health` (llama.cpp) и вызывает `/model/delete` при исчезновении ноды / `/model/new` при появлении; (в) `num_retries: 2` + `fallbacks` между группами моделей, где применимо.

## 4. Минимальный конфиг и окружение

`config.yaml` (модели добавляются через API, `model_list` может быть пустым):
```yaml
model_list: []

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY      # обязан начинаться с sk-
  store_model_in_db: true
  proxy_batch_write_at: 10
  maximum_spend_logs_retention_period: "90d"
  background_health_checks: true
  enable_health_check_routing: true
  health_check_interval: 60
  health_check_staleness_threshold: 180
  health_check_ignore_transient_errors: true
  disable_error_logs: true                       # не писать исключения в БД

litellm_settings:
  request_timeout: 600
  drop_params: true                              # молча отбрасывать неподдерживаемые параметры
  set_verbose: false
  json_logs: true

router_settings:
  routing_strategy: least-busy                   # или simple-shuffle
  num_retries: 2
  cooldown_time: 30
  allowed_fails: 3
```

Env:
- `LITELLM_MASTER_KEY=sk-...` (обязательно, `sk-` префикс)
- `LITELLM_SALT_KEY=...` (отдельный, генерировать один раз и не менять; шифрует креды моделей)
- `DATABASE_URL=postgresql://user:pass@host:5432/litellm` (для TLS `?sslmode=verify-full&sslrootcert=...`)
- `STORE_MODEL_IN_DB=True`
- `DISABLE_ADMIN_UI=True`, `NO_DOCS=True`, `NO_REDOC=True`, `NO_OPENAPI=True` (последние три - по доке configs; в коде `ui_discovery_endpoints.py`/`admin_ui_utils.py::is_admin_ui_disabled` проверено только `DISABLE_ADMIN_UI`)
- `LITELLM_MODE=PRODUCTION` (не вызывает `load_dotenv()`), `LITELLM_LOG=ERROR`
- `NUM_WORKERS` (на одной VM = числу vCPU; в k8s `--num_workers 1`)
- По необходимости: `DISABLE_SCHEMA_UPDATE=true` (на serving-подах, когда миграции идут отдельной задачей), `LITELLM_PRISMA_MIGRATE_DEPLOY_TIMEOUT` (дефолт 600 с).

Миграции: при старте `litellm` (`proxy_cli.py`) вызывает `PrismaManager.setup_database(use_migrate=True)` -> `prisma migrate deploy` (в пакете `litellm-proxy-extras` 428 файлов миграций в этой версии) + `prisma generate`. Отключается `disable_prisma_schema_update`/`DISABLE_SCHEMA_UPDATE`. В образе Prisma CLI и query-engine предустановлены в `/opt/prisma` (env `PRISMA_BINARY_CACHE_DIR`, `PRISMA_CLI_PATH`, `PRISMA_OFFLINE_MODE=true`, `PRISMA_CLI_QUERY_ENGINE_TYPE=binary`), сети не нужно. Отдельный миграционный запуск: `docker/entrypoint.sh` (запускает `prisma_migration.py`; `run_server(--skip_server_startup)`). Postgres: версия не заявлена; в доке примеры PG 16 (UNVERIFIED нижняя граница).

Память/ресурсы: дока prod - **1 vCPU и 4 GiB на воркер** как request=limit, 4 GiB "floor" (консервативно). Реальный idle RSS одного воркера - сотни МБ (**UNVERIFIED**, замерить). Пул БД: `database_connection_pool_limit` (дефолт 10 на воркер). В k8s - 1 воркер на под.

Отключение телеметрии: в v1.106.6 прокси ничего не отправляет наружу по умолчанию; флаг `--telemetry` - hidden no-op (`proxy_cli.py`: "Deprecated no-op"). На `main` (с ~2026-10-14) появился opt-in модуль `litellm/telemetry` с `LITELLM_TELEMETRY_DISABLED=true` (veto), `LITELLM_TELEMETRY_GROUPS`; по умолчанию OFF. В v1.106.6 этого модуля нет (проверено деревом тега). При апгрейде до 1.107+ выставить `LITELLM_TELEMETRY_DISABLED=true` и перечитать release notes. Другие исходящие вызовы: загрузка `model_prices_and_context_window.json` с GitHub при старте - отключается `LITELLM_LOCAL_MODEL_COST_MAP=True` (по доке известный env; в этом исследовании не проверялся, **UNVERIFIED**) - важно для изолированной сети.

Healthcheck:
- `GET /health/liveliness` (алиас `/health/liveness`): без авторизации, 200 `"I'm alive!"`; 503 `{"status":"shutting_down"}` при остановке.
- `GET /health/readiness`: без авторизации, 200 `{"status":"healthy","db":...}`, 503 при недоступной БД; подробности скрыты, если не `allow_public_health_readiness_details: true`.
- Для Docker HEALTHCHECK: `curl -f http://localhost:4000/health/liveliness` (curl в образе Wolfi может отсутствовать - **UNVERIFIED**; использовать python: `python -c "import urllib.request;urllib.request.urlopen('http://localhost:4000/health/readiness')"`).

## 5. Стриминг к vLLM / llama.cpp

Провайдеры:
- vLLM: `litellm_params.model: hosted_vllm/<served-model-name>` (provider `hosted_vllm`; `vllm/` - deprecated, это SDK-режим). Класс `HostedVLLMChatConfig` наследует OpenAI-совместимый конфиг: `api_base` передается как `base_url` OpenAI-клиенту без дописывания пути (в `_get_openai_compatible_provider_info` только `api_base or HOSTED_VLLM_API_BASE`; `get_complete_url` не переопределяется) -> задавать **с `/v1`**: `http://<ip>:<port>/v1`. В доке LiteLLM примеры без `/v1` - противоречие, проверить живым запросом (**UNVERIFIED**). Ключ: `api_key` или env `HOSTED_VLLM_API_KEY` (иначе подставляется `"fake-api-key"`).
- llama.cpp `llama-server`: `model: openai/<name>`, `api_base: http://<ip>:<port>/v1`, `api_key: sk-dummy`. (Есть также `llamafile/`, `lm_studio/`, `openai_like/` - не требуются.) Различия: тип обработки `max_tokens`, отсутствие поддержки некоторых полей - `drop_params: true` страхует.
- `hosted_vllm` дополнительно маппит `reasoning_effort`/`thinking`; тулзы очищает от `strict`/`additionalProperties`.

Usage / stream_options:
- В v1.106.6 `common_request_processing.py::_stream_usage_tracking_updates`: для стрим-запросов `/chat/completions` прокси **автоматически добавляет** `stream_options.include_usage=true` (если модель поддерживает stream_options, `always_include_stream_usage` не `false`) и **скрывает** usage-чанк от клиента через `_litellm_strip_stream_usage`, если клиент его не просил. `general_settings.always_include_stream_usage: true` - добавлять всегда (и показывать). Это нужно для подсчета токенов/spend в стриме.
- vLLM отдает usage в финальном чанке при `include_usage` (стандартно). llama.cpp `llama-server` - поддерживает `stream_options.include_usage` в современных версиях (**UNVERIFIED** для вашей сборки); если usage не вернулся, LiteLLM считает токены своим токенайзером (`token_counter`, по умолчанию tiktoken - для Gemma/других моделей **приблизительно**; issue #8244 "Accurate token counting for vLLM-hosted models (Gemma, Mistral)" закрыт). Проверка: стрим через прокси, затем `/spend/logs/v2?api_key=...` - сравнить `prompt_tokens`/`completion_tokens` с ответом бэкенда.
- Известные баги в тек. версиях (открытые issues): #38926 - при `include_usage=true` аргументы tool_calls стримятся не инкрементально, а пачкой в конце (vLLM, 1.98.0-rc.1); #36168 - если апстрим шлет usage в чанке с непустым `choices`, usage теряется; #28735 (закрыт) - LiteLLM эмитит usage-чанк с непустым `choices`, что ломает клиентов, ждущих `choices=[]`; #40253 - искажение usage при цепочке прокси с reasoning-моделями. Статус в v1.106.6 по каждому - **UNVERIFIED**.
- #32112 (закрыт 2026-10-10): на версии 1.81.16 нестандартный параметр запроса (пример: `dry_multiplier` для llama.cpp) "залипал" в памяти прокси и добавлялся во все последующие запросы к тому же деплойменту (связано с `extra_body` в `litellm_params`). Закрыт, но в какой версии исправлено - **UNVERIFIED**; не использовать `extra_body` в `litellm_params` без регрессионного теста, и запретить клиентам нестандартные поля (`drop_params`/allowlist на уровне портала).
- Gemma + `hosted_vllm`: конкретных LiteLLM-багов именно под Gemma в GitHub issues не найдено (поиск по `hosted_vllm gemma`; полнота - UNVERIFIED). Общеизвестное поведение vLLM, не LiteLLM: чат-шаблон Gemma 2 отвергает роль `system` (HTTP 400) - решение на стороне vLLM (`--chat-template`) или клиента; Gemma 3 шаблон принимает system. Нужен тест на реальной модели.
- Таймауты: `litellm_settings.request_timeout` дефолт 6000 с; для стриминга выставить `timeout`/`stream_timeout` в `litellm_params` по необходимости.

## 6. LiteLLM рядом с другими процессами в одном контейнере

Рекомендация: **отдельный контейнер/сервис** LiteLLM + портал (обычный docker-compose). Если все-таки один контейнер:
- Вариант A (проще): взять образ `ghcr.io/berriai/litellm:v1.106.6` как базу, добавить портал (Python 3.13 уже есть, venv в `/app/.venv`), запускать через супервизор (s6/tini + два процесса). Образ на Wolfi (`apk`, glibc), по умолчанию root, запускает `litellm "$@"`; порт 4000. Для сторонних пакетов в образе нужен публичный Wolfi-репо (в образе добавлен `https://packages.wolfi.dev/os`). Non-root вариант: `docker/Dockerfile.non_root` (имя опубликованного тега - **UNVERIFIED**).
- Вариант B: `pip install "litellm[proxy,extra_proxy]==1.106.6"`. Нюансы: `proxy` включает FastAPI/uvicorn/gunicorn/granian/`litellm-proxy-extras`/`litellm-enterprise`, но **`prisma` лежит в `extra_proxy`** - без него БД-функции (ключи, модели) не заработают; Python `>=3.10,<3.15` (образ использует 3.13); Prisma CLI - Node-программа: нужен `node` в PATH либо при первом запуске Prisma скачает Node через nodeenv и CLI через npm (до 600 с; нужен интернет) и query-engine с `binaries.prisma.sh`; в изолированной сети заранее прогнать `prisma generate`, задать `PRISMA_BINARY_CACHE_DIR`, `PRISMA_CLI_PATH`, `PRISMA_OFFLINE_MODE=true` (как в Dockerfile). Конфликт зависимостей с портальным FastAPI/pydantic (`fastapi>=0.136.3`, `starlette>=1.0.1`, `pydantic>=2.12`, `httpx2`) - **держать в отдельных venv**. Плюс `polars`, `boto3`, `azure-*`, `mcp` - тяжелый footprint образа (сотни МБ+).
- Порты: LiteLLM 4000; наружу (к пользователям OpenAI-клиентов) публиковать только `/v1/*`; management-роуты (`/key/*`, `/user/*`, `/model/*`, `/spend/*`, `/health`) оставить на внутреннем интерфейсе - reverse proxy с allowlist путей. Master key никогда не выдается пользователям.
- Перезапуск процесса при миграциях: первый старт выполняет `migrate deploy` (десятки секунд - минуты на пустой БД с 428 миграциями), readiness `/health/readiness` станет 200 только после; портал должен ждать.

## 7. Итоговая схема интеграции портала

1. Логин NSU ID -> портал: get-or-create `POST /user/new {"user_id":"<nsu_sub>","user_email":..., "user_role":"internal_user","auto_create_key":false,"max_budget":N,"budget_duration":"30d","rpm_limit":..,"tpm_limit":..}`.
2. Выдача ключа: `POST /key/generate {"user_id":..., "key_alias":"<nsu_sub>:<name>", "models":[...], "key_type":"llm_api", "rpm_limit":.., "tpm_limit":.., "max_budget":.., "budget_duration":"30d", "duration":"90d", "metadata":{"portal":"1","label":..}}`; показать `key` один раз.
3. Список: `GET /key/list?user_id=..&return_full_object=true`; удаление: `POST /key/delete {"keys":["<token hash>"]}`; обновление лимитов: `POST /key/update`.
4. Статистика: `GET /user/info?user_id=` + `GET /user/daily/activity?user_id=&start_date=&end_date=`.
5. Ноды: `POST /model/new` с `model_info.id="node-<ip>-<port>"`; `POST /model/delete {"id":...}` при уходе ноды; периодически сверять `GET /v2/model/info` с реестром нод кластера.

## 8. Главные риски
1. Passive cooldown игнорирует `APIConnectionError` - мертвая нода (connection refused) может продолжать получать трафик; нужен `enable_health_check_routing` + удаление нод порталом + интеграционный тест.
2. Одна нода в группе - cooldown отключен по умолчанию; fallback отсутствует.
3. Стоимость локальных моделей = 0: без `input/output_cost_per_token` бюджеты и "квоты" не работают; прямой токенной квоты в OSS нет.
4. Сырой ключ невосстановим; потеря `LITELLM_SALT_KEY` = потеря кредов моделей в БД (ключи пользователей это не затрагивает, но модели придется добавлять заново).
5. Enterprise-гейты (`model_max_budget`, `tags`, `/key/regenerate`, `*/spend/report`, ротация) - не использовать в портале; проектировать на `/user/info`, `/user/daily/activity`, `/spend/logs/v2`.
6. Баги стриминга/usage (tool_calls burst при `include_usage`, потеря usage для нестандартных чанков, leaky `extra_body`) - нужен smoke-тест на реальных vLLM и llama.cpp до продакшена.
7. Быстрый темп релизов (нестабильные ветки 1.107+/1.109 dev; смена ключа подписи) - пин точного тега, обновление по расписанию с проверкой release notes и миграций БД; откат схемы БД не поддерживается автоматически (**UNVERIFIED**).
8. Сеть: автоматические исходящие запросы (cost map с GitHub, Prisma binaries) в изолированном кластере - использовать готовый образ с `/opt/prisma` и `LITELLM_LOCAL_MODEL_COST_MAP=True`.
