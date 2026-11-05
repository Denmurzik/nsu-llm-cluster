# Образ ноды NSU LLM

Один образ для любой машины кластера: llama.cpp (`llama-server`, `ggml-rpc-server`), агент k3s и режим
standalone без Kubernetes. Теги: `nsu-llm-node:0.1.0-cpu` и `nsu-llm-node:0.1.0-gpu` (CUDA 12.8, архитектуры
sm_86, sm_89, sm_120; драйвер хоста не ниже 570).

## Сборка

```bash
docker build --provenance=false -f node/Dockerfile --target cpu --build-arg JOBS=4 -t nsu-llm-node:0.1.0-cpu node
docker build --provenance=false -f node/Dockerfile --target gpu --build-arg JOBS=16 -t nsu-llm-node:0.1.0-gpu node
```

* llama.cpp тег `b12033`; скачивание моделей по HTTPS через `-DLLAMA_OPENSSL=ON` (в этом теге `LLAMA_CURL`
  объявлен устаревшим). Цель RPC: `ggml-rpc-server` из `tools/rpc`, в образе лежит как
  `/opt/llm/bin/ggml-rpc-server`.
* Пределы устройств подняты с 16 до 64 (`node/llama/patch-llama.sh`): `llama_max_devices()` правится `sed`, а
  `GGML_SCHED_MAX_BACKENDS` задаётся флагом `-DCMAKE_CXX_FLAGS=-DGGML_SCHED_MAX_BACKENDS=64`. Без этого лидер split не
  берёт больше 15 RPC-устройств, а для CUDA0 + CPU на 15 нодах нужно 30. Если исходники тега не такие, как ожидает скрипт,
  сборка падает. Проверка: `docs/verification.md`, «Фаза 2: сборка с поднятым пределом устройств».
* k3s `v1.36.5+k3s1`, контрольная сумма проверяется по `sha256sum-amd64.txt` релиза.
* GPU-образ содержит `nvidia-container-toolkit` (при сборке проверяется версия не ниже 1.20.1), чтобы k3s внутри
  контейнера регистрировал рантайм `nvidia`.
* Бинарники в `/opt/llm/bin`, `ENV PATH=/opt/llm/bin:$PATH LLAMA_CACHE=/var/lib/llm-models`,
  `ENTRYPOINT ["/opt/llm/bin/entrypoint.sh"]`, метка `org.opencontainers.image.version=0.1.0`.

## Скрипты в образе

| Скрипт | Назначение |
|---|---|
| `entrypoint.sh` | PID 1. Аргументы переданы (команда пода) — `exec "$@"`. `MODE=k3s` (по умолчанию) — `exec k3s agent`; `MODE=standalone` — `exec registrar.sh`. k3s обязан оставаться PID 1: эвакуацию корневого cgroup v2 он делает только в этом случае. |
| `serve-llama.sh` | Запуск `llama-server` из переменных окружения (таблица ниже). |
| `serve-rpc.sh` | `exec ggml-rpc-server -H 0.0.0.0 -p ${RPC_PORT:-18050} -t ${THREADS:-$(nproc)}`, плюс `-d $RPC_DEVICES` (например `CUDA0,CPU`: видеопамять и RAM ноды, порядок задаёт порядок `--tensor-split`) и `-c` при `RPC_CACHE=1` (кэш тензоров в `$LLAMA_CACHE/rpc`). Значения выставляет портал. `--print-cmd` печатает команду. Перед `exec` запускает в фоне `rpc-cache-gc.sh --loop`. |
| `rpc-cache-gc.sh` | LRU-очистка кэша тензоров `ggml-rpc-server -c` (`$LLAMA_CACHE/rpc`): `RPC_CACHE_MAX_GIB` (по умолчанию 5, 0 — без лимита), `RPC_CACHE_MIN_FREE_PCT` (10 % файловой системы свободно), проход раз в `RPC_CACHE_GC_INTERVAL_S` (600 с). |
| `registrar.sh` | Режим standalone: регистрация, heartbeat, запуск `llama-server` по `desired`. С `--report` (k3s, фоном; старое имя `--wake-report`) получает и хранит ключ ноды, сообщает сетевую карту для Wake-on-LAN и образ; с `--models-report` (k3s, фоном) раз в `MODELS_REPORT_S` (300 с) сообщает размер тома моделей. |
| `models-cleanup.sh` | Удаляет с тома моделей файлы, которые не нужны развёртыванию ноды, и кэш тензоров (задача 2-26): раздел «Очистка весов» ниже. |
| `detect-accel.sh` | `nvidia` или `cpu`; с `--vram` — VRAM первой GPU в МиБ; с `--ram` — память контейнера в МиБ (`MemTotal`, в WSL это память WSL; меньше, если у контейнера есть лимит cgroup). |
| `node-metrics.sh` | Сборщик мониторинга (задача 2-33). Запускается `entrypoint.sh` в фоне в обоих режимах (k3s и standalone) и раз в `METRICS_INTERVAL_S` секунд (20; допустимо 10-60) отправляет нагрузку ноды на `POST $MAIN_URL/api/nodes/$NODE_NAME/metrics` с токеном агента, как отчёт сетевой карты. Отправляет: по каждой GPU загрузку, VRAM занято/всего, температуру и мощность (`nvidia-smi`; чего он не отдаёт, то `null`), RAM занято/всего в пределах лимита контейнера (в WSL это память WSL), CPU % с прошлой отправки (`/proc/stat`), размер кэша моделей `$LLAMA_CACHE` и свободное место под ним (`du` раз в 5 минут), признаки «запущен `llama-server`» и «запущен `ggml-rpc-server`» (в режиме k3s процессы подов видны в контейнере ноды). Ответ не используется; портал отвечает 429 на отправку чаще раза в 5 секунд. Один сбой пишется в журнал один раз. `NODE_METRICS=0` выключает сборщик. `--once` печатает JSON одного замера. |

### Переменные окружения

`entrypoint.sh`: `MODE` (`k3s`|`standalone`), `MAIN_HOST`, `MAIN_URL` (по умолчанию `https://$MAIN_HOST`),
`CLUSTER_TOKEN` (токен подключения нод из раздела «Ноды»: токен агента k3s `K10…::node:…`), `NODE_NAME` (по умолчанию имя хоста, в нижнем регистре), `NODE_IP`, `ACCEL` (`auto`|`cpu`|`nvidia`),
`MAIN_INSECURE_TLS=1` (не проверять сертификат).
Токен в командные строки процессов не попадает: k3s агент получает его как `K3S_TOKEN`, `registrar.sh` передаёт curl
заголовок `Authorization` через файл (`-H @…`), `serve-llama.sh` так же передаёт `HF_TOKEN`; `run-node` передаёт
`CLUSTER_TOKEN` в `docker run` через окружение (`-e CLUSTER_TOKEN`).
Метки k3s: `llm.nsu/accel=<cpu|nvidia>`, для nvidia `llm.nsu/vram-mib=<МиБ>`, для всех нод `llm.nsu/ram-mib=<МиБ>` (из `RAM_MIB`,
который передаёт `run-node` по `docker info`, иначе `detect-accel.sh --ram`). По `ram-mib` портал считает бюджет RAM ноды
в split (по умолчанию 70 %), но не выше памяти, которую видит kubelet: метка ставится только при регистрации ноды.
`--node-external-ip` берётся из `NODE_IP`.

`serve-llama.sh`:

| Переменная | Значение |
|---|---|
| `MODEL_REPO`, `MODEL_FILE` | `--hf-repo R --hf-file F`. Если файл вида `…-00001-of-000NN.gguf`, скрипт сам скачивает все части `curl -L $HF_ENDPOINT/<repo>/resolve/main/<часть>` в `$LLAMA_CACHE/<repo, «/» → «__»>/` и запускает с `-m <первая часть>`. Часть на диске остаётся, если её размер и хэш содержимого совпадают с ответом `HEAD` (последний ответ цепочки редиректов, только код 200; хэш из `X-Linked-Etag`, его шлют и Hugging Face, и зеркало; записан в `<часть>.etag`). Ответ с ошибкой не повод качать заново. Части берутся из `resolve/$MODEL_REVISION`, без него из `resolve/main`: тогда, если репозиторий изменится между загрузкой частей на разных нодах, ноды получат разные версии (хэш поймает это только при следующем старте). |
| `MODEL_REVISION` | коммит Hugging Face (40 hex), который портал зафиксировал за развёртыванием. Все файлы, и одиночный тоже, скрипт качает сам из `resolve/<sha>` в `$LLAMA_CACHE/<repo, «/» → «__»>/` и запускает с `-m` (у `--hf-repo` в b12033 нет флага ревизии, он всегда берёт `main`). Пусто — `main`, как до фиксации. Для `local/*` не используется. Не коммит — выход с кодом 2 |
| `HF_ENDPOINT` | по умолчанию `https://huggingface.co`; поды k3s получают зеркало главного `https://<главный>/hf/c/<нода>` (`docs/hf-mirror.md`) |
| `HF_TOKEN` | необязательный токен (заголовок `Authorization`, для `--hf-repo` его читает сам llama-server); поды k3s получают ключ зеркала |
| `LLM_CA_BUNDLE` | набор CA для https-адреса зеркала, по умолчанию `/etc/llm-ca/bundle.crt` (ConfigMap `llm-ca`): если файл есть, скрипт выставляет `SSL_CERT_FILE` и `CURL_CA_BUNDLE` |
| `HF_FALLBACK_ENDPOINT`, `HF_FALLBACK_TOKEN` | standalone: собственные настройки ноды, когда `registrar.sh` получил зеркало главного; если `$HF_ENDPOINT/_mirror/health` не отвечает, скрипт качает отсюда |
| `KEEP_MODEL_REPOS` | репозитории всех развёртываний (`владелец/репо,…`, ConfigMap `llm-model-repos`): при старте скрипт удаляет папки частей других репозиториев в `$LLAMA_CACHE`, кроме `local__*`, `rpc` и `huggingface`; без списка ничего не удаляет. Копия ещё развёрнутой модели на переназначенной ноде остаётся (`docs/hf-mirror.md`) |
| `PUBLIC_MODEL_NAME` | `--alias` |
| `INFER_PORT` | 18080 |
| `CTX_SIZE`, `PARALLEL_SLOTS` | `--ctx-size`, `--parallel` |
| `THREADS` | по умолчанию `nproc - 1` (минимум 1): потоки llama.cpp ждут друг друга, и одно занятое ядро тормозит все |
| `THINKING` | `--reasoning`: по умолчанию `off` (иначе Gemma 4 тратит короткий `max_tokens` на `reasoning_content` и `content` пустой), `on` или `auto` включают рассуждения. Портал передаёт её из поля «Рассуждение» развёртывания, только если оно не `off` |
| `KV_CACHE_TYPE` | `f16` (по умолчанию, флагов нет), `q8_0` или `q4_0`: `--cache-type-k X --cache-type-v X`. `-fa auto` остаётся: b12033 сам включает flash attention для квантованного V-кэша. Другое значение — выход с кодом 2 |
| `SAMPLING_ARGS` | значения генерации по умолчанию: пары `--temp`, `--top-p`, `--top-k`, `--min-p`, `--repeat-penalty` и число (`--temp 0.7 --top-k 40`). Клиент может передать свои. Другой флаг или не число — выход с кодом 2 |
| `DRAFT_REPO`, `DRAFT_FILE`, `DRAFT_REVISION` | draft-модель для спекулятивного декодирования. Файл (все части) качается тем же `curl` с повторами, что и модель, из `resolve/<DRAFT_REVISION>` (без него `main`; `local/*` берётся у зеркала главного), и передаётся как `-md <файл> --draft-max 16 --draft-min 1`. В b12033 нет `--hf-file-draft`. `-ngld 99` на ноде с видеокартой, `-ngld 0` на CPU-ноде и на лидере split (там draft лежит в RAM). Репозиторий и файл только вместе. У rpc-серверов draft не бывает |
| `MMPROJ_FILE` | проектор изображений: файл репозитория модели (из `resolve/$MODEL_REVISION`) или `local/<модель>/<файл>`. Качается как draft, передаётся как `--mmproj <файл>`; без него остаётся `--no-mmproj`. На лидере split добавляется `--no-mmproj-offload`: иначе b12033 перенёс бы проектор на rpc-устройство, где место занято слоями |
| `EXTRA_ARGS` | свои флаги администратора, **по одному на строку** (портал разбил их `shlex`; значение с пробелом остаётся одной строкой). Добавляются в конец команды отдельными аргументами, без разбора оболочкой. Скрипт проверяет их ещё раз теми же правилами, что портал (`llama_args.validate_extra_args`): не больше 40 частей, печатный ASCII, начинаются с `-`, имя длинного флага сравнивается после замены `_` на `-` и в нижнем регистре со списком `/opt/llm/forbidden-flags`, длинные флаги `*-file`, `*-path`, `*-dir`, `*-url`, `*-key*` запрещены, значения с `/`, `\`, `~`, `.`, `..` запрещены. Нарушение или отсутствие списка — выход с кодом 2 и строка `EXTRA_ARGS refused: …` |
| `ACCEL` | `cpu`\|`nvidia`, иначе `detect-accel.sh` |
| `NGL` | по умолчанию 99, если задан `RPC_SERVERS` (слои попадают на RPC-устройства только через `-ngl`) или ускоритель `nvidia`; иначе 0. Лидер split получает от портала 999 |
| `RPC_SERVERS` | `--rpc … --fit off`; перед запуском скрипт до 300 с ждёт, пока каждый `host:port` примет TCP |
| `TENSOR_SPLIT` | только вместе с `RPC_SERVERS`: `--tensor-split`, целые числа слоёв на каждое RPC-устройство в порядке `--rpc`, внутри сервера в порядке его `-d`; считает портал |

Всегда: `--host 0.0.0.0 --port $INFER_PORT --jinja --metrics --cache-ram 512 -fa auto --ctx-size $CTX_SIZE
--parallel $PARALLEL_SLOTS --no-kv-unified`, плюс `--no-mmproj`, если нет `MMPROJ_FILE`. `--metrics` включает `/metrics` (Prometheus) для мониторинга портала: он закрыт тем же `LLAMA_API_KEY`, что и `/v1`, открытым остаётся только `/health`; администратор не может ни добавить, ни выключить флаг (он в `forbidden-flags`). Нода со старым образом `/metrics` не отдаёт: у такого развёртывания нет скорости токенов, очереди и слотов, пока образ не обновлён. `--print-cmd` печатает команду и список скачиваемых частей, ничего не запуская.

**Флаг отключения общего KV-кэша в b12033: `--no-kv-unified`** (`-no-kvu`; проверено по `llama-server --help` собранного
образа). С ним каждый слот получает ровно `CTX_SIZE / PARALLEL_SLOTS` токенов: при `CTX_SIZE=2048` и двух слотах
лог сервера показывает `n_slots = 2, n_ctx_slot = 1024, kv_unified = 'false'`, а `GET /props` отдаёт
`default_generation_settings.n_ctx = 1024`. Поэтому `ctx_size` в `desired` и `CTX_SIZE` пода — это суммарный контекст
(`ctx_per_request × parallel`), а на запрос приходится `ctx_size / parallel`.

## Очистка весов (`models-cleanup.sh`)

Задача 2-26. Скрипт удаляет с тома моделей (`$LLAMA_CACHE`, по умолчанию `/var/lib/llm-models`) то, что ноде не нужно; что именно нужно, решает портал и передаёт аргументами
(`docs/deploy-class.md`, «Очистка весов моделей на нодах»).

```
models-cleanup.sh [--dry-run] [--keep РЕПОЗИТОРИЙ:ФАЙЛ]... [--keep-rpc] [--keep-vllm]
models-cleanup.sh --usage
```

* `--keep owner/repo:file.gguf` (можно несколько раз; `local/<имя>:файл` для своих моделей) оставляет файл. Из многофайловой модели (`…-00001-of-0000N.gguf`) остаются все части, у оставленного
  файла остаются `.etag`, `.part`, `.part.etag`. Остальные файлы того же репозитория (старый квант) и каталоги других репозиториев удаляются.
* `--keep-rpc` оставляет кэш тензоров `rpc/` (нода в группе split своего развёртывания), `--keep-vllm` кэш `huggingface/`. Без этих флагов содержимое каталога удаляется, сам каталог остаётся (rpc-сервер пишет в него).
* Без аргументов удаляется всё. `--dry-run` ничего не удаляет и сообщает, сколько освободилось бы; `--usage` печатает размер тома в байтах (сумму размеров файлов).
* Не трогаются файлы, изменённые за последние `MODELS_CLEANUP_MIN_AGE_S` (60) секунд, ссылки и всё, что не похоже на веса. Путь тома проверяется: абсолютный, не короче двух уровней, без `..`.
* Вывод: строка `removed <путь> <байт>` на каждое удаление и последняя строка `RESULT {"freed_bytes":…,"models_bytes":…,"removed":[…],"skipped":[…],"dry_run":false}`
  (`models_bytes` — сколько осталось). Код 2 — неверные аргументы или небезопасный путь.

Откуда скрипт запускается: на ноде k3s из одноразового Job портала, на standalone-ноде из `registrar.sh` по приказу в ответе на heartbeat. Размер тома нода сообщает порталу сама:
`registrar.sh --models-report` (k3s, фоном из `entrypoint.sh`, `POST /api/nodes/models` `{"name","models_bytes"}` каждые `MODELS_REPORT_S` секунд, по умолчанию 300) или поле `models_bytes` в register и heartbeat (standalone).

## Режим standalone (`registrar.sh`)

Контракт реестра (реализован в портале, задача 10); везде заголовок `Authorization: Bearer $CLUSTER_TOKEN`. Портал принимает
токен агента (сырой или `K10…::node:…`) и, один релиз, серверный токен k3s нод, установленных до токена агента:

* Свой ключ ноды (задача SEC-1, `portal/app/nodes/identity.py`): первая регистрация имени возвращает `"node_key"` один раз;
  `registrar.sh` хранит его в `$STATE_DIR/node.key` (0600) и шлёт как `X-Node-Key: <ключ>` (через файл заголовков, не argv)
  во всех следующих вызовах, `node-metrics.sh` тоже. Из него выводится ключ бэкенда `sha256("nsu-llm-backend:" + ключ)` в
  `$STATE_DIR/backend/key` (его монтируют поды k3s, `LLAMA_API_KEY_FILE`/`VLLM_API_KEY_FILE`). Ответ 403 с
  `detail.code = node_key_invalid` (ключ сброшен администратором или нода удалена): ключ удаляется, нода регистрируется заново
  и ждёт подтверждения (`"approved": false` в ответах). Регистрация без ключа несёт `X-Node-Enroll-Nonce` (`$STATE_DIR/enroll.nonce`, до сохранения ключа): потерянный ответ повторяется и получает новый ключ вместо 409. В режиме k3s `registrar.sh --report` сначала вызывает
  `POST /api/nodes/enroll` `{"name"}`, потом шлёт отчёты и остаётся в фоне: раз в `ENROLL_RETRY_S` (60 с) подтверждает ключ и заменяет потерянный или отвергнутый. `entrypoint.sh` перезапускает его, если он завершился.
* `POST /api/nodes/register` `{"name","url","accel","vram_mib"|null,"ram_mib","version"}` →
  `{"ok":true,"heartbeat_interval":15,"approved":true|false,"node_key"?}`; `url = http://$NODE_IP:$INFER_PORT`. Известное имя без
  ключа: заявка (ключ только для heartbeat, `approved: false`, администратор выбирает её на «Нодах»); 409 `node_name_taken` только сверх 10 заявок.
* `GET /api/nodes/<name>/known` → `{"known","own_key","own_claim","approved","approval_required","message"}`: `run-node` печатает перед запуском, что будет и что нажать.
* `POST /api/nodes/heartbeat` `{"name","applied_spec_hash"|null}` → `{"ok":true,"heartbeat_interval":15,"desired":null|{…}}`;
  404 — зарегистрироваться заново. В `desired` есть `backend_api_key`: `registrar.sh` передаёт его `serve-llama.sh`
  в переменной окружения `LLAMA_API_KEY` (не в командной строке) и перезапускает сервер при его смене; `/health` ключа не требует.
* Если `GET /ca.crt` отвечает 404 (главный запущен со своим сертификатом, `--certs`), `registrar.sh` проверяет TLS по
  системному хранилищу сертификатов.
* В register и heartbeat есть необязательное `models_bytes` (размер тома моделей в байтах). Ответ на heartbeat может нести `purge`: приказы очистки `[{"id","keep":["репозиторий:файл"],"keep_rpc","keep_vllm"}]`.
  `registrar.sh` выполняет их в фоне `models-cleanup.sh` (по одному, оставляя то, что запущено сейчас) и сообщает итог в следующем heartbeat: `purge_result` `{"id","freed_bytes","models_bytes","removed","skipped"}`
  или `{"id","error"}`. Для нод k3s есть `POST /api/nodes/models` `{"name","models_bytes"}`.
* `POST /api/nodes/{name}/stop` → 204 при остановке контейнера (нода выходит из маршрутизации, запись и сетевая карта для Wake-on-LAN остаются); `DELETE /api/nodes/{name}` → 204 только при `-Uninstall`.

Поведение:

1. TLS: при первом запуске CA портала скачивается с `$MAIN_URL/ca.crt` (`curl -k`, один раз, trust on first use) в
   `/var/lib/llm-node/ca.crt`, дальше проверка идёт по нему. `MAIN_INSECURE_TLS=1` — всегда `-k`. Для `http://` TLS не используется.
2. Новый `spec_hash` в `desired`: `llama-server` останавливается (TERM, ожидание до 30 с, затем KILL), `serve-llama.sh`
   запускается в фоне с `MODEL_REPO`, `MODEL_FILE`, `MODEL_REVISION=model_revision`, `PUBLIC_MODEL_NAME`, `CTX_SIZE=ctx_size`, `PARALLEL_SLOTS=parallel`
   и настройками из `llm_env` (`THINKING`, `KV_CACHE_TYPE`, `SAMPLING_ARGS`, `DRAFT_*`, `MMPROJ_FILE`, `EXTRA_ARGS`: те же переменные, что получают поды k3s;
   `registrar.sh` передаёт только эти имена, остальные из `desired` игнорирует).
   Хеш попадает в `applied_spec_hash` только после `GET /health` = 200, и сразу отправляется внеочередной heartbeat.
3. `desired: null` — сервер останавливается, `applied_spec_hash` становится `null`.
4. Упавший `llama-server` перезапускается через 5 с, пока `desired` не изменился.
5. TERM/INT: `POST /api/nodes/{name}/stop`, остановка сервера, выход с кодом 0. Контейнер запускается с `--stop-timeout 45`.

## Запуск: `run-node.sh` (Linux) и `run-node.ps1` (Windows)

```bash
# k3s-нода (по умолчанию), адрес берётся из маршрута до главного узла
sudo ./run-node.sh --main 192.168.3.21 --token "$CLUSTER_TOKEN"
# standalone, GPU, без Kubernetes
sudo ./run-node.sh --main 192.168.3.21 --token "$CLUSTER_TOKEN" --mode standalone --accel nvidia
# удалить ноду
sudo ./run-node.sh --uninstall --main 192.168.3.21 --name "$(hostname)" --token "$CLUSTER_TOKEN"
```

Параметры: `--main HOST` (обязателен), `--token T` (обязателен, кроме `--uninstall`), `--mode k3s|standalone`,
`--accel auto|cpu|nvidia`, `--node-ip IP`, `--name N` (имя хоста в нижнем регистре; как и портал, принимает только
латинские буквы, цифры и дефис, до 63 символов), `--network bridge|host` (bridge),
`--image I` (`nsu-llm-node:0.1.0-<cpu|gpu>`), `--data-dir D` (`/var/lib/nsu-llm-node`), `--insecure-tls`, `--dry-run`
(печатает команды, ничего не выполняя), `--uninstall`. `HF_ENDPOINT`, `HF_TOKEN`, `THREADS`, `MODELS_REPORT_S` из окружения скрипта
передаются в контейнер.

* Образ должен быть на хосте (`docker load` или сборка). В режиме k3s скрипт делает
  `docker save --platform linux/amd64 $IMAGE -o $DATA_DIR/airgap/node.tar`, если `$DATA_DIR/airgap/node.id` не совпадает
  с id образа; k3s импортирует архив при старте, и поды ноды стартуют без реестра.
* Контейнер: `docker run -d --name llm-node --hostname $NAME --restart unless-stopped --stop-timeout 45`, тома
  `llm-models`, `$DATA_DIR/state`; для k3s добавляются `--privileged --tmpfs /run --tmpfs /var/run`, тома
  `llm-node-k3s`, `llm-node-kubelet`, `$DATA_DIR/airgap` и ulimit. В режиме standalone привилегии и тома k3s не нужны
  и не подключаются. Пароль ноды k3s (`/etc/rancher/node/password`) entrypoint хранит на томе `llm-node-k3s`
  (`node-password`), поэтому повторный запуск скрипта пересоздаёт контейнер, а нода входит в кластер под тем же именем.
  Если главный сервер всё же отвечает `Node password rejected` (нода запускалась из образа без этого исправления, либо
  `--uninstall` не смог выполнить `DELETE /api/nodes/<имя>` и объект ноды остался), удалите ноду на главном сервере
  (`docker exec llm-main k3s kubectl delete node <имя>`) и запустите `run-node` снова. Сеть bridge: `-p 18080:18080`, в режиме k3s ещё `-p 18050:18050` (standalone RPC не публикует); `--gpus all` для nvidia.
* Метки k3s применяются только при первой регистрации, поэтому скрипт хранит их в `$DATA_DIR/state/labels` и при
  изменении отвечает «Метки ноды изменились — выполните --uninstall и запустите снова».
* `--uninstall`: сначала `docker rm -f llm-node` (чтобы нода не зарегистрировалась снова), затем
  `curl -X DELETE $MAIN_URL/api/nodes/$NAME` (если есть токен; CA берётся из `$DATA_DIR/state/ca.crt`, который скрипт
  сохраняет при запуске; ответ не 204 и не 404 — предупреждение с командой ручного удаления ноды), затем удаление томов и `$DATA_DIR`. Для `--network host` дополнительно временным привилегированным
  контейнером удаляются интерфейсы `cni0`, `flannel.1` и цепочки iptables `KUBE-*`, `CNI-*`, `FLANNEL-*`
  (резервная копия iptables — `/tmp/nsu-llm-iptables.bak`). Очистка запускается из образа: `--image`, иначе образ существующего контейнера `llm-node` (читается до удаления), иначе любой локальный `nsu-llm-node:*`; без `docker pull`. Если образа нет, скрипт печатает предупреждение и пропускает очистку. На общем Docker-хосте используйте bridge.

`run-node.ps1` принимает те же параметры в стиле PowerShell (`-Main`, `-Token`, `-Mode`, `-Accel`, `-NodeIp`, `-Name`,
`-Network`, `-Image`, `-DataDir`, `-InsecureTls`, `-DryRun`, `-Uninstall`) и `-Wsl <дистрибутив>`: все команды docker идут
через `wsl -d <дистрибутив> -u root -e docker …` (`-Wsl default` — дистрибутив по умолчанию), сеть по умолчанию `host`, данные
внутри WSL; там же сохраняется CA портала и выполняется `DELETE` при `-Uninstall`. `HF_ENDPOINT`, `HF_TOKEN`, `THREADS`, `MODELS_REPORT_S` из
окружения передаются в контейнер, как в `run-node.sh` (в WSL — через `WSLENV`). Ошибка любой команды docker останавливает скрипт.
Скрипт проверяет `docker info`, для `-Accel` не `cpu` — `docker run --rm --pull never --gpus all --entrypoint nvidia-smi nsu-llm-node:0.1.0-gpu`
(локальный образ, `-Image` его заменяет; без загруженного gpu-образа `-Accel auto` выбирает cpu),
приводит имя хоста к нижнему регистру и создаёт правило брандмауэра `NSU-LLM-Node` (TCP 18080 и, в режиме k3s, 18050),
если запущен от администратора, иначе печатает команду. `-AllowFrom 10.1.2.0/24,10.9.0.5` ограничивает правило этими адресами
(иначе с любых). Порты и адреса правила сверяются при каждом запуске (правило пересоздаётся), `-Uninstall` удаляет правило.

Обновление контейнера по запросу главного (кнопка «Обновить» на странице «Ноды», `NODE_AUTO_UPDATE`): `run-node.sh --update` и
`run-node.ps1 -Update [-Wsl <дистрибутив>]`. Параметры (главный, токен, режим, адрес, сеть, каталог данных, сетевая карта) берутся у работающего
контейнера `llm-node`; скрипт спрашивает главного (`GET /api/nodes/<имя>/desired`), и только если тот велит, скачивает образ (`pull-image.sh --ca
state/ca.crt --record state/image.digest`) и создаёт контейнер заново обычным путём установки (без брандмауэра). Неудача уходит главному
(`POST /api/nodes/<имя>/image`, поле `update_error`). Версию, коммит и digest образа контейнер сообщает сам (`registrar.sh --report` в режиме k3s,
после регистрации в standalone; `NODE_VERSION`, `NODE_REVISION` из образа, `NODE_IMAGE`, `NODE_IMAGE_DIGEST` от `run-node`). `node-boot.ps1` вызывает
`run-node.ps1 -Update` раз в минуту (`-UpdateEverySec`), на Linux это таймер systemd, подробнее: `docs/release.md`, «Обновление нод».

`install-node.ps1` (Windows, от администратора) ставит ноду целиком: WSL2, дистрибутив `NSU-LLM`, Docker CE, `.wslconfig`,
задачу автозапуска без входа пользователя (`node-boot.ps1`), электропитание, брандмауэр и Wake-on-LAN физической сетевой карты
(`-PowerButton nothing|keep`), затем вызывает `run-node.ps1 -Wsl` (`-WolMac/-WolIp/-WolPrefix`: нода сообщает карту порталу).
Интерактивный по умолчанию, `-NonInteractive` и `-AnswersFile` для автоматизации, `-DryRun`, `-Status`, `-Uninstall`.
Рядом должны лежать `secure-dir.ps1` (защита `C:\ProgramData\NSU-LLM` до любой записи, общий с `class-deploy.ps1`)
и `class-run.ps1` (исполнитель разовой задачи планировщика для сеансов без учётных данных).
Подробно: `docs/deploy-class.md`, раздел «Нода на Windows одной командой».

## Тесты

```bash
bash node/tests/test_scripts.sh                 # без docker: синтаксис, serve-llama --print-cmd, run-node --dry-run
bash node/tests/test_install_node.sh            # без WSL и docker: install-node.ps1 -DryRun, wsl-setup.sh, node-boot.ps1 с подменой команд
bash node/tests/test_node_metrics.sh            # без docker: сборщик мониторинга с подменой nvidia-smi, curl и /proc, его запуск из entrypoint.sh, --metrics в serve-llama
bash node/tests/test_dockerfile.sh              # без docker: патч пределов llama.cpp и его проводка в Dockerfile
bash node/tests/test_update_node.sh            # без WSL и docker: run-node.ps1 -Update и цикл обновлений node-boot.ps1 с подменой wsl, docker, curl (Windows PowerShell 5.1)
bash node/tests/test_standalone.sh              # docker: cpu-образ + mock_main.py, модель ggml-org/gemma-3-270m-it-GGUF
bash node/tests/gpu_smoke.sh                    # только на GPU-сервере: gpu-образ в режиме standalone
bash node/tests/test_class_deploy.sh            # без сети: class-deploy.ps1 с подменой WinRM/SMB (нужен Windows PowerShell 5.1)
```

`test_standalone.sh` запускает контейнер `it-t2-standalone` на порту 28080 и проверяет: регистрацию и поля, heartbeat
с `h1`, ответ чата, контекст слота, отказ на слишком длинный запрос, повторную регистрацию после 404, смену конфигурации
(`h2`, контекст 4096), `desired: null`, `POST .../stop` (без `DELETE`) и код выхода 0 при `docker stop`. Тесты, запускающие контейнеры, на машине
разработки идут через `test-lock`.

## Проверка GPU-образа

Сборка и проверка выполнены на `newbar` (RTX 5090, Ubuntu 24.04, Docker 29) в `~/nsu-llm-build` отдельным builder
`nsu-llm-builder` с `nice -n 19`. После проверки образ, builder и тома удалены.

Результат `tests/gpu_smoke.sh` (режим standalone, модель `ggml-org/gemma-3-270m-it-GGUF` Q8_0, ctx 2048, 2 слота):

| Параметр | Значение |
|---|---|
| GPU | NVIDIA GeForce RTX 5090, 32607 МиБ (драйвер с CUDA 12.8 и выше) |
| Регистрация | `accel=nvidia`, `vram_mib=32607` (определено внутри контейнера) |
| Скорость | генерация 982 токена/с, обработка промпта 1557 токенов/с (второй запрос, 25 токенов) |
| VRAM | процесс `llama-server` контейнера в `nvidia-smi`: 896 МиБ; занятость GPU 1405 → 2832 МиБ |
| Образ | `nsu-llm-node:0.1.0-gpu`, 6,38 ГБ; `nvidia-container-toolkit` 1.20.1 |

Скрипту нужен только docker; mock-портал запускается во втором контейнере (`it-t2-mock`, образ `python:3.11-slim`)
в одной приватной сети с нодой, потому что брандмауэр хоста не пропускает трафик контейнер → хост. Собственная
загрузка модели `llama-server --hf-repo` использует cpp-httplib без перебора адресов: на хосте с адресом IPv6 без
маршрута (так на `newbar`) при `--network host` она падает с `HTTPLIB failed: Could not establish connection`
(в bridge работает). `curl` в том же контейнере работает, поэтому для частей GGUF скачивание идёт через `curl`.
