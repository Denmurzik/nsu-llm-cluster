# Инференс Gemma 4 E4B на кластере: vLLM (GPU), llama.cpp (CPU), разбиение модели

Дата исследования: 2026-11-02. Источники: HF API (`curl https://huggingface.co/api/models/<id>`), Docker Hub / ghcr.io registry API, GitHub API (релизы, issues, PR), docs.vllm.ai, recipes.vllm.ai, lws.sigs.k8s.io, llama.cpp README. Всё, что не проверено напрямую, помечено **UNVERIFIED**.

## 0. Главные выводы

1. **`google/gemma-4-E4B-it-qat-w4a16-ct` существует, но НЕ помещается с запасом в 12 GB.** Файл 11.51 GB (10.7 GiB). Причина: `embed_tokens_per_layer` (PLE) = 5.64 GB остаётся в BF16, плюс `lm_head` 1.34 GB и `embed_tokens` 1.34 GB в BF16, аудио-энкодер 0.62 GB, vision 0.34 GB. Квантованные слои трансформера занимают всего 2.22 GB. Автор PR vllm-project/vllm#62354 измерил `model_memory_usage` для E4B QAT w4a16 = **11.02 GiB** на GPU. Для 4070 (12 GiB, из них часть занята WDDM/WSL2/CUDA-контекстом) это почти нулевой KV-кэш.
2. Выгрузка PLE на CPU (`--cpu-offload-gb 6 --cpu-offload-params embed_tokens_per_layer`) снижает вес на GPU до 5.77 GiB без заметной потери латентности (данные автора PR, RTX 4090), **но PR #62354 на 2026-10-29 всё ещё open (не влит)**, и до его слияния этот флаг для PLE — no-op. Плоский `--cpu-offload-gb 6` без PR выгружает только слои декодера и даёт 11.97 с вместо 0.80 с на запрос (512 in / 128 out), т.е. ~15x замедление.
3. Поэтому для GPU-ноды 12 GB есть три рабочих пути (см. §1.6). Самый предсказуемый: **llama.cpp CUDA-образ на GPU-ноде** (`ghcr.io/ggml-org/llama.cpp:server-cuda12-b12033`, GGUF ~5 GB). Он даёт тот же OpenAI API, тот же набор квирков и один тип рантайма на обеих нодах.
4. Gemma 4 **не gated**: Apache-2.0, `gated: false` у всех проверенных репозиториев. HF token и принятие лицензии не нужны.
5. Официальные образы llama.cpp собраны **без `-DGGML_RPC=ON`** (проверено по `.devops/cpu.Dockerfile` и `.devops/cuda.Dockerfile`; в `ggml/CMakeLists.txt` `GGML_RPC` по умолчанию OFF). Для RPC-разбиения нужен собственный образ.

## 1. vLLM на GPU-ноде (RTX 4070 12 GB, Windows + Docker Desktop/WSL2)

### 1.1 Версия и образ

- Gemma 4 появилась в vLLM 0.19.0 (баг-фиксы в патче **0.19.1**, транскрипт релиза: "bug fixes for Gemma4"; бейдж recipes.vllm.ai: "vLLM 0.19.1+"). Это абсолютный минимум; на нём ещё много багов tool-парсера.
- Архитектура `Gemma4UnifiedForConditionalGeneration` (12B) присутствует в дереве с **v0.23.0** (файл `vllm/model_executor/models/gemma4_unified.py` есть в v0.23.0, нет в v0.22.0).
- Последние релизы: v0.32.0 (2026-10-18), v0.33.0 (Docker-тег опубликован 2026-11-02; GitHub-релиз на момент проверки помечен `v0.33.0rc3`, не prerelease).
- **Рекомендуемый пин: `vllm/vllm-openai:v0.32.0`** (amd64 ~9.21 GB сжато по данным Docker Hub; unpacked заметно больше, UNVERIFIED). Образ по умолчанию на CUDA 13 (torch 2.13+cu130), требует драйвер хоста R580+. Для старых драйверов: `vllm/vllm-openai:v0.32.0-cu129` (~11.18 GB).
  - На Windows драйвер должен поддерживать CUDA 13 внутри WSL2: проверить `nvidia-smi` (R580+), иначе брать `-cu129`.
  - v0.33.0 вышел сегодня, на нём нет наработанных отчётов. Если нужна свежая отладка tool-парсеров, имеет смысл пробовать v0.33.0 после v0.32.0.
- Образ vLLM содержит `/vllm-workspace/examples/` (в Dockerfile `WORKDIR /vllm-workspace`, `COPY examples examples`), поэтому `--chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja` работает. Issue #47600 (шаблон "appears path-like, but doesn't exist") относится к pip-установке, где `examples/` нет.
- Пример из LWS-репозитория пинит `vllm/vllm-openai:v0.28.0`.

### 1.2 Чекпоинты (все `gated: false`, license apache-2.0)

| id | размер | комментарий |
|---|---|---|
| `google/gemma-4-E4B-it` | model.safetensors 15.99 GB (BF16) | на 12 GB не влезает; с PLE-offload (PR #62354) 9.80 GiB на GPU |
| `google/gemma-4-E4B-it-qat-w4a16-ct` | 11.51 GB | compressed-tensors W4A16; на GPU 11.02 GiB без offload, 5.77 GiB с PLE-offload |
| `google/gemma-4-E4B-it-qat-q4_0-unquantized` | n/a | BF16 из QAT-конвейера, для самостоятельной квантизации |
| `google/gemma-4-E2B-it-qat-w4a16-ct` | не измерялось | существует (есть в листинге HF) |
| `google/gemma-4-12B-it-qat-w4a16-ct` | 10.26 GB | эффективно без PLE; embed 2.01 GB + lm_head 2.01 GB в BF16, слои 6.13 GB |
| `google/gemma-4-31B-it-qat-w4a16-ct` | 23.27 GB | |
| `88plug/Gemma4-E4B-it-W8A16` | заявлено ~5 GB | сторонний, статус "Provisional"; заявление "5 GB" противоречит тому, что PLE в BF16 сам весит ~5.25 GiB. **UNVERIFIED, не использовать без проверки.** |
| RedHatAI FP8 / AWQ для E4B | **не найдено** | для 31B recipes.vllm.ai упоминает `RedHatAI/gemma-4-31B-it-FP8-dynamic` и `nvidia/gemma-4-31B-it-NVFP4` (Blackwell); для E4B аналогов не проверял |

Онлайн-квантизация `--quantization fp8` на E4B: открытый баг #63732 (с v0.23.0 запросы с изображениями падают; текст не проверен). Кроме того, PLE всё равно остаётся BF16, выигрыш невелик.

### 1.3 Флаги `vllm serve` для 12 GB (вариант с PLE-offload, требует патча #62354)

```
vllm serve google/gemma-4-E4B-it-qat-w4a16-ct \
  --served-model-name gemma-4-e4b \
  --host 0.0.0.0 --port 8000 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --kv-cache-dtype fp8 \
  --max-num-seqs 8 \
  --limit-mm-per-prompt '{"image": 0, "audio": 0}' \
  --cpu-offload-gb 6 --cpu-offload-params embed_tokens_per_layer \
  --enable-auto-tool-choice \
  --tool-call-parser gemma4 \
  --reasoning-parser gemma4 \
  --chat-template /vllm-workspace/examples/tool_chat_template_gemma4.jinja
```

Основание по каждому флагу:
- `--tool-call-parser gemma4`, `--reasoning-parser gemma4`, `--enable-auto-tool-choice`, `--chat-template examples/tool_chat_template_gemma4.jinja`: из recipes.vllm.ai (31B и 12B). Оба парсера нужны одновременно: иначе issue #57231/#57232 (без reasoning-parser `<|channel>` просачивается в content).
- `--kv-cache-dtype fp8`: recipe говорит "saves about 50% KV memory". Совместимость с SWA-слоями Gemma 4 на Ada: **UNVERIFIED**.
- `--limit-mm-per-prompt '{"image": 0, "audio": 0}'`: рецепт предлагает для text-only. Освобождает ли это веса энкодеров на GPU (0.96 GB у E4B): **UNVERIFIED**; для профилирования памяти точно полезно.
- `--max-model-len 8192`: подобрано мной под остаток памяти; рецепты используют 16384 (full-featured) и 32768. Контекст модели 131072. **UNVERIFIED на 4070.**
- `--gpu-memory-utilization 0.90`: значение из рецептов. На 12 GB в WSL2 с подключённым дисплеем может потребоваться 0.85.
- `--cpu-offload-gb 6 --cpu-offload-params embed_tokens_per_layer`: рекомендация из PR #62354. Флаг `cpu_offload_params` в конфиге есть (проверено в `vllm/config/offload.py` v0.33.0rc3), но для PLE работает только после слияния PR. UVA-offload в WSL2: в v0.24.0 есть "pinned memory for WSL2 (#41496)", работоспособность UVA на WSL2 **UNVERIFIED**.
- `--enforce-eager`: освобождает память под CUDA graphs (порядка сотен MB, UNVERIFIED), ценой скорости. В рецептах для GPU не используется (только Intel XPU). Добавлять только при OOM на старте.
- Мультимодальность: бюджет токенов картинки по умолчанию 280, допустимо 70/140/280/560/1120 (`--mm-processor-kwargs '{"max_soft_tokens": 560}'`).
- MTP/spec-decoding: `--speculative-config`, нужен отдельный assistant-чекпоинт (`google/gemma-4-E4B-it-assistant`, `...-qat-q4_0-unquantized-assistant`); для QAT-цели ассистент тоже должен быть QAT. На 12 GB не нужен.

Если PLE-offload недоступен, на 12 GB вариант одно-GPU для vLLM реалистичен только с `google/gemma-4-12B-it-qat-w4a16-ct` (весит ~9.56 GiB, KV остаётся ~1 GiB: контекст порядка 2-4k токенов, **UNVERIFIED**) или с самосборным образом с cherry-pick PR #62354.

### 1.4 HF token

Не нужен: `google/gemma-4-E4B-it`, `...-qat-w4a16-ct`, `12B`, `31B` имеют `gated: false`. Всё равно полезно пробросить для rate-limit: `-e HF_TOKEN=...` (Docker) или `env: HF_TOKEN` из Secret (k8s). Кэш: `-v ~/.cache/huggingface:/root/.cache/huggingface` (в k8s: hostPath/PVC на `/root/.cache/huggingface`, иначе 11.5 GB качаются при каждом рестарте).

### 1.5 Эндпоинты и k8s

- Health: `GET /health` (200, пустое тело; исходник `vllm/entrypoints/serve/instrumentator/health.py` в v0.32.0). Также `/v1/models`, `/v1/chat/completions` (stream), `/metrics` (Prometheus). Во время загрузки порт не слушается, поэтому в k8s нужен `startupProbe` с большим `failureThreshold` (загрузка + компиляция + CUDA graphs занимают минуты).
- Docker-запуск из рецептов: `--ipc=host --network host --shm-size 16G --gpus all`. В k8s: `emptyDir` `medium: Memory` на `/dev/shm`, `hostNetwork: true`.
- Docker Desktop/WSL2: контейнер с `--gpus all` работает. k3s-агент на Windows-хосте с NVIDIA device plugin в WSL2 — отдельная нетривиальная тема; **UNVERIFIED**, в рамках этого исследования не проверялось.

### 1.6 Варианты для GPU-ноды 12 GB (по убыванию надёжности)

| # | вариант | вес на GPU | риск |
|---|---|---|---|
| A | **llama.cpp CUDA**: `ghcr.io/ggml-org/llama.cpp:server-cuda12-b12033` + GGUF Q4_K_M (4.98 GB), `-ngl 99` | ~5 GB + KV | образ 2.6 GB сжатый; тот же рантайм, что на CPU-ноде; нет continuous-batching класса vLLM, но для 1-4 одновременных запросов достаточно |
| B | vLLM v0.32.0 + QAT w4a16-ct + **кастомный образ с PR #62354** | 5.77 GiB | PR не влит; UVA в WSL2 не проверено |
| C | vLLM + `google/gemma-4-12B-it-qat-w4a16-ct` без offload | ~9.56 GiB | мало места под KV; другая модель, чем выбрана |
| D | vLLM + плоский `--cpu-offload-gb` без PR | ~7.98 GiB (QAT) | ~15x медленнее (11.97 с против 0.80 с), не рекомендуется |

Ожидаемая скорость vLLM на 4070 (поток 1): грубо 50-90 tok/s на декоде для 4-битной E4B (пропускная способность 504 GB/s; по агрегаторам ~70 tok/s на 4000 Ada Laptop 12 GB для Q4_K_M в llama.cpp). **UNVERIFIED**, прямых бенчмарков на 4070 нет.

## 2. llama.cpp на CPU-ноде (i5-4570, 4 ядра, AVX2, ~7.9 GB RAM, Ubuntu 24.04)

### 2.1 Образ

- Пин: **`ghcr.io/ggml-org/llama.cpp:server-b12033`** (манифест проверен: amd64, arm64, s390x; amd64 ~309 MB сжато). Самый свежий числовой тег `server-bNNNNN` в ghcr на момент проверки; GitHub-релизы b12034/b12038 уже есть, но образы под ними на ghcr не опубликованы (манифест 404).
- Поддержка Gemma 4 в llama.cpp добавлена в апреле 2026 (PR #21343, #21488, #21492, #21534, #21625, #21661, #21697, #21704, даты 2026-04-03..10); `b12033` намного новее. Минимальный билд с поддержкой: порядка b86xx (в issue #21375 фигурирует build 8643 с `peg-gemma4`); точный первый тег **UNVERIFIED**.
- CPU-образ собран с `-DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON`: рантайм выбирает вариант под CPU (в логах issue #21375 виден `libggml-cpu-haswell.so`). i5-4570 (Haswell, AVX2/FMA) получит haswell-вариант. AVX512 не требуется.
- Образ: `ENV LLAMA_ARG_HOST=0.0.0.0`, `LLAMA_ARG_PORT=8080`, `HEALTHCHECK CMD curl -f http://localhost:8080/health`, `ENTRYPOINT ["/app/llama-server"]`.
- Для GPU-ноды: `server-cuda12-b12033` (2.6 GB сжато) или `server-cuda13-b12033` (CUDA 13.4.1, нужен новый драйвер).

### 2.2 GGUF (все репозитории `gated: false`)

| repo | файл | размер |
|---|---|---|
| `unsloth/gemma-4-E4B-it-GGUF` | `gemma-4-E4B-it-Q4_K_M.gguf` | **4.98 GB** |
| `unsloth/gemma-4-E4B-it-GGUF` | `gemma-4-E4B-it-Q3_K_M.gguf` | **4.06 GB** |
| `unsloth/gemma-4-E4B-it-GGUF` | `gemma-4-E4B-it-UD-Q4_K_XL.gguf` | 5.13 GB |
| `bartowski/google_gemma-4-E4B-it-GGUF` | `google_gemma-4-E4B-it-Q4_K_M.gguf` | 5.41 GB |
| `bartowski/google_gemma-4-E4B-it-GGUF` | `google_gemma-4-E4B-it-Q3_K_M.gguf` | 4.90 GB |
| `google/gemma-4-E4B-it-qat-q4_0-gguf` | `gemma-4-E4B_q4_0-it.gguf` | 5.15 GB (+ `gemma-4-E4B-it-mmproj.gguf` 0.99 GB) |
| `ggml-org/gemma-4-E4B-it-GGUF` | `gemma-4-E4B-it-Q4_0.gguf` / `Q8_0` / `BF16` | 4.59 / 8.03 / 15.05 GB (Q4_K_M и Q3_K_M в этом репо **нет**) |

Для Q4_K_M и Q3_K_M "официального" источника Google нет; `unsloth/...` содержит оба с точными именами выше. QAT Q4_0 от Google по заявлению авторов сохраняет качество, близкое к BF16 (из model card), и размером сопоставим с Q4_K_M. Предлагаю: основной выбор `google/gemma-4-E4B-it-qat-q4_0-gguf` (качество), запасной `unsloth/...:Q4_K_M`, при нехватке RAM `Q3_K_M`. Сравнения качества и скорости Q4_0 vs Q4_K_M на этом CPU не делал (**UNVERIFIED**).

### 2.3 Рекомендуемые флаги

```
llama-server \
  -hf unsloth/gemma-4-E4B-it-GGUF:Q4_K_M \
  --alias gemma-4-e4b \
  --host 0.0.0.0 --port 8080 \
  --jinja \
  -t 4 -tb 4 \
  -c 8192 -np 1 \
  -fa on \
  --no-mmproj \
  --cache-ram 512 \
  --metrics
```

- `-t 4`: 4 физических ядра, Hyper-Threading у i5-4570 нет. Больше потоков только замедлит.
- `-c 8192 -np 1`: при unified KV (`-kvu`, включён по умолчанию при auto-слотах) контекст делится между слотами. Gemma 4 E4B: SWA-окно 512, 18 из 42 слоёв делят KV (`num_kv_shared_layers: 18`), поэтому KV мал (порядка десятков-сотен MB на 8k, расчёт ориентировочный, **UNVERIFIED**). Можно поднять до 16384 при запасе RAM. `-np 2` допустим, но на 4 ядрах пропускная способность почти не растёт (память и вычисления делятся).
- `--no-mmproj`: по документации сервера при `-hf` mmproj скачивается автоматически; для текстового сервера это лишние ~0.5-1 GB и RAM.
- `--cache-ram 512`: значение по умолчанию **8192 MiB** (README сервера), что при 7.9 GB свободной RAM опасно для хранения prompt-кэша. Ограничить.
- `--jinja`: в текущей документации "on by default", но явный флаг безвреден; обязателен для tool calling.
- mmap включён по умолчанию (режим `auto`); `--mlock` не использовать при такой RAM (4.98 GB веса + page cache + ОС). Свопа на ноде быть не должно. Если файл не влезает в page cache, скорость упадёт катастрофически.
- Флаг `-fa on|off|auto` (default auto) принимает значения, не просто toggle.
- Мультимодальность и MTP/spec-decoding на CPU не нужны (`--spec-type draft-mtp` существует, не проверял на CPU).
- Не задавать `--reasoning`: по умолчанию `auto`, формат `--reasoning-format` auto (deepseek-подобный `reasoning_content`).
- Подробное: `--chat-template-kwargs '{"enable_thinking": false}'` или поле запроса `chat_template_kwargs`.

### 2.4 Скорость (оценка, **UNVERIFIED**)

Бенчмарков Gemma 4 E4B на i5-4570 не найдено (поиск дал только агрегаторы). Расчёт: DDR3-1600 двухканальная ~25.6 GB/s пик (~15-18 GB/s реально); на токен читается порядка 2.5-3 GB весов (4.5B "эффективных" параметров; PLE-таблица только lookup). Потолок по памяти ~5-7 tok/s; AVX2 без AVX512 на 4 ядрах даёт реалистично **3-6 tok/s генерация, 15-40 tok/s prefill**. Для проверки на месте: `llama-bench -m <gguf> -t 4`.

### 2.5 Эндпоинты

- `GET /health`: публичный (без API key), **503** "Loading model" пока грузится, **200** `{"status":"ok"}` когда готов. Подходит для readinessProbe; для startupProbe задать запас на скачивание.
- `GET /v1/models`: ровно одна запись; `id` по умолчанию путь модели, `--alias` заменяет (использовать одинаковый `--alias`/`--served-model-name` = `gemma-4-e4b` на обеих нодах).
- `POST /v1/chat/completions` со `stream: true`, tool calling только с `--jinja`.
- `--api-key`/`LLAMA_API_KEY` (comma-separated), `--metrics` для Prometheus (`/metrics`).

### 2.6 Загрузка модели: `-hf` при старте vs предзапекание

- `-hf repo:quant` качает в кэш (`LLAMA_CACHE`, по умолчанию `~/.cache/llama.cpp`); см. `-hff`/`--hf-file` для точного имени файла.
- Рекомендация: `-hf` + постоянный том (hostPath/PVC) на `LLAMA_CACHE`. Тогда первый старт качает ~5 GB, последующие нет. Предзапекать модель в образ (5 GB слой) дороже по реестру и обновлению; допустимо, если у кластера нет доступа к huggingface.co. Альтернатива: initContainer, который делает `curl -L -o` по `https://huggingface.co/<repo>/resolve/main/<file>` в общий том, а основной контейнер стартует с `-m /models/<file>` (детерминированно, без зависимости от `-hf`).
- `/health` возвращает 503 во время загрузки/скачивания, readiness корректно блокирует трафик.

## 3. Разбиение одной модели между машинами

### 3.1 vLLM multi-node на Kubernetes (LeaderWorkerSet)

Источники: docs.vllm.ai (Parallelism and scaling, deployment/frameworks/lws), lws.sigs.k8s.io, kubernetes-sigs/lws.

- LWS: последний релиз **v1.0.0 (2026-10-16)**; CRD `leaderworkerset.x-k8s.io/v1`. Ставится отдельным манифестом/Helm (команда установки не проверялась, **UNVERIFIED**).
- Форма манифеста из репозитория LWS (`docs/examples/leaderworkerset/basic/vllm.yaml`, коммит `bd0cfc7`, **без Ray**, нативный multiprocessing):
  - `kind: LeaderWorkerSet`, `spec.replicas`, `leaderWorkerTemplate.size: N` (подов в группе = узлов), `restartPolicy: RecreateGroupOnPodRestart`, `leaderTemplate` (label `role: leader`) и `workerTemplate`.
  - Лидер: `vllm serve <model> --port 8080 --tensor-parallel-size T --pipeline-parallel-size P --nnodes $(LWS_GROUP_SIZE) --node-rank $(LWS_WORKER_INDEX) --master-addr $(LWS_LEADER_ADDRESS)`; воркеры те же флаги плюс `--headless` (без `--port`).
  - `/dev/shm` через `emptyDir{medium: Memory, sizeLimit: 15Gi}`, readiness `tcpSocket` на 8080, Service `ClusterIP` с селектором `leaderworkerset.sigs.k8s.io/name: vllm` + `role: leader`.
  - Образ в примере пинится `vllm/vllm-openai:v0.28.0`; в странице docs.vllm.ai стоит `:latest`.
- **Ray-вариант** (в docs.vllm.ai): лидер `multi-node-serving.sh leader --ray_cluster_size=$(LWS_GROUP_SIZE)`, затем `vllm serve ... --distributed-executor-backend ray`; воркер `multi-node-serving.sh worker --ray_address=$(LWS_LEADER_ADDRESS)`. Ray нужно ставить отдельно (optional dependency); для Kubernetes docs советуют KubeRay (`ray-project/kuberay`). То есть "LWS + Ray" — не единственная и не основная рекомендация; основной современный рецепт — LWS + `--nnodes/--node-rank/--master-addr` без Ray.
- TP vs PP (docs.vllm.ai): "the common practice is to set the tensor parallel size to the number of GPUs in each node, and pipeline parallel size to the number of nodes". Для одной GPU на узел это `--tensor-parallel-size 1 --pipeline-parallel-size <число узлов>`. Эффективный TP требует быстрой межузловой сети (InfiniBand, `NCCL_IB_HCA=mlx5`, проверка `NCCL_DEBUG=TRACE` -> `NET/IB/GDRDMA`; `NET/Socket` означает "inefficient" TCP).
- Гетерогенное разбиение слоёв: env `VLLM_PP_LAYER_PARTITION` (есть в `vllm/envs.py` v0.32.0).
- `SupportsPP` реализован для Gemma 4 (`gemma4_mm.py`, `gemma4.py`).

Оценка для 1GbE между 4070 (расчёт мой, **UNVERIFIED**, бенчмарков нет):
- TP через TCP: 2 all-reduce на слой, у 31B 60 слоёв = 120 all-reduce/токен; по 10.7 KB (hidden 5376 в BF16) на каждый. Это не пропускная способность, а латентность: при 0.3-1 мс на NCCL-sockets all-reduce получается 40-120 мс/токен только на сеть, т.е. 8-25 tok/s в лучшем случае на одном потоке; prefill 2k токенов: ~2.6 GB трафика, ~20 с на 1GbE. **TP по 1GbE нецелесообразен.**
- PP: на границе стадий передаётся один hidden-вектор на токен (десятки KB на prefill-чанк), т.е. 1GbE достаточно. Но при одном потоке стадии работают последовательно: скорость примерно как у одного GPU, выигрыш только в том, что модель вообще помещается в память; нужна конкурентность (несколько запросов) для заполнения конвейера.
- Требование LWS: узлы должны быть k8s-узлами с одинаковым образом; для TP одинаковые GPU. Окружение Windows/WSL2 в качестве k8s-узла для NCCL между хостами (NAT WSL2) — **высокий риск, UNVERIFIED**; hostNetwork на Windows-хосте неприменим к WSL2 так же, как на Linux.
- E4B с PP: у E4B общие KV между слоями (`num_kv_shared_layers: 18`, последние слои читают KV более ранних). При разрезании по стадиям это потенциально ломается; **UNVERIFIED**. Для демонстрации разбиения лучше брать модель без KV-sharing (12B, 31B: `num_kv_shared_layers: 0`).

Какие модели оправдывают разбиение на 2-4 x 12 GB:

| модель | id (vLLM) | вес | комбинация |
|---|---|---|---|
| Gemma 4 12B | `google/gemma-4-12B-it-qat-w4a16-ct` (10.26 GB) | ~5.1 GB на GPU при PP=2 | 2 x 12 GB, PP=2: комфортно; на одной карте впритык (§1.6) |
| Gemma 4 31B | `google/gemma-4-31B-it-qat-w4a16-ct` (23.27 GB) | 23.27 / N | 31B имеет 32 attention heads, 16 KV heads: TP должен делить 16 (допустимо 1,2,4,8,16; **TP=3 невозможен**). 2 x 12 GB не хватает (11.6 GB/карту без KV). 3 карты: PP=3 (60 слоёв / 3 = 20). 4 карты: PP=4, либо TP=2 x PP=2 (требует быстрой сети внутри пар) |
| Gemma 4 26B-A4B (MoE) | `google/gemma-4-26B-A4B-it`; w4a16-ct для неё **не найден**, есть только `google/gemma-4-26B-A4B-it-qat-q4_0-gguf` (14.44 GB) | | через llama.cpp |
| GGUF 31B | `google/gemma-4-31B-it-qat-q4_0-gguf` (`gemma-4-31B_q4_0-it.gguf`, 17.65 GB) | | llama.cpp RPC: 2 x 12 GB может хватить |

Базовый 12B BF16 `google/gemma-4-12B-it`: 23.9 GB; 31B BF16: 62.5 GB.

### 3.2 llama.cpp RPC (CPU/GPU split)

Источник: `tools/rpc/README.md`, `tools/rpc/rpc-server.cpp`.

- Как работает: на удалённых хостах запускается `ggml-rpc-server` (бинарь называется `ggml-rpc-server`, не `rpc-server`), он экспонирует локальные ggml-устройства (`CUDA0`, `CPU`, ...). На главном хосте `llama-server`/`llama-cli` с флагом `--rpc host1:port,host2:port`. По умолчанию веса и KV-кэш распределяются между локальными и удалёнными устройствами пропорционально доступной памяти; переопределение: `--tensor-split`. Это послойное (pipeline-подобное) разбиение, не TP.
- Сборка: на **всех** хостах `-DGGML_RPC=ON` (например `cmake .. -DGGML_CUDA=ON -DGGML_RPC=ON`). Официальные образы без RPC (§0.5), поэтому нужен свой Dockerfile на базе `.devops/cpu.Dockerfile` / `cuda.Dockerfile` с добавленным `-DGGML_RPC=ON` и копированием `ggml-rpc-server`. Совместимость с `-DGGML_BACKEND_DL=ON` (как в официальных образах) **UNVERIFIED**.
- Флаги сервера (из исходника): `-H/--host` (default 127.0.0.1), `-p/--port` (default 50052), `-t/--threads`, `-d/--device <dev1,dev2,...>`, `-c/--cache` (локальный кэш тензоров в `$HOME/.cache/llama.cpp/rpc`, управляется `LLAMA_CACHE`). Переменные `GGML_RPC_DEBUG=0..3`, `GGML_RPC_NO_RDMA=1`. RDMA опционально (RoCEv2 Linux), по умолчанию TCP.
- Безопасность: README прямо говорит "proof-of-concept ... fragile and insecure. Never run the RPC server on an open network". Нет аутентификации; в k8s необходимо NetworkPolicy и привязка к приватному интерфейсу (`-H <ip>`).
- Жизнеспособность: можно собрать 4070 (CUDA) + CPU-ноду как один "виртуальный" набор устройств и держать 31B Q4_0 (17.65 GB). Но скорость определяется слабейшим звеном и сетью: на каждый токен передаются активации через границы, а CPU-стадия i5-4570 (3-6 tok/s на 4.5B) станет тормозом. Подходит как демонстрация "общей формы без настройки под задачу", не как производственное решение. Скорость RPC на 1GbE: **UNVERIFIED**.

### 3.3 Data-parallel (реплика на ноду) против разбиения модели

- **Реплика на ноду** выигрывает, когда модель целиком помещается в ноду (как E4B Q4 на 12 GB GPU и на 8 GB CPU): нет межузловых коммуникаций, линейное масштабирование пропускной способности по числу реплик, отказ одной ноды не роняет остальные, простая балансировка. Для этого кластера (две разнородные ноды, 1GbE) это основной режим.
- **Разбиение модели** (PP, RPC, TP) выигрывает только когда модель не помещается ни в одну ноду (31B на 12 GB-картах) или когда нужен больший контекст/KV. Цена: латентность сети на токен, связность отказов (в LWS `RecreateGroupOnPodRestart` пересоздаёт всю группу), требование одинаковых нод.
- Что даёт Kubernetes "в общей форме" без настройки под конкретную модель: планирование по ресурсам (`nvidia.com/gpu`, nodeSelector/affinity на метки нод), Deployment/StatefulSet, Service/Endpoints для балансировки между репликами, `readinessProbe`/`startupProbe` по `/health`, постоянные тома для кэша моделей, HPA (по CPU/памяти; для LLM осмысленнее по метрикам очереди через `/metrics` vLLM и `--metrics` llama.cpp через Prometheus adapter или KEDA, **UNVERIFIED**), LWS как примитив для групп подов с общим жизненным циклом и топологическим размещением, автоскейлинг групп LWS (в доках lws есть разделы про HPA и topology-aware scheduling).
- Что требует настройки под модель: значения TP/PP (делители числа голов/слоёв), `--max-model-len`, `--gpu-memory-utilization`, квантизация/формат (compressed-tensors vs GGUF), tool/reasoning-парсеры и chat template, объём KV, NCCL/сеть, разбиение слоёв (`VLLM_PP_LAYER_PARTITION`, `--tensor-split`).
- Готовые надстройки над k8s для LLM (существуют, проверены по GitHub API): `kubernetes-sigs/gateway-api-inference-extension` (релиз v1.6.2, inference-aware маршрутизация), `llm-d/llm-d`, `vllm-project/production-stack`. Применимость к смешанным vLLM+llama.cpp нодам не проверялась (**UNVERIFIED**).

## 4. Квирки chat template и tool calling

Общее (model card Gemma 4):
- Нативная роль `system`. Thinking включается токеном `<|think|>` в начале системного промпта (в HF Transformers: `enable_thinking=True`); формат вывода `<|channel>thought\n...<channel|>ответ`. У E2B/E4B при выключенном thinking пустой блок размышлений **не** генерируется (у остальных моделей генерируется пустой `<|channel>thought\n<channel|>`).
- В многоходовом диалоге мысли прошлых ходов в историю не класть (кроме ходов с tool call, где thinking сохраняется). Клиент, который складывает `reasoning_content` обратно в messages, нарушает это.
- Рекомендуемый сэмплинг: `temperature=1.0, top_p=0.95, top_k=64`. OpenAI-клиенты по умолчанию шлют `temperature=1`, но `top_k` не шлют; на vLLM берётся из `generation_config.json` (в репо есть), на llama.cpp задать `--top-k 64 --top-p 0.95 --temp 1.0`.
- Картинки перед текстом, аудио после текста.
- Structured outputs: модель не видит descriptions из JSON-схемы, семантику писать в системный промпт (recipes.vllm.ai).

vLLM:
- Нужны вместе `--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4` и `--chat-template .../tool_chat_template_gemma4.jinja`. Без reasoning-parser: служебный `<|channel>` просачивается в content (issues #57231, #57232 открыты).
- Thinking по умолчанию выключен; включать `extra_body={"chat_template_kwargs": {"enable_thinking": True}}` или серверным `--default-chat-template-kwargs '{"enable_thinking": true}'`. При thinking поле `reasoning` может быть пустым без `"skip_special_tokens": false` (HF discussion на 0.19.0).
- Открытые баги tool-парсера (на 2026-11-02): #47906 (с v0.24.0 числа/булевы в аргументах возвращаются строками: `{"n": "5", "b": "true"}` вместо `{"n": 5, "b": true}`; ломает строгие клиенты и валидацию схем), #53363 и #50477 (`tool_choice="required"`/именованный forced tool_choice не принуждает), #50889 (`finish_reason=tool_calls` при пустом `tool_calls`), #39392 (`<pad>` токены при конкурентных tool-запросах), #49412 (расхождение пробелов stream vs non-stream), #39072 (клиент PI coding agent: валидация аргументов `edit`). Закрыты исправления стриминга (#38992, #38909, #39114 в 0.19.1; линейный стриминг аргументов в v0.33.0rc3: #61253).
- Следствие: **клиентам не полагаться на `tool_choice: "required"`**, приводить типы аргументов по JSON-схеме на стороне клиента, тестировать streaming и non-streaming отдельно.

llama.cpp:
- Tool calling только с `--jinja`; для Gemma 4 используется встроенный парсер `peg-gemma4`. Закрытые баги: #21316 (лишние токены в tool call), #21375 (бесконечный цикл повторов), #25072 (500 "The model produced output that does not match the expected peg-gemma4 format"), #25986 (длинные многострочные строковые аргументы парсятся непостоянно). На b12033 они закрыты, но класс ошибки "500 вместо ответа при нестандартном формате вывода" остаётся: клиенту нужен retry.
- Reasoning: `--reasoning` (on/off/auto), `--reasoning-format` (none/deepseek/deepseek-legacy, default auto); с `none` мысли остаются в `message.content`.
- Единый `--alias gemma-4-e4b` и `--served-model-name gemma-4-e4b` на vLLM, чтобы клиент не различал бэкенды по `model`.
- Различия поведения: vLLM и llama.cpp по-разному обрабатывают `parallel_tool_calls`, `tool_choice` и усечение истории. Единый набор интеграционных тестов (одинаковые запросы к обеим нодам) обязателен.

## 5. Топ-риски

1. **E4B QAT w4a16-ct (11.02 GiB на GPU) не помещается нормально в 12 GB** без PLE-offload; нужный PR #62354 не влит (UNVERIFIED, что он вообще будет влит и что UVA работает под WSL2). Решение: кастомный образ с cherry-pick либо llama.cpp-CUDA на GPU-ноде.
2. **Открытые баги `gemma4` tool-парсера vLLM** (#47906, #53363, #39392, #50889): OpenAI-клиенты с function calling могут получать строковые аргументы, пустые `tool_calls`, `<pad>`-мусор при конкуренции.
3. **RPC в llama.cpp не входит в официальные образы** и объявлен небезопасным PoC; на разнородных нодах скорость определяется CPU-нодой и 1GbE.
4. **TP по 1GbE нецелесообразен** (латентность all-reduce); осмысленно только PP. WSL2/Docker Desktop как k8s-узел для межхостового NCCL — не проверено.
5. **Версии**: v0.33.0 вышел сегодня; драйвер R580+ нужен для дефолтного образа (иначе `-cu129`). `server-b12033` — последний образ на ghcr, GitHub-релизы b12034+ образов пока не имеют.
6. **Память CPU-ноды**: ~7.9 GB RAM, `--cache-ram` по умолчанию 8192 MiB; без ограничения и без swap возможен OOM-kill.
7. Скорость на CPU (3-6 tok/s) и на 4070 — оценки без бенчмарков.

## 6. Список UNVERIFIED (кратко)

- Работоспособность UVA/PLE-offload и pinned memory под WSL2/Docker Desktop.
- Влияние `--limit-mm-per-prompt '{"image":0,"audio":0}'` на выгрузку весов энкодеров; `--kv-cache-dtype fp8` на Ada для SWA-слоёв Gemma 4.
- `--max-model-len 8192` и остальные числа под 4070 (подобраны оценкой, не запуском).
- Точная первая версия llama.cpp (билд) с Gemma 4; совместимость `GGML_BACKEND_DL` с `GGML_RPC`.
- tok/s для CPU-ноды, 4070, PP/TP по 1GbE, RPC.
- Корректность PP для E4B из-за KV-sharing.
- Команда установки LWS v1.0.0; интеграция HPA/KEDA с метриками; применимость GAIE/llm-d к смешанным бэкендам.
- Заявленные 88plug `Gemma4-E4B-it-W8A16` ~5 GB; наличие FP8/AWQ чекпоинтов для E4B.
- Распакованный размер `vllm/vllm-openai:v0.32.0`.

## 7. Источники

- HF API: `https://huggingface.co/api/models/<id>?blobs=true` для всех перечисленных id; `https://huggingface.co/google/gemma-4-E4B-it-qat-w4a16-ct` (README, заголовок safetensors по HTTP Range).
- https://recipes.vllm.ai/Google/gemma-4-31B-it ; https://vllm-recipes-1xyudt62i-inferact-inc.vercel.app/Google/gemma-4-12B-it
- https://docs.vllm.ai/en/latest/serving/parallelism_scaling.html ; https://docs.vllm.ai/en/latest/deployment/frameworks/lws.html
- https://lws.sigs.k8s.io/docs/examples/vllm/ ; https://github.com/kubernetes-sigs/lws (manifest `docs/examples/leaderworkerset/basic/vllm.yaml`)
- https://github.com/vllm-project/vllm : releases v0.19.1, v0.20.0..v0.33.0rc3; PR #62354; issues #63732, #47600, #47906, #53363, #57231, #39392
- https://hub.docker.com/r/vllm/vllm-openai/tags ; https://ghcr.io/v2/ggml-org/llama.cpp (tags/manifests)
- https://github.com/ggml-org/llama.cpp : `.devops/cpu.Dockerfile`, `tools/server/README.md`, `tools/rpc/README.md`, `tools/rpc/rpc-server.cpp`, issues #21316, #21375, #25072, #25986
