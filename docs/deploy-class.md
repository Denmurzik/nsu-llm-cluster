# Развёртывание на машинах класса

Руководство для администратора: как подключить к главному серверу Windows-машины класса (RTX 4070 12 GB, 32 GB RAM) и
Linux-ноды. Команды `run-node.*` сверены со скриптами в `node/`; полный список параметров в `node/README.md`.
Числа и результаты прогонов взяты из `docs/verification.md`.

## Что проверено и что нет

**Прямо: GPU-путь не проверялся на реальном железе класса.** Ни k3s с GPU на Windows и WSL2, ни RTX 4070 под Windows, ни
режим `split` на GPU, ни vLLM на реальной GPU-ноде на машинах класса не запускались.

| Проверено на реальном железе | Не проверялось |
|---|---|
| GPU-образ ноды `nsu-llm-node:0.1.0-gpu` собран и запущен на сервере `newbar` (RTX 5090, Ubuntu 24.04, Docker 29) в режиме standalone: регистрация с `accel=nvidia` и `vram_mib`, ответы llama.cpp на CUDA (генерация 982 ток/с на тестовой модели 270M). Образ содержит CUDA 12.8 (sm_86, sm_89, sm_120) и `nvidia-container-toolkit` 1.20.1 | **GPU-путь на машинах класса:** k3s с GPU на Windows и WSL2, RTX 4070 под Windows (лестница проверки ниже не проводилась) |
| Главный сервер на Docker Desktop (Windows, WSL2 mirrored, ноутбук разработки) с настоящим NSU ID: вход, ключи, шлюз, развёртывания | `run-node.ps1 -Wsl` и `install-node.ps1` (Docker CE внутри WSL2 на Windows) на реальной машине: скрипты проверены тестами с подменой команд, на железе не запускались |
| CPU-нода `122b` (Ubuntu, Docker, i5-4570, 4 ядра) в режиме k3s по LAN: присоединение, Gemma 4 E4B (8192 × 2 и 16384 × 1), смена контекста, отказ ноды под нагрузкой, повторный запуск и `--uninstall` | CPU-нода `122b` в режиме standalone: нода запускалась в этом режиме на ноутбуке, а с `122b` проверены только регистрация и удаление пробной ноды по HTTPS (порт 443 главного был закрыт правилом брандмауэра, потом открыт) |
| Нода на Windows с Docker Desktop (ноутбук, k3s и standalone, CPU) | Режим `split` на Gemma 4 E4B: на ноутбуке в WSL оставалось 5,4 ГБ памяти |
| Режим `split` на CPU (тестовая модель 270M, `122b` и ноутбук по Wi-Fi), возврат в `replicas` | vLLM и LWS: объекты проходят `kubectl apply --dry-run=server`, запуска не было |

Всё, что ниже сказано о k3s с GPU под Windows и WSL2, взято из исследования (`docs/research/k3s-in-docker.md`) и должно быть
проверено на одной машине до раскатки на класс. Лестница проверки приведена ниже. Если k3s с GPU не заработает, запасной путь:
режим `standalone` (см. ниже), он проверен на GPU на `newbar`.

Драйвер NVIDIA на машинах класса: **не ниже 570** (образ собран на CUDA 12.8, для sm_120 нужен именно такой драйвер).

## Порты

| Откуда → куда | Порт | Зачем |
|---|---|---|
| нода → главный | TCP 6443 | присоединение k3s и обратный туннель |
| нода → главный | TCP 443 | регистрация standalone, `/ca.crt`, удаление ноды |
| главный → нода | TCP 18080 | запросы шлюза к `llama-server`; требуют общий ключ бэкендов `BACKEND_API_KEY` (кроме `/health`) |
| нода ↔ нода (режим split) | TCP 18050 | llama.cpp RPC; без аутентификации и шифрования, только внутренняя сеть класса |
| главный → `huggingface.co` | TCP 443 | зеркало HF на главном скачивает модели один раз (за прокси: `HTTPS_PROXY`); ноды берут модели с главного по 443 (`https://<MAIN_ADVERTISE_IP>/hf/`, DNS не нужен) и в интернет не ходят |
| пользователь → главный | TCP 443, 80 | UI и `/v1`; порт 80 отдаёт `/ca.crt` |

**Порт 18050 не защищён.** RPC-сервер llama.cpp не проверяет, кто к нему подключается: любой, кто достанет до порта, может
передавать тензоры и нагружать ноду. Он нужен только режиму split на нодах k3s: `run-node.sh` публикует его в режиме
`--mode k3s` и не публикует в `--mode standalone`. Не открывайте 18050 за пределы сети класса и не пробрасывайте его наружу.
Порт 18080 защищён общим ключом `BACKEND_API_KEY`: главный сервер создаёт его при первом запуске (`/var/lib/llm/secrets.env`)
и передаёт подам и standalone-нодам сам; задавать его вручную не нужно. Проверка `/health` ключа не требует.

**Токен подключения нод = токен агента k3s, а не серверный токен.** Главный хранит два токена в `/var/lib/llm/secrets.env`:
`CLUSTER_TOKEN` (серверный токен k3s: шифрует данные bootstrap кластера, даёт полный доступ к серверу k3s; на свежей установке главный не покидает)
и `AGENT_TOKEN` (k3s `--agent-token`). Ноды получают только токен агента: UI («Ноды»), `GET /api/admin/join` и
`run-main --print-token` выдают его в виде `K10<хэш CA>::node:<токен>` (k3s пишет его в `server/agent-token`). С ним нода
присоединяется к k3s и регистрируется в портале, но данные bootstrap сервера (`/v1-k3s/server-bootstrap`: CA-ключи и секреты
кластера) k3s по нему не отдаёт (проверено на k3s v1.36.5: 403). Храните его как пароль: он позволяет присоединить к кластеру
свою машину. На ноде токен не попадает в командные строки процессов (k3s агент получает его через `K3S_TOKEN`, curl — через
файл заголовков), но лежит в окружении контейнера `llm-node` (`docker inspect`).

**Остаточный риск при обновлении со старой установки.** Серверный токен не покидает главный только на свежей установке. Если
главный обновлён с версии до отдельного токена агента, то ноды, присоединённые тогда, получили серверный токен в окружение
контейнера `llm-node`, и он остаётся там (`docker inspect`). Любой, у кого есть доступ к Docker на такой ноде, может получить
`/v1-k3s/server-bootstrap` (CA-ключи и секреты кластера). `NODE_LEGACY_TOKEN=false` этого не исправляет: он лишь закрывает старый
токен в портале и убирает `CLUSTER_TOKEN` из окружения портала. Исправление: переустановить главный (`--uninstall`, затем заново
присоединить все ноды токеном агента) или поставить кластер с нуля. Свежая установка этой проблемы не имеет.

Главный и класс находятся в разных маршрутизируемых подсетях. Инференс идёт напрямую на адрес ноды и порт 18080, оверлейная сеть
между машинами не нужна.

## Сеть и DNS

Раздел описывает, какие порты реально нужны между главным и классом, что сделать с DNS и сертификатом и как проверить связность
скриптами `main/diag.sh` и `node/diag.ps1`. Таблица в разделе «Порты» выше — краткая выжимка.

### Матрица портов

Источники: документация k3s (<https://docs.k3s.io/installation/requirements>, таблица «Inbound Rules for K3s Nodes» и абзацы про
reverse tunneling и metrics-server; исследование `docs/research/k3s-in-docker.md`, разделы 1.3 и 1.4) и наша реальная конфигурация:
`main/entrypoint.sh` (k3s server: flannel по умолчанию, то есть VXLAN; `--egress-selector-mode cluster`;
`--disable traefik,servicelb,metrics-server`; `--tls-san` для `MAIN_ADVERTISE_IP` и `PUBLIC_HOSTNAME`),
`portal/app/workloads/builders.py` (все поды инференса и RPC на нодах с `hostNetwork: true`, `dnsPolicy: Default` и допуском taint
`llm.nsu/class`), `node/rootfs/opt/llm/bin/entrypoint.sh` (агент `--server https://<главный>:6443`, `--node-external-ip`,
`--node-taint llm.nsu/class=true:NoSchedule`), `node/run-node.sh`, `node/run-node.ps1`.

Почему так мало портов. Шлюз ходит к llama.cpp напрямую на `<адрес ноды>:18080` (адрес берётся из `ExternalIP` узла), поды на нодах
сидят в сети хоста и не общаются друг с другом через оверлей, а `kubectl logs/exec` идёт через туннель, который нода сама открывает
к 6443 («K3s uses reverse tunneling such that the nodes make outbound connections to the server and all kubelet traffic runs through
that tunnel», docs.k3s.io).

| Откуда → куда | Порт | Статус | Основание |
|---|---|---|---|
| нода → главный | 6443/tcp | **нужен** | присоединение k3s и туннель (k3s: Agents → Servers, «supervisor and Kubernetes API Server»); адрес зашит в `--server https://$MAIN_HOST:6443` |
| нода → главный | 443/tcp | **нужен** для standalone, скачивания `ca.crt`, регистрации и `--uninstall`; в режиме k3s после присоединения не нужен | реестр нод `POST /api/nodes/register`, `GET /ca.crt`, `DELETE /api/nodes/<имя>` |
| пользователи → главный | 443/tcp | **нужен** | портал и `/v1`, вход через NSU ID |
| пользователи, ПК класса → главный | 80/tcp | условно: только с внутренним CA | `http://<главный>/ca.crt` для установки сертификата; остальные пути редиректят на https |
| главный → нода | 18080/tcp | **нужен** | запросы шлюза LiteLLM к `llama-server` (`api_base http://<ip>:18080/v1`), поды `hostNetwork` с `hostPort` |
| нода → нода | 18050/tcp | условно: только режим split | RPC llama.cpp: лидер группы подключается к rpc-серверам других нод. Главный в этом трафике не участвует. Без аутентификации: открывать только для адресов класса |
| главный → нода | 10250/tcp | **не нужен** | kubelet API нужен только metrics-server (`kubectl top`): «If you wish to utilize the metrics server, all nodes must be accessible to each other on port 10250». metrics-server выключен (`--disable metrics-server`), портал метрики k3s не читает; `logs` и `exec` идут через туннель (`--egress-selector-mode cluster`). На адресе ПК kubelet вообще слушается только у ноды с `--network host` |
| нода ↔ нода, нода ↔ главный | 8472/udp (flannel VXLAN) | **не нужен** | оверлей между машинами не используется: системные поды k3s закреплены на главном (taint `llm.nsu/class` на ПК класса, см. «Системные поды на главном»), поэтому CoreDNS и тома не уезжают в класс; инференс и RPC идут по адресам хостов, `dnsPolicy: Default` не обращается к CoreDNS. Flannel оставлен по умолчанию только ради состояния `Ready` (`docs/research/k3s-in-docker.md`, раздел 1.4). Не публиковать |
| нода → DNS кампуса | 53/udp, 53/tcp | **нужен** | поды инференса используют резолвер ноды (`dnsPolicy: Default`): имя главного и `huggingface.co` должны разрешаться на ноде |
| главный → `huggingface.co` | 443/tcp | **нужен**: напрямую, через прокси (`HTTPS_PROXY`) или файлы руками в `offline/` | зеркало HF на главном скачивает GGUF один раз (`docs/hf-mirror.md`); ноды ходят только на главный по 443. Прямой выход нод нужен лишь при `HF_MIRROR_ENABLED=false` |
| сервер ↔ сервер | 2379–2380/tcp | не нужен | только HA с embedded etcd; сервер один |
| все ↔ все | 51820, 51821/udp | не нужен | только flannel wireguard-native; бэкенд VXLAN |
| все ↔ все | 5001/tcp, 6443/tcp | не нужен | только встроенный реестр Spegel; он не включён |

Что из этого проверяют скрипты: `main/diag.sh` и `node/diag.ps1` проверяют 6443, 443, 18080, DNS, `ca.crt`, регистрацию и правила
брандмауэра; `main/diag.sh` ещё проверяет, что все системные поды kube-system работают на `llm-main`; 10250 не проверяется (см.
таблицу); 18050 проверяется как правило брандмауэра на ноде (порт слушается только пока работает split-развёртывание).

**Главному не нужно ничего сверх 80/443/6443.** `main/install-main.sh` (задача 2-12) открывает ровно эти три порта tcp, и этого
достаточно: 18080 и 18050 открываются на стороне нод, а 8472/udp и 10250 не нужны вовсе. На ноде `run-node.ps1` (запущенный от
администратора) создаёт правило Windows Firewall `NSU-LLM-Node`: tcp 18080, в режиме k3s ещё 18050. Правило Hyper-V firewall для ноды
в WSL с mirrored-сетью он не создаёт: его добавляют вручную, команду с подставленными значениями печатает `node/diag.ps1` (она же ниже). От сети главному требуется достижимость нод по
18080: маршрутизатор между подсетями должен пропускать tcp 18080 в сторону класса. Порты, опубликованные Docker (`-p`), обходят ufw и
firewalld через собственные цепочки iptables, поэтому отсутствие правила в ufw не доказывает блокировки: решает проверка
`node/diag.ps1` с ПК класса.

Правила на стороне нод (PowerShell от администратора; `node/diag.ps1` печатает те же команды с подставленными адресами):

```powershell
New-NetFirewallRule -DisplayName NSU-LLM-Node -Direction Inbound -Protocol TCP -LocalPort 18080,18050 -RemoteAddress <подсеть класса>,<адрес главного> -Action Allow
# WSL с mirrored-сетью: дополнительно правило Hyper-V firewall
New-NetFirewallHyperVRule -Name llm-inference -DisplayName 'LLM inference' -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 18080,18050
```

### Системные поды на главном

ПК класса присоединяются к кластеру с taint `llm.nsu/class=true:NoSchedule` (`k3s agent --node-taint`, `node/rootfs/opt/llm/bin/entrypoint.sh`).
Системные поды k3s v1.36.5 его не терпят: в манифестах `coredns.yaml`, `local-storage.yaml` и `metrics-server-deployment.yaml` тега
`v1.36.5+k3s1` (<https://github.com/k3s-io/k3s/tree/v1.36.5%2Bk3s1/manifests>) допуски только `CriticalAddonsOnly` и
`node-role.kubernetes.io/control-plane:NoSchedule`, а допуска с пустым ключом (`operator: Exists` без `key`) нет. Поэтому CoreDNS и
local-path-provisioner остаются на `llm-main`; вспомогательные поды local-path назначаются через `nodeName` на ноду тома, а тома
(Postgres) живут на главном. metrics-server выключен. Поды инференса, RPC, лидеры split, vLLM и `nvidia-device-plugin` несут допуск
`llm.nsu/class=true:NoSchedule` и встают на ПК класса как раньше.

`--node-taint` действует только при регистрации узла: «The two options only add labels and/or taints at registration time, so they can
only be added once and not changed after that again by running K3s commands» (<https://docs.k3s.io/cli/agent>, «Node Labels and Taints
for Agents»). Ноды, присоединившиеся до этой версии, получают taint от контроллера портала: каждый проход он добавляет его узлам кроме
`llm-main`, у которых его нет (патч с `resourceVersion`, прочие taint сохраняются). Уже работающий системный под taint `NoSchedule` не
выселяет: `main/diag.sh` покажет такой под провалом с командой перезапуска (`kubectl -n kube-system delete pod <имя>`), после неё он
встанет на главный.

Главный и класс в разных подсетях: попросите администратора сети кампуса разрешить между ними tcp 6443 и 443 (класс → главный) и
tcp 18080 (главный → класс), а внутри класса tcp 18050.

### DNS

Нужна A-запись в DNS кампуса: `PUBLIC_HOSTNAME` (например `double.fresh.nsuts.ru`) → `MAIN_ADVERTISE_IP`.
Исключение: `double.fresh.nsuts.ru` указывает на сервер tsga, и тот проксирует домен на главный, а ноды ходят на главный по IP
(раздел «Домен double.fresh.nsuts.ru через сервер tsga» ниже).

- Имя нужно в трёх местах: пользователи и ПК класса обращаются к порталу по нему; оно в сертификате и в redirect URI NSU ID
  (`https://<PUBLIC_HOSTNAME>/auth/callback`); оно в `--tls-san` сервера k3s, поэтому ноды присоединяются к
  `https://<PUBLIC_HOSTNAME>:6443`.
- Нода должна разрешать имя изнутри своей среды, а не только в Windows. Запись в `C:\Windows\System32\drivers\etc\hosts` WSL и Docker
  Desktop не обязаны видеть. Проверка для поддерживаемого пути (Docker CE в WSL2): `wsl -d <дистрибутив> -- getent hosts <PUBLIC_HOSTNAME>`
  и `docker exec llm-node getent hosts <PUBLIC_HOSTNAME>`. Запись в DNS кампуса видна везде без дополнительных действий.
- Запасной вариант без DNS: передавать ноде адрес главного вместо имени. Сертификат НГУ тогда не подойдёт (он выписан на имя), а внутренний
  CA покрывает и адрес (`MAIN_ADVERTISE_IP` входит в сертификат Caddy).
- Если главный стоит за NAT или балансировщиком, A-запись может указывать на другой адрес, чем `MAIN_ADVERTISE_IP`: `main/diag.sh`
  покажет это предупреждением, а не провалом.

### Сертификат

Два варианта, выбираются при запуске главного (`main/run-main.sh`, `main/install-main.sh`).

**1. Сертификат НГУ (`--certs DIR`, рекомендуется).** Каталог содержит `tls.crt` и `tls.key`. В `tls.crt` кладите полную цепочку:
сертификат сайта, затем промежуточные; без промежуточных Windows и браузеры сертификат не примут. ПК класса уже доверяют центру
сертификации НГУ, раздавать ничего не нужно. Особенности:

- `GET /ca.crt` отвечает 404: это нормальный признак режима; `registrar.sh` и `node/diag.ps1` в этом случае проверяют TLS по системному
  хранилищу (если в каталоге лежит необязательный `ca.crt`, он отдаётся вместо 404);
- сертификат выписан на имя, поэтому и пользователи, и ноды ходят по `PUBLIC_HOSTNAME`, а не по адресу;
- порт 6443 этот сертификат не использует: его обслуживает сам k3s по собственному CA, ноды проверяют его по токену кластера;
- обновление сертификата: заменить файлы в каталоге и перезапустить главный (`run-main.sh` снова с `--certs`; данные сохраняются).

**2. Внутренний CA Caddy (по умолчанию, без `--certs`).** Главный сам выпускает сертификат на `PUBLIC_HOSTNAME` и `MAIN_ADVERTISE_IP`.
Корневой сертификат отдаётся по `http://<главный>/ca.crt` и по https. Чтобы ПК класса и браузеры ему верили, его нужно установить в
доверенные корневые центры сертификации.

- На одном ПК (PowerShell от администратора):

  ```powershell
  curl.exe -o ca.crt http://<главный>/ca.crt
  certutil -addstore -f Root ca.crt
  ```

- На всех ПК класса через GPO: консоль управления групповыми политиками, политика для подразделения с ПК класса, затем Конфигурация
  компьютера, Политики, Конфигурация Windows, Параметры безопасности, Политики открытого ключа, Доверенные корневые центры
  сертификации, Импорт, выбрать `ca.crt`. На ПК выполнить `gpupdate /force` или перезагрузить. Проверка: `certutil -store Root` показывает
  сертификат, а `node/diag.ps1` печатает «сертификат главного принят системой этого ПК».
- Перед импортом сверьте отпечаток: `node/diag.ps1` печатает SHA256 скачанного `ca.crt`, на главном его же даёт
  `openssl x509 -in ca.crt -noout -fingerprint -sha256`. Так вы не доверите подменённый файл.
- Внутри WSL и контейнера ноды корень вручную ставить не нужно: нода скачивает `ca.crt` сама при первом запуске (trust on first use) и
  дальше проверяет по нему.

Пересоздание тома `llm-main-data` выпускает новый CA: его придётся раздать заново.

### Синхронизация времени

Часы ПК класса и главного должны идти синхронно, расхождение не больше минуты. На Windows нужна служба «Служба времени
Windows» (`w32time`, `w32tm /query /status`), WSL2 берёт время у Windows. На Linux-главном нужен chrony (`chronyc
tracking`) или systemd-timesyncd. Контроллер split-групп сравнивает время старта контейнеров (часы ноды) со временем,
когда главный заметил потерю ноды. Старт в пределах 120 с от этого момента считается перезапуском: при сомнении группа
перезапускается, а не остаётся висеть. При большем расхождении контроллер может не перезапустить rpc-серверы после
перезагрузки ПК лидера, и группа будет ждать до следующей потери.

### Диагностика

Оба скрипта ничего не меняют, печатают список проверок на русском и для каждого провала готовую команду исправления. Недостающее (имя и
адрес главного) они спрашивают и записывают в файл конфигурации, повторный запуск идёт без вопросов. Коды выхода: 0 — провалов нет,
1 — есть провалы, 2 — ошибка параметров.

```bash
# на главном сервере (Linux): 443 и 6443 слушаются, портал отвечает, DNS, ноды по 18080, системные поды на llm-main, ufw и firewalld
bash main/diag.sh
bash main/diag.sh --yes --hostname double.fresh.nsuts.ru --node pc01=10.0.1.11 --node pc02=10.0.1.12
```

```powershell
# на ПК класса (PowerShell от администратора, чтобы были видны все правила брандмауэра)
.\node\diag.ps1
.\node\diag.ps1 -NonInteractive -Main double.fresh.nsuts.ru
```

`node/diag.ps1` проверяет DNS, 443 и 6443 главного, скачивание `ca.crt` и доверие к сертификату, эндпоинт регистрации нод, входящие
правила Windows Firewall для 18080 и 18050 (действующий набор вместе с групповой политикой; с проверкой, что в правиле есть адрес
главного). Если правила для порта нет, но его могут пропускать другие (правило программы на любые порты, правило для порта в другом
профиле сети), это предупреждение, а не провал: точный ответ даёт `main/diag.sh` с главного. Проверки WSL выбираются по тому, где
работает контейнер `llm-node`: для ноды в WSL (основной путь) это версия WSL, режим mirrored в `.wslconfig` и в работающем WSL, правило
Hyper-V firewall, а также имя главного и 6443 изнутри WSL (там работает k3s-агент); для ноды в Docker Desktop (запасной путь) NAT-режим
WSL нормален и отмечается информационной строкой. Если контейнер не найден, проверяется основной путь, но его провалы только
предупреждения. Проверка WSL запускает команды в WSL: остановленный WSL при этом стартует. `main/diag.sh` берёт
ноды из `kubectl get nodes` внутри `llm-main`; ноды в режиме standalone в k3s не видны, их передают через `--node ИМЯ=АДРЕС`. Если
`diag.ps1` на ПК проходит, а `diag.sh` на главном показывает закрытый 18080, не хватает правила на самой ноде или маршрута между
подсетями: сообщение содержит готовую команду.

## Домен double.fresh.nsuts.ru через сервер tsga

В NSU ID (Keycloak `sso-ng.nsu.ru`, realm NSU) есть клиент `double.fresh.nsuts.ru` с redirect URI
`https://double.fresh.nsuts.ru/*`, а портал строит адрес возврата как `https://<PUBLIC_HOSTNAME>/auth/callback`. A-запись
`double.fresh.nsuts.ru` указывает на 10.4.0.105, сервер tsga в сети НГУ, и перенести её нельзя. Доступ только из сети НГУ,
частный адрес в DNS так и задуман. Поэтому домен остаётся на tsga, а nginx на tsga проксирует его целиком на главный.
LMS MTSGA, которая сейчас занимает домен на tsga, останавливается.

```
пользователь (сеть НГУ) ──https──> tsga 10.4.0.105, nginx, сертификат tsga
                                     └──https на <IP главного>, SNI double.fresh.nsuts.ru, проверка по CA главного──> Caddy главного
ноды класса ──6443 (k3s), 443 (/hf, /ca.crt, регистрация)──> главный по IP, мимо tsga
```

Файлы на tsga: `deploy/front-tsga/switch-front.sh` (переключение и откат) и `deploy/front-tsga/site.conf.tmpl` (шаблон сайта
nginx). Тесты: `bash deploy/front-tsga/tests/test_switch_front.sh` (заглушки docker, nginx, systemctl, curl, на tsga ничего не делают)
и `IMAGE=nsu-llm-main:0.1.0 bash deploy/front-tsga/tests/test_front_e2e.sh` на сервере сборки: настоящий nginx 1.18 (как на tsga)
перед образом главного, проверка CA и SNI, `X-Forwarded-For`, SSE без буферизации, загрузка 50 МБ, 502 при чужом CA.

### Настройки главного

В `.env` главного (или в ответах `install-main.sh`, вопрос про фронт-прокси):

```
PUBLIC_HOSTNAME=double.fresh.nsuts.ru
TRUSTED_PROXIES=10.4.0.105
```

- `TRUSTED_PROXIES`: адреса и подсети через запятую. Главный пишет их в глобальные опции Caddy
  (`servers { trusted_proxies static ... }`) и принимает `X-Forwarded-For` и `X-Forwarded-Proto` только от них: настоящий адрес
  пользователя видит Caddy (журнал, `client_ip`) и зеркало HF (читает `X-Forwarded-For` слева). Портал адреса пользователя не видит:
  uvicorn доверяет только 127.0.0.1 и идёт по `X-Forwarded-For` справа налево, поэтому в `request.client.host` у него адрес tsga
  (10.4.0.105); портал использует адрес лишь в предупреждении о старом токене ноды, а ноды ходят к главному напрямую. nginx на tsga записывает в `X-Forwarded-For` адрес клиента (`$remote_addr`), а не дописывает его к
  присланному заголовку, поэтому подделать адрес через tsga нельзя. Без `TRUSTED_PROXIES` Caddy заменяет эти заголовки адресом
  соединения. Значение проверяется при старте: допустимы только цифры, точки, двоеточия, `/` и шестнадцатеричные цифры IPv6.
- Ноды подключаются к главному по IP, а не по имени: имя ведёт на tsga, а tsga проксирует только 443. k3s (6443) на tsga нет, а
  модели с `/hf` (десятки гигабайт) незачем гнать через лишний сервер. Команда подключения в портале («Ноды») и в выводе
  `install-main.sh` уже содержит `MAIN_ADVERTISE_IP`: `run-node.ps1 -Main <IP>`, `run-node.sh --main <IP>`, `node/diag.ps1 -Main <IP>`.
  Зеркало HF с внутренним CA по умолчанию тоже идёт по IP (`https://<MAIN_ADVERTISE_IP>/hf`), `HF_MIRROR_URL` задавать не нужно
  (он нужен только со своим сертификатом, см. `docs/hf-mirror.md`). Сертификат внутреннего CA выписан и на
  имя, и на `MAIN_ADVERTISE_IP`, ноды проверяют его по `ca.crt`.
- Сертификат главного. Внутренний CA (по умолчанию) выпускает сертификат на `double.fresh.nsuts.ru`; `switch-front.sh` скачивает
  корень с `https://<главный>/ca.crt`, и по нему nginx проверяет главный (`proxy_ssl_verify on`, глубина цепочки 2: сайт,
  промежуточный, корень). Со своим сертификатом (`CERT_MODE=own`) `tls.crt` должен покрывать `double.fresh.nsuts.ru`, а корень,
  которым он подписан, передаётся скрипту явно: `--ca FILE`.
- `main/diag.sh` предупредит, что A-запись указывает не на `MAIN_ADVERTISE_IP`: при tsga так и должно быть.

### Брандмауэр главного

- 443 и 80: tsga (10.4.0.105), подсети класса (ноды ходят на 443 за `/ca.crt`, `/hf` и регистрацией), хосты администраторов.
  Пользователи кампуса приходят через tsga, им прямой доступ к главному не нужен.
- 6443: только подсети класса.
- Порты нод (18080 и 18050) открыты только для подсетей класса и главного, как в «Матрице портов» выше.

Порты главного опубликованы Docker (`-p 80:80 -p 443:443 -p 6443:6443`), а такие порты обходят правила ufw и firewalld. Поэтому
ограничения ставятся в цепочку `DOCKER-USER` (её Docker создаёт и не очищает; `install-main.sh` с `FIREWALL=yes` открывает порты в
ufw, но доступ по адресам не ограничивает). Пример (каждая `-I` встаёт в начало цепочки, поэтому запрет вставляется первым):

```bash
EXT=eth0                  # внешний интерфейс главного: ip -4 route get 10.4.0.105
CLASS=10.0.1.0/24         # подсети класса через запятую
ADMIN=10.4.1.20           # хосты администраторов через запятую
sudo iptables -I DOCKER-USER -i $EXT -p tcp -m multiport --dports 80,443,6443 -m conntrack --ctstate NEW -j DROP
sudo iptables -I DOCKER-USER -i $EXT -p tcp -m multiport --dports 80,443 -s 10.4.0.105,$ADMIN -j RETURN
sudo iptables -I DOCKER-USER -i $EXT -p tcp -m multiport --dports 80,443,6443 -s $CLASS -j RETURN
sudo iptables -L DOCKER-USER -n -v --line-numbers       # проверка
sudo apt-get install -y iptables-persistent && sudo netfilter-persistent save   # правила после перезагрузки
```

Номера портов в контейнере совпадают с опубликованными, поэтому `--dports` верен и после DNAT. Правила привязаны к внешнему
интерфейсу (`-i $EXT`): проверка главного самого себя по LAN-адресу (`api-advertise-ip.py`, hairpin через docker0) их не задевает.
Проверка: с tsga `curl -sk https://<IP главного>/readyz` отвечает 200, с ПК класса `node/diag.ps1 -Main <IP>` проходит, с
постороннего адреса сети НГУ соединение с 443 и 6443 главного не устанавливается.

### Порядок переключения

sudo на tsga спрашивает пароль: его вводит владелец в терминале (`ssh -t`), в файлы, команды и журналы пароль не попадает.

1. Главный работает, `/readyz` по IP отвечает 200, сертификат на имя проверяется по его CA:

   ```bash
   curl -sk https://<IP главного>/readyz
   curl -s -o ca.crt http://<IP главного>/ca.crt
   curl --cacert ca.crt --resolve double.fresh.nsuts.ru:443:<IP главного> https://double.fresh.nsuts.ru/readyz
   ```

2. Скопировать скрипт на tsga и посмотреть, что он сделает (ничего не меняет):

   ```bash
   scp -r deploy/front-tsga nsu:~/front-tsga
   ssh -t nsu 'sudo ~/front-tsga/switch-front.sh --main <IP главного> --dry-run'
   ```

3. Переключить. Скрипт скачает `https://<главный>/ca.crt` и напечатает его отпечаток SHA-256. Сверьте его с главным
   (`curl -s http://127.0.0.1/ca.crt | openssl x509 -noout -fingerprint -sha256` на главном) и ответьте `y`:

   ```bash
   ssh -t nsu 'sudo ~/front-tsga/switch-front.sh --main <IP главного>'
   ```

   Скрипт сначала проверяет главный по скачанному CA (`/readyz` с SNI `double.fresh.nsuts.ru`); если главный не готов, ничего не
   меняется. Затем: резервная копия файла сайта и политик перезапуска контейнеров LMS в `/var/backups/nsu-llm-front/<время>/`,
   корень главного в `/etc/nginx/nsu-llm-main-ca.crt`, новый `/etc/nginx/sites-enabled/double.fresh.nsuts.ru` из шаблона (пути
   `ssl_certificate` и `ssl_certificate_key` берутся из прежнего файла), `nginx -t` (при ошибке прежние файлы возвращаются и
   скрипт завершается с ошибкой), `docker update --restart=no` и `docker stop` для `double.fresh.nsuts`, `double.fresh.nsuts.test`,
   `daemon_server`, `daemon_server_test`, `systemctl reload nginx` и проверка `https://double.fresh.nsuts.ru/readyz` через nginx tsga.
   Базы `deploy-db-1` и `mtsga-test-db-1`, pgadmin и `gitlab-runner` не трогаются, данные LMS сохраняются.
   Повторный запуск на уже переключённом tsga (например, главный сменил адрес: `--main <новый IP>`, или новый CA: `--ca FILE`)
   переписывает только файл сайта и CA; резервная копия остаётся прежней, с исходным сайтом LMS.

4. Проверить вход: в браузере из сети НГУ открыть `https://double.fresh.nsuts.ru/`, войти через NSU ID и вернуться в портал;
   в чате ответ модели должен идти потоком, а не появляться целиком (буферизация на tsga выключена).

5. Откат (сайт и CA из последней резервной копии, `nginx -t`, прежние политики перезапуска, запуск контейнеров LMS, которые
   работали до переключения, `reload`):

   ```bash
   ssh -t nsu 'sudo ~/front-tsga/switch-front.sh --rollback'
   ```

В каталоге `sites-enabled` на tsga лежат ещё `double.fresh.nsuts.ru.bak` и `double.fresh.nsuts.ru.pre-test-bak` с тем же
`server_name`. nginx читает и их, но для одинакового имени берёт первый по алфавиту файл (`double.fresh.nsuts.ru`), остальные
пропускает с предупреждением `conflicting server name`, которое было и до переключения. Скрипт их не трогает и пишет об этом.

**gitlab-runner.** Он остаётся работать: он может обслуживать и другие проекты. Пайплайн LMS при push в её репозиторий может
пересоздать контейнеры `double.fresh.nsuts` и остальные с `restart: always`. Домен при этом остаётся на главном (nginx на 5001 и
5002 больше не проксирует), но LMS снова займёт память и порты tsga. Договоритесь с владельцем LMS приостановить пайплайн; если
контейнеры всё же пересоздались, остановите их снова (`docker update --restart=no <имя> && docker stop <имя>`).

### Сертификат tsga для пользователей

Пользователи видят сертификат nginx tsga (`/etc/nginx/ssl/selfsigned.crt`), а не главного. Let's Encrypt здесь недоступен: адрес
10.4.0.105 частный, из интернета проверку домена не пройти. По умолчанию сертификат tsga остаётся как есть.

Вариант (не по умолчанию): заменить его сертификатом на `double.fresh.nsuts.ru`, выпущенным CA главного. Тогда пользователи ставят
один корень, `ca.crt` главного, тот же, что у нод и у `curl --cacert`. Копировать сертификат, который Caddy выпускает себе, нельзя:
внутренний CA выдаёт его на 12 часов и обновляет сам. Поэтому для tsga выпускается отдельный сертификат на 397 дней, подписанный
корнем внутреннего CA; ключ корня главный не покидает:

```bash
# на главном
docker exec llm-main sh -ec '
  P=/var/lib/llm/caddy-data/caddy/pki/authorities/local; D=/tmp/front-cert
  rm -rf $D; mkdir -m 700 $D; cd $D
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout front.key \
    -subj "/CN=double.fresh.nsuts.ru" -out front.csr
  printf "subjectAltName=DNS:double.fresh.nsuts.ru\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n" >ext
  openssl x509 -req -in front.csr -CA $P/root.crt -CAkey $P/root.key -set_serial 0x$(openssl rand -hex 16) \
    -days 397 -sha256 -extfile ext -out front.crt'
umask 077; docker cp llm-main:/tmp/front-cert/front.crt .; docker cp llm-main:/tmp/front-cert/front.key .
docker exec llm-main rm -rf /tmp/front-cert
openssl verify -CAfile ca.crt front.crt                     # front.crt: OK

# на tsga (сначала скопировать: scp front.crt front.key nsu:~/, затем удалить их с главного)
ssh -t nsu 'sudo install -m 644 ~/front.crt /etc/nginx/ssl/nsu-llm-front.crt && sudo install -m 600 ~/front.key /etc/nginx/ssl/nsu-llm-front.key && rm -f ~/front.crt ~/front.key'
ssh -t nsu "sudo sed -i 's|/etc/nginx/ssl/selfsigned.crt;|/etc/nginx/ssl/nsu-llm-front.crt;|; s|/etc/nginx/ssl/selfsigned.key;|/etc/nginx/ssl/nsu-llm-front.key;|' /etc/nginx/sites-enabled/double.fresh.nsuts.ru && sudo nginx -t && sudo systemctl reload nginx"
```

Повторный `switch-front.sh` берёт пути сертификата из текущего файла сайта и сохраняет их, откат возвращает прежний сайт с
`selfsigned.crt`. Сертификат нужно перевыпускать раз в год и после пересоздания тома `llm-main-data` (новый CA). Проверка с
устройства пользователя: `curl --cacert ca.crt https://double.fresh.nsuts.ru/readyz` без `-k`.

### Долгосрочно: свой клиент NSU ID

Схема с tsga нужна только потому, что клиент NSU ID привязан к имени `double.fresh.nsuts.ru`. Постоянное решение: попросить у
поддержки НГУ (4141@nsu.ru) отдельный клиент NSU ID для имени, которое указывает на сам главный, с redirect URI
`https://<имя>/auth/callback`. Затем на главном `PUBLIC_HOSTNAME=<имя>`, новые `OIDC_CLIENT_ID` и `OIDC_CLIENT_SECRET`, пустой
`TRUSTED_PROXIES`, а на tsga `switch-front.sh --rollback` возвращает домен LMS.

## Вход через NSU ID, когда у главного свой домен (мост через tsga)

Клиент NSU ID зарегистрирован для `https://double.fresh.nsuts.ru/*`. Если у главного свой домен (например публичный), а DNS
`double.fresh.nsuts.ru` перенести нельзя, nginx на tsga переадресует возврат из NSU ID на главный. Остальной сайт LMS не трогается.

1. В `.env` главного:
   ```
   PUBLIC_HOSTNAME=<домен главного>
   OIDC_REDIRECT_URI=https://double.fresh.nsuts.ru/llm-auth/callback
   OIDC_POST_LOGOUT_REDIRECT_URI=https://double.fresh.nsuts.ru/llm-auth/logged-out
   ```
2. На tsga в `server { listen 443 ... server_name double.fresh.nsuts.ru; }` (сначала копия файла, потом `nginx -t`,
   `systemctl reload nginx`):
   ```
   location = /llm-auth/callback   { return 302 https://<домен главного>/auth/callback$is_args$args; }
   location = /llm-auth/logged-out { return 302 https://<домен главного>/; }
   ```
   Адрес назначения жёсткий: открытого редиректа нет. Код и state проходят через tsga, но обменять код без client_secret
   и проверки PKCE на главном нельзя.
3. Браузер пользователя должен открывать и домен главного, и `double.fresh.nsuts.ru` (он указывает на 10.4.0.105: сеть НГУ
   или VPN). Нодам класса мост не нужен: они ходят на главный по IP.

Откат: удалить два `location` и перезагрузить nginx; на главном убрать две переменные.

## Главный на Docker Desktop (Windows)

Запуск: `.\main\run-main.ps1 -EnvFile .env` (см. `README.md`). Особенности:

- Docker Desktop должен иметь не менее 4 ГиБ памяти (`%UserProfile%\.wslconfig`: `[wsl2]`, `memory=8GB`, затем `wsl --shutdown`).
- **Адрес API и hairpin.** Поды главного и агенты нод ходят к API по адресу, который k3s публикует в `kubernetes` Endpoints
  (`--advertise-address`). При старте `main/api-advertise-ip.py` проверяет рукопожатием с nonce (не дольше 8 с), достанет ли контейнер
  до `MAIN_ADVERTISE_IP:6443` (hairpin). Если да, объявляется LAN-адрес. Если нет, главный печатает предупреждение «нет hairpin» и объявляет
  адрес контейнера: ноды присоединяются (агент подключается через `--server https://<IP главного>:6443`), но `kubectl logs`/`exec`
  для подов на других машинах и логи в портале не работают. **Адрес выбирается один раз при старте:** если сеть хоста настроили
  позже (например, добавили `hostAddressLoopback`), перезапустите главный (`run-main.ps1` снова, тома сохраняются).
- **Docker Desktop на ноутбуке с WSL2 в режиме mirrored** (как на проверенном ноутбуке): в `%UserProfile%\.wslconfig` нужны
  `networkingMode=mirrored` и, для hairpin, `[experimental]` `hostAddressLoopback=true`, затем `wsl --shutdown`. Без `hostAddressLoopback`
  хост и контейнеры не достают до собственного LAN-адреса.
- **Hyper-V firewall.** Docker Desktop публикует порты внутри WSL, а Hyper-V firewall по умолчанию блокирует входящие. Правило для главного
  (PowerShell от администратора; применённое на ноутбуке разработки содержало и порты ноды):
  ```powershell
  New-NetFirewallHyperVRule -Name NSU-LLM -DisplayName "NSU LLM" -Direction Inbound `
    -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 80,443,6443,18080,18050 -Action Allow
  ```
  Порт 18050 открывайте только в сети класса (раздел «Порты»).
- **Запрещающие правила Windows.** Если 80 и 6443 доступны с другой машины, а 443 даёт таймаут, проверьте входящие правила, которые
  блокируют 443 (`Get-NetFirewallRule -Direction Inbound -Action Block`): запрет сильнее разрешения, и правило попадает в брандмауэр
  Hyper-V. На проверенном ноутбуке так блокировало собственное правило владельца. Последствия: не работают standalone-ноды на других машинах,
  скачивание `/ca.crt`, `DELETE` при `--uninstall` с удалённой ноды и доступ пользователей из LAN, а ноды k3s (6443) работают.
- Брандмауэр Windows: правило `NSU-LLM-Main` (TCP 443, 80, 6443). Скрипт создаёт его, если запущен от администратора.
- Ноутбук-главный подключается к сети класса по LAN, `MAIN_ADVERTISE_IP` должен быть адресом этого интерфейса, а не виртуального
  адаптера VPN.

## Нода на Windows одной командой: `install-node.ps1`

`node\install-node.ps1` ставит ноду по основному пути (WSL2 + Docker CE + mirrored, раздел ниже) без ручных шагов и
поднимает её после перезагрузки без входа пользователя. PowerShell 5.1, от имени администратора, Windows 11 22H2 и новее.
Скрипт идемпотентен: повторный запуск доводит машину до нужного состояния и ничего не делает повторно.

**Статус проверки.** Скрипт проверен только тестами с подменой команд (`node/tests/test_install_node.sh`: `-DryRun`,
поддельные `wsl`, `powercfg`, `nvidia-smi`). На реальной машине класса и на ноутбуке с RTX 4070 он не запускался: первая
живая проверка — задача 2-10.

### Быстрый старт

Положите рядом со скриптами архив образа ноды (`nsu-llm-node-0.1.0-gpu.tar.gz` или `-cpu.tar.gz`, раздел «Распространение
образов») и запустите в PowerShell от имени администратора:

```powershell
powershell -ExecutionPolicy Bypass -File .\node\install-node.ps1              # интерактивно
powershell -ExecutionPolicy Bypass -File .\node\install-node.ps1 -DryRun      # только показать, что будет сделано
powershell -ExecutionPolicy Bypass -File C:\ProgramData\NSU-LLM\scripts\install-node.ps1 -Status   # после установки
```

Скрипт сам определяет сборку Windows, ОЗУ, число ЦП, версию WSL, дистрибутив, GPU и версию драйвера (`nvidia-smi`), видит
ли GPU WSL, LAN-адрес в сторону главного (маршрут до него), готовность главного и занятость портов 18080/18050 в Windows.
Главный проверяется запросом `GET https://<главный>/readyz` (`curl.exe -k --noproxy *`): ответ 200 с JSON портала
`{"db": …, "litellm": …}` — портал готов; тот же JSON с 503 — портал отвечает, но не готов (печатаются `db` и `litellm`);
другой ответ — по этому адресу не портал NSU LLM; нет ответа — главный недоступен. Для k3s ещё проверяется, что на 6443
отвечает TLS-сервер. Голое TCP-подключение не используется: в некоторых сетях посредник принимает соединение на любой адрес.
Ни один из этих исходов установку не останавливает, но каждый печатается с подсказкой. Затем спрашивает только недостающее, каждый раз с подставленным значением:

| Вопрос | По умолчанию |
|---|---|
| адрес главного | нет |
| токен кластера (ввод скрыт) | `CLUSTER_TOKEN` из окружения или сохранённый |
| режим | `k3s` |
| память WSL, ГБ | ОЗУ минус 4 (на ПК класса 32 ГБ: 28) |
| с каких адресов пускать на 18080/18050 | подсеть /24 этой машины и адрес главного |
| пароль учётной записи для задачи автозапуска (ввод скрыт) | нет; спрашивается, только если задачу надо создать |

Перед действиями печатается сводка (нода, главный, ускоритель, адрес, брандмауэр и нумерованный список шагов) и вопрос
«Продолжить? (д/н)». Если WSL ещё не установлен, скрипт ставит его (`wsl --install --no-distribution`), сохраняет ответы,
просит перезагрузить ПК и завершается с кодом 3010; повторный запуск после перезагрузки идёт дальше без вопросов, кроме пароля.

### Если на машине уже есть Docker Desktop

Скрипт находит Docker Desktop (дистрибутив `docker-desktop` или `C:\Program Files\Docker\Docker\Docker Desktop.exe`) и
спрашивает, где запускать ноду (по умолчанию `desktop`):

| Вариант | Что происходит | Автозапуск |
|---|---|---|
| `desktop` (запасной путь) | нода в Docker Desktop через `run-node.ps1` без `-Wsl` (bridge, порты 18080/18050 опубликованы, `NODE_IP` — LAN-адрес Windows); WSL, дистрибутивы и `.wslconfig` не меняются, задача планировщика не создаётся; образ грузится `docker load` в Docker Desktop; электропитание и оба правила брандмауэра — как в основном пути | только после входа пользователя: Docker Desktop стартует при входе (Settings, General, «Start Docker Desktop when you sign in»), контейнер поднимается по `--restart unless-stopped` |
| `wsl` (основной путь) | всё из раздела «Что делает»; Docker Desktop остаётся установленным, но запись `.wslconfig` и `wsl --shutdown` останавливают его движок; его автозапуск лучше выключить, чтобы два Docker не делили память WSL | без входа |

С `-NonInteractive` остаётся Docker Desktop, и печатается предупреждение. Выбор задаётся явно `-Docker desktop` или
`-Docker wsl` и сохраняется в файле ответов (ключ `Docker`); `-Status` и `-Uninstall` следуют ему. Если выбран `desktop`, а
движок Docker Desktop не запущен (`docker info` с ошибкой), установка останавливается с подсказкой: войти в Windows под этой
учётной записью и запустить Docker Desktop. По ssh Docker Desktop не запускается.

### Старый драйвер NVIDIA

Образ собран на CUDA 12.8, поэтому GPU-ноде нужен драйвер **не ниже 570**. Версию скрипт берёт из `nvidia-smi` на Windows.
Если драйвер старше, скрипт пишет, как его обновить: скачать драйвер 570 или новее на https://www.nvidia.com/Download/index.aspx
(GeForce, RTX 40 Series, своя модель, Windows 11) или через NVIDIA App, установить, перезагрузить ПК, проверить `nvidia-smi`.
Затем он предлагает «Продолжить как CPU-нода? (д/н)». Ответ «н» останавливает установку. С `-NonInteractive` нода ставится как
cpu с тем же предупреждением. С `-Accel nvidia` установка останавливается. После обновления драйвера повторный запуск делает
ноду GPU-нодой: метки меняются, и `run-node.ps1` попросит сначала выполнить `-Uninstall`.

### Установка по ssh

`-NonInteractive` никогда не читает ввод и не открывает окон. Если значения не хватает, скрипт завершается с ошибкой и
говорит, какой параметр передать. Так установку можно запускать по OpenSSH (сеанс администратора в Windows повышен):

```powershell
ssh admin@pc01 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\NSU-LLM\node\install-node.ps1 -NonInteractive -AnswersFile C:\NSU-LLM\class.json -Token <токен>"
ssh admin@pc01 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\NSU-LLM\scripts\install-node.ps1 -Status"
```

**Сеанс без учётных данных.** Вход по ssh-ключу (S4U) и WinRM дают сеанс без учётных данных пользователя: DPAPI
CurrentUser в нём не работает («Key not valid for use in specified state»). Работают ли в таком сеансе `wsl.exe` и
регистрация задачи, на машине класса не проверено. Поэтому скрипт не полагается на такой сеанс:

- Секреты в `install.json` шифруются DPAPI **LocalMachine** (работает в любом сеансе). Файл сразу создаётся с DACL
  «только SYSTEM и Administrators», без наследования, и ни в какой момент не доступен Users. Файлы прежнего формата
  (`TokenProtected`, DPAPI пользователя) по-прежнему читаются там, где это возможно.
- Если сеанс без учётных данных (проверка: DPAPI CurrentUser), а выбран основной путь (`wsl`), скрипт сам отвечает на
  вопросы, показывает сводку, сохраняет ответы и затем регистрирует одноразовую задачу `\NSU-LLM\NSU-LLM-Install` со
  входом по паролю (`-TaskPassword`). Задача выполняет `install-node.ps1 -NonInteractive -Yes -FromTask` уже с полными
  учётными данными: WSL, DPAPI и регистрация задачи автозапуска работают как при обычном входе. Исполнитель задачи —
  `node\class-run.ps1`, тот же, что у `class-deploy.ps1 -RunMode task`. Аргументы (адрес ноды, пароль задачи и т. д.)
  лежат в `run\run-params.bin` под DPAPI LocalMachine, файл доступен только администраторам и удаляется при чтении.
  Вывод построчно идёт в `C:\ProgramData\NSU-LLM\run\install-task.log`, код — в `run\run.done`. Родительский
  процесс показывает журнал по мере выполнения, возвращает код и удаляет задачу.
  - Задача не запустилась за 60 с: ошибка с `LastTaskResult` и подсказкой про право «Вход в качестве пакетного задания».
  - Задача завершилась без `run.done`: тоже ошибка.

  `-ViaTask` включает этот режим явно.
- Путь `desktop` выполняется прямо в сеансе ssh: CLI Docker Desktop работает без учётных данных, пароль не нужен.

`-Uninstall` в таком сеансе тоже идёт одноразовой задачей (нужен `-TaskPassword`).

### Защита каталога `C:\ProgramData\NSU-LLM`

Права `C:\ProgramData` по умолчанию позволяют любому пользователю создавать в нём каталоги и становиться их владельцем.
Ученик, заранее создавший `NSU-LLM`, мог бы подменить `node-boot.ps1` (он выполняется от администратора при каждой
загрузке) или выдать себе чтение `install.json`. Поэтому перед любой записью `install-node.ps1` вызывает `Secure-Dir`
(`node\secure-dir.ps1`, общий с `class-deploy.ps1`):

- **Владелец и права.** Владелец каталога — Administrators. DACL защищён от наследования: SYSTEM и Administrators — полный доступ,
  Users — чтение. DACL заменяется целиком, поэтому собственные записи ученика тоже уходят.
- **Недоверенное не чинится на месте, а откладывается.** Это каталог или объект внутри с чужим владельцем, чужими правами на запись
  или ссылкой. Дескриптор, открытый раньше (например, с `WRITE_DAC`), переживает смену DACL, и через него ученик снова выдал бы себе
  доступ. Поэтому такой объект переименовывается в соседний `<каталог>.untrusted-<время>` (только SYSTEM и Administrators; его можно
  удалить) вместе со всеми дескрипторами к нему. Каталог создаётся заново сразу с нужным SD
  (`[IO.Directory]::CreateDirectory(path, DirectorySecurity)`), без мгновения с унаследованными правами. Не удалось отложить
  (объект держит программа) — установка останавливается с ошибкой.
- **Ссылка (junction, symlink) на месте самого каталога** — отказ с подсказкой удалить её вручную.
- **`wsl\` не трогается.** В нём диск дистрибутива `ext4.vhdx` с правом виртуальной машины Hyper-V (`NT VIRTUAL MACHINE\<ВМ>`),
  без которого ВМ WSL может не запуститься; диск в зарегистрированном дистрибутиве нельзя и переносить.
  - **Каталог с `wsl\` не откладывается.** Допустим только каталог администратора, у которого чужие лишь права (например,
    унаследованные от `ProgramData` прошлой версией). Его DACL заменяется, а недоверенные объекты рядом с `wsl\` откладываются.
  - **Сам `wsl\` проверяется:** не ссылка; владелец SYSTEM, Administrators или администратор, который ставит ноду.
  - **Из DACL `wsl\` убираются Users, Everyone, Authenticated Users и CREATOR OWNER.** В `ext4.vhdx` лежат токен кластера и пароль
    прокси. Каталог защищается от наследования, запись ВМ и остальные записи остаются, содержимое не проверяется и не меняется.
  - **Новый `wsl\` создаётся до `wsl --install --location` сразу с DACL «только SYSTEM и Administrators»;** запись для ВМ добавляет
    WSL. `-Status` печатает права `wsl\` (владелец, защита от наследования, все записи) и сообщает о проблеме, если там есть
    Users или Everyone.
- **Если каталог не удалось привести к такому виду,** установка останавливается.

Скрипты в `scripts\` и `state.json` пишутся новыми файлами вместо старых: дескриптор, открытый раньше, не видит нового
содержимого.

Доверенные владельцы и те, кто может писать: SYSTEM, Administrators, службы (`S-1-5-80-*`) и учётная запись, от которой идёт установка, если она входит в Administrators; обычный пользователь доверенным не бывает. Пока каталог не доверенный (не администраторский владелец или чужие права на запись), `install.json` и `state.json` из
него не читаются: подложенный файл с чужим адресом главного не уведёт токен. Явно заданный `-AnswersFile` читается всегда.
Каталог, созданный прошлой версией скрипта, ещё имеет права `ProgramData` и тоже считается недоверенным. Первый запуск
новой версии предупреждает об этом, не берёт из него ответы (передайте `-Main`, `-Token` или `-AnswersFile`) и сбрасывает
права.

### Что делает

1. Ставит или обновляет WSL (не старше 2.4.4: нужны `wsl --install --name/--location/--from-file`).
2. Ставит отдельный дистрибутив `NSU-LLM` (Ubuntu 24.04) в `C:\ProgramData\NSU-LLM\wsl`: из интернета
   (`wsl --install Ubuntu-24.04 --name NSU-LLM --location … --no-launch --web-download`) или из файла
   (`-DistroSource <ubuntu-24.04.wsl>`, для машин без интернета). Дистрибутивы владельца не трогаются.
3. Вписывает свои ключи в `%UserProfile%\.wslconfig` (остальные ключи файла остаются) и выполняет `wsl --shutdown`
   (остановит все дистрибутивы этого пользователя, в том числе Docker Desktop; это сказано в сводке).
4. В дистрибутиве (`wsl-setup.sh`, от root): `systemd=true` в `/etc/wsl.conf`, Docker CE из `download.docker.com`
   (без Docker Desktop), для GPU NVIDIA Container Toolkit 1.20.1-1 и runtime `nvidia` в Docker.
5. Загружает образ ноды из архива (`docker load` внутри WSL; архив с сетевой шары сначала копируется локально).
   Повторно архив грузится, только если файл изменился.
6. Выключает сон и гибернацию (`powercfg`, от сети и от батареи; `powercfg /hibernate off`). Прежние значения сохраняются
   для `-Uninstall`. Действие при закрытии крышки ноутбука скрипт не меняет.
7. Брандмауэр: правило Hyper-V firewall для WSL (`New-NetFirewallHyperVRule … -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'`)
   и правило Windows Firewall `NSU-LLM-Node`, оба на TCP 18080 и (k3s) 18050 и только с адресов `-AllowFrom`.
8. Создаёт задачу планировщика `\NSU-LLM\NSU-LLM-Node` (раздел «Автозапуск без входа пользователя») и запускает её.
9. Wake-on-LAN (раздел «Автопробуждение ПК класса»): на физической сетевой карте с адресом ноды включает «Wake on Magic
   Packet» и право будить ПК, выключает быстрый запуск Windows, кнопка питания перестаёт выключать ПК (`-PowerButton`).
10. Запускает ноду через `run-node.ps1 -Wsl NSU-LLM` (сеть host, `-AllowFrom` и MAC, адрес и префикс карты передаются
    дальше), затем печатает `-Status`.

Ускоритель: `-Accel auto` (по умолчанию) выбирает `nvidia`, если есть `nvidia-smi`, драйвер не старше 570 и GPU виден в WSL
(`/usr/lib/wsl/lib/nvidia-smi -L`); иначе нода ставится как cpu, и скрипт пишет причину. `-Accel nvidia` в этих случаях
останавливает установку с командой исправления.

### Параметры и файл ответов

| Параметр | Значение |
|---|---|
| `-Main`, `-Token`, `-Mode k3s\|standalone`, `-Accel auto\|cpu\|nvidia`, `-Name`, `-NodeIp`, `-InsecureTls` | как у `run-node.ps1` |
| `-AllowFrom 10.1.2.0/24,10.9.0.5` | адреса и подсети IPv4 для 18080/18050 |
| `-MemoryGB 28` | `memory` в `.wslconfig` |
| `-ImageTar <путь>` | архив образа; по умолчанию ищется `nsu-llm-node-0.1.0-<gpu\|cpu>.tar*` рядом со скриптом. Если сохранённого архива уже нет (class-deploy удаляет его после установки), а образ с нужным тегом загружен, архив пропускается; без образа скрипт спрашивает путь (с `-NonInteractive` останавливается с подсказкой) |
| `-Distro NSU-LLM`, `-DistroSource <файл .wsl>` | имя дистрибутива, офлайн-источник Ubuntu |
| `-Docker desktop\|wsl` | при найденном Docker Desktop: оставить его или основной путь (по умолчанию спрашивает, с `-NonInteractive` — `desktop`) |
| `-TaskPassword` (или `NSU_LLM_TASK_PASSWORD`) | пароль учётной записи для задачи автозапуска |
| `-NonInteractive` | без вопросов: недостающее берётся из файла ответов или значений по умолчанию, иначе ошибка |
| `-Yes` | без вопроса «Продолжить?» |
| `-AnswersFile <файл>` | прочитать ответы из файла (ключи как в `install.json`) |
| `-PowerButton nothing\|keep` | `nothing` (по умолчанию, в интерактивном режиме вопрос): кнопка питания ничего не делает; `keep` — не трогать |
| `-Proxy http://host:port` | прокси для `wsl --install/--update --web-download`, apt и Docker CE в WSL, демона Docker и контейнера ноды |
| `-ViaTask` | выполнить установку одноразовой задачей со входом по паролю (сама включается в сеансе без учётных данных) |
| `-DryRun` | только напечатать команды; ничего не меняет и работает без прав администратора |
| `-Status`, `-Uninstall [-KeepDistro]` | см. ниже |

Ответы сохраняются в `C:\ProgramData\NSU-LLM\install.json` до начала действий: `Main`, `Mode`, `Accel`, `Docker`, `Name`, `Distro`,
`AllowFrom`, `MemoryGB`, `NodeIp` (только если задан явно: иначе адрес определяется заново, DHCP может его сменить),
`DistroSource` и `ImageTar` (полные пути), `InsecureTls`, `PowerButton`, `Proxy` (пароль заменён на `***`), `ProxyMachine` (прокси с
паролем под DPAPI LocalMachine) и `TokenMachine` (токен под DPAPI LocalMachine; на другую машину он с файлом не переносится). Доступ к файлу есть только у Administrators и SYSTEM, поэтому
`-Status` запускают от администратора. Архив образа без `-ImageTar` ищется только рядом со скриптом (не в родительском каталоге:
у раздачи это `C:\ProgramData`, куда может писать любой пользователь) и берётся, только если его владелец и все, кто может его
менять, — администраторы: образ запускается с токеном кластера в окружении. Пароль задачи в `install.json` не сохраняется.

**Прокси.** С `-Proxy` скрипт задаёт `HTTP_PROXY`, `HTTPS_PROXY` и `NO_PROXY` своему процессу (их видят `wsl --install
--web-download` и `wsl --update --web-download`). `wsl-setup.sh` получает `PROXY` и `NO_PROXY_LIST` через `WSLENV`, а не
в командной строке: её видят все пользователи. Пароль прокси нигде не печатается. Скрипт пишет (только для root, `%` в drop-in
удваивается для systemd)
`/etc/apt/apt.conf.d/95nsu-llm-proxy` и drop-in демона Docker `/etc/systemd/system/docker.service.d/nsu-llm-proxy.conf`
(без `-Proxy` оба файла убираются). `run-node.ps1` передаёт контейнеру ноды `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` и
те же имена в нижнем регистре. `NO_PROXY` собирается сам: `localhost`, `127.0.0.1`, `::1`, главный (имя и адрес), адрес
ноды, подсети `-AllowFrom`, сети k3s `100.64.0.0/16` и `100.65.0.0/16`, `.svc` и `.cluster.local`. Системный прокси WinHTTP
скрипт не меняет. Если `wsl --install --web-download` прокси из окружения не берёт (не проверено), задайте его вручную:
`netsh winhttp set proxy <host:port>` или установите дистрибутив из файла (`-DistroSource`). Для Docker Desktop прокси
загрузки образов задаётся в его настройках (Settings, Resources, Proxies).
Без вопросов на новой машине:

```powershell
$env:CLUSTER_TOKEN = '<токен>'; $env:NSU_LLM_TASK_PASSWORD = '<пароль>'
.\node\install-node.ps1 -NonInteractive -Main 10.0.0.5 -ImageTar \\server\share\nsu-llm-node-0.1.0-gpu.tar.gz
.\node\install-node.ps1 -NonInteractive -AnswersFile \\server\share\class.json     # {"Main":"10.0.0.5","Mode":"k3s","MemoryGB":28}
```

Коды завершения: 0 — готово, 1 — ошибка или отказ в подтверждении, 3010 — нужна перезагрузка и повторный запуск.
Каждая ошибка печатается как `ОШИБКА: …` и `Как исправить: <команда>`.

### `-Status` и `-Uninstall`

`-Status` ничего не меняет и печатает по строке на проверку (`[ok]`, `[--]`, `[!!]` с командой исправления): версия WSL,
`networkingMode` и `memory` в `.wslconfig`, GPU и драйвер, дистрибутив установлен и запущен, `docker` в WSL, контейнер
`llm-node`, образы, GPU в WSL, задача автозапуска (состояние, последний запуск и результат), правила Hyper-V firewall и
Windows Firewall с их адресами, сон от сети, готовность главного (`/readyz` и, для k3s, TLS на 6443), прокси, последние
строки журнала автозапуска. Код 0, если
проблем нет, иначе 1.

`-Uninstall` по сохранённому состоянию (`state.json`) откатывает то, что поставил скрипт: нода удаляется через
`run-node.ps1 -Uninstall -Wsl` (с токеном и из кластера), задача и оба правила брандмауэра удаляются, электропитание и
`.wslconfig` возвращаются к прежним значениям, дистрибутив `NSU-LLM` удаляется (`wsl --unregister`), только если его создал
скрипт (`-KeepDistro` оставляет его), затем удаляется `C:\ProgramData\NSU-LLM`. Сам WSL не удаляется: им могут пользоваться
другие программы. Пустые значения электропитания из `state.json` пропускаются. На пути `desktop` удаляются и образы ноды
(`docker rmi`).

`-Uninstall` останавливается, ничего не удаляя, в двух случаях:

- **Ноду ставила другая учётная запись** (`state.json` или задача автозапуска). Её `.wslconfig`, дистрибутив и задача
  принадлежат ей: запустите `-Uninstall` от её имени.
- **Скрипт создавал дистрибутив, его диск `wsl\*.vhdx` на месте, а `wsl -l` его не показывает или `wsl.exe` не отвечает.**
  Дистрибутив может оставаться зарегистрированным, и удаление диска его сломает. Проверьте `wsl -l -v`; если дистрибутива
  действительно нет, удалите каталог `wsl` вручную.

### Ключи `.wslconfig`

Сверено с документацией Microsoft «Advanced settings configuration in WSL»
(https://learn.microsoft.com/windows/wsl/wsl-config, редакция 2026-10-16):

| Ключ | Секция | Что пишем | Что говорит документация |
|---|---|---|---|
| `memory` | `[wsl2]` | `28GB` (`-MemoryGB`) | размер, по умолчанию 50 % ОЗУ Windows |
| `swap` | `[wsl2]` | `0` | «0 for no swap file» |
| `networkingMode` | `[wsl2]` | `mirrored` | значения `none`, `nat`, `bridged` (устарел с WSL 2.4.5), `mirrored`, `consomme`; только Windows 11 22H2+ |
| `dnsTunneling` | `[wsl2]` | `false` | как в ручном рецепте ниже; Windows 11 22H2+ |
| `vmIdleTimeout` | `[wsl2]` | `-1` | мс простоя ВМ до остановки, по умолчанию 60000, только Windows 11. **Значение `-1` в документации не описано** (описано только для `instanceIdleTimeout`) |
| `instanceIdleTimeout` | `[general]` | `-1` | мс простоя дистрибутива до остановки, по умолчанию 15000, «Set to -1 to disable auto shutdown» |
| `autoMemoryReclaim` | `[experimental]` | `gradual` | `disabled`, `gradual`, `dropCache` (по умолчанию) |

На таймауты простоя скрипт не полагается: задача автозапуска держит в дистрибутиве процесс `sleep infinity`, и пока он жив,
дистрибутив не простаивает. Ключи со значением `-1` нужны как страховка. Изменения `.wslconfig` применяются после остановки
ВМ («8 second rule» в той же документации), поэтому скрипт выполняет `wsl --shutdown` сразу после записи. Опции
`wsl --install` (`--name`, `--location`, `--from-file`, `--no-launch`, `--web-download`, `--no-distribution`) сверены с
`wsl --help` (WSL 3.0.4) и https://learn.microsoft.com/windows/wsl/basic-commands.

### Автозапуск без входа пользователя

Задача `\NSU-LLM\NSU-LLM-Node`: триггер «при запуске Windows», учётная запись администратора, от которой запускали установку,
с сохранённым паролем («выполнять для всех пользователей», не S4U), наивысшие права, без ограничения времени, перезапуск
каждую минуту до 999 раз, работает от батареи. Действие: `node-boot.ps1` выполняет `wsl -d NSU-LLM -u root -e systemctl start docker`,
затем `wsl -d NSU-LLM -u root -e sleep infinity`; если дистрибутив остановили, цикл через 15 с поднимает его снова.
Docker стартует через systemd, контейнер `llm-node` поднимается по своей политике `--restart unless-stopped`. Журнал:
`C:\ProgramData\NSU-LLM\boot.log` (ротация на 1 МБ).

Почему так:

- **Дистрибутивы WSL принадлежат пользователю.** Они зарегистрированы в профиле учётной записи, поэтому задача идёт от неё, а
  не от SYSTEM; запуск установки от SYSTEM скрипт отвергает.
- **Пароль, а не S4U.** Вход S4U не даёт сетевых учётных данных и не всегда загружает профиль. Задачу с сохранённым паролем
  Windows запускает при загрузке, вход пользователя не нужен.
- **ВМ WSL живёт, пока жив хотя бы один процесс в дистрибутиве** (или пока не истёк таймаут простоя), отсюда `sleep infinity`.

Ограничения (не проверены на машине класса, проверка в задаче 2-10):

- Сменили пароль учётной записи: задача перестаёт запускаться. Повторите установку с новым паролем (`-TaskPassword`).
- Политика домена «Сетевой доступ: не разрешать хранение паролей и учётных данных для сетевой проверки подлинности»
  запрещает задачи с сохранённым паролем: нужен локальный администратор вне политики или исключение.
- Учётная запись без пароля (или вход только по ПИН-коду без пароля) задачу «без входа» не создаст.
- Пользователь, вошедший под той же учётной записью, видит тот же дистрибутив: `wsl --shutdown` в его сеансе остановит ноду
  на 15 с, пока цикл не поднимет её.
- Запуск WSL из задачи без входа (сеанс 0) Microsoft не документирует. В сообществе он описан как рабочий для фоновых команд,
  но с оговорками: GUI-приложения WSLg в таком сеансе не работают (ноде они не нужны), а поведение отличается от обычного
  запуска (https://github.com/microsoft/WSL/issues/8217, https://github.com/microsoft/WSL/issues/834).
- Запасной путь, если задача без входа не заработает на машинах класса: автовход служебной учётной записи и та же задача с
  триггером «при входе».

### Брандмауэр

В mirrored-режиме входящие в WSL фильтруют и Windows Firewall, и Hyper-V firewall (ключ `firewall` в `[wsl2]`, по умолчанию
`true`: «allows the Windows Firewall rules, as well as rules specific to Hyper-V traffic, to filter WSL network traffic»),
поэтому скрипт создаёт оба правила. По умолчанию `-AllowFrom` содержит подсеть /24 LAN-адреса машины и адрес главного. Если
класс шире /24 или в другой подсети, укажите её явно (`-AllowFrom 10.1.0.0/22,10.9.0.5`). Если главный не входит в список,
скрипт предупреждает: шлюз не достанет до ноды. Другие входящие порты ноде не нужны: агент k3s сам подключается к главному.

### Автопробуждение ПК класса (Wake-on-LAN)

Если ПК класса выключили (кнопкой, из меню «Пуск», пропало питание), портал включает его по сети. Как это устроено:

1. `install-node.ps1` находит сетевую карту, у которой адрес ноды (`-NodeIp` или адрес в сторону главного). Берётся только
   физическая карта: виртуальные адаптеры WSL и Hyper-V (`vEthernet`), VPN, туннели и loopback не подходят. Если адрес
   ноды на виртуальном адаптере или ни на одной карте, скрипт объясняет это и ставит ноду без Wake-on-LAN. На Wi-Fi скрипт
   предупреждает: из выключенного состояния ПК по Wi-Fi обычно не просыпается.
2. На этой карте включаются «Wake on Magic Packet» (`Set-NetAdapterPowerManagement`), свойства драйвера
   `*WakeOnMagicPacket` и `S5WakeOnLan` (если они у драйвера есть) и «Разрешить этому устройству выводить компьютер из
   ждущего режима» (`powercfg /deviceenablewake`). Карта при этом не перезапускается (`-NoRestart`): настройки действуют
   после перезагрузки. Быстрый запуск Windows выключается (`HiberbootEnabled=0`): с ним «Завершение работы» — это
   гибернация ядра, и многие карты из него не просыпаются. Кнопка питания ничего не делает (`PBUTTONACTION 0`), пока не
   задано `-PowerButton keep`. Прежние значения сохраняются в `state.json`, `-Uninstall` их возвращает.
3. MAC, адрес и префикс подсети карты уходят ноде (`WOL_MAC`, `WOL_IP`, `WOL_PREFIX`). Нода k3s сообщает их порталу при
   каждом старте контейнера (`POST /api/nodes/wake`, повтор каждые 30 с, пока портал не ответит), standalone-нода — в
   каждом heartbeat. Остановка и удаление различаются: при штатной остановке контейнера (например, «Завершение
   работы» Windows) standalone-нода сообщает `POST /api/nodes/<имя>/stop` — портал сразу убирает её из маршрутизации,
   но запись, назначение и сетевую карту хранит; следующий heartbeat возвращает её в работу. Только
   `run-node -Uninstall` (`DELETE /api/nodes/<имя>`) стирает и карту: удалённый ПК портал не будит. Запись
   standalone-ноды без heartbeat дольше `STANDALONE_PURGE_S` (сутки) удаляется, а карта и назначение остаются, так что
   ПК, выключенный на выходные, в понедельник в рабочее окно будится.
4. Контроллер портала замечает ноду, которая перестала отвечать (NotReady в k3s, нет heartbeat у standalone), хотя
   нужна: не на обслуживании, её развёртывание включено и внутри рабочего окна (или нода одолжена другому развёртыванию).
   Ноды без назначения, на обслуживании или вне окна расписания не будятся. Через `WOL_DELAY_S` (60 с) портал шлёт magic
   packet (UDP, порты 9 и 7) на широковещательный адрес подсети ноды (directed broadcast, например `10.1.2.255` для
   `10.1.2.3/24`) и на последний адрес ноды (`WOL_UNICAST`). Дальше паузы `WOL_RETRY_S` (60 с), удваиваясь до 30 минут,
   всего `WOL_MAX_TRIES` (6) попыток. Каждая попытка пишется в журнал контроллеров (`wake-on-lan: node … attempt N of 6`).
   Счётчик хранится в базе и переживает перезапуск главного.
5. Запасной путь (`WOL_VIA_NODE`, по умолчанию `fallback`). Если первые `WOL_VIA_NODE_AFTER` (2) попытки главного ПК
   не разбудили, каждая следующая попытка идёт ещё и через живую ноду той же подсети: она шлёт пакет на
   широковещательный адрес своей сети, куда пакет главного мог не дойти. С `WOL_VIA_NODE=always` так с первой попытки,
   с `off` никогда. Нода выбирается однозначно: первая по имени среди готовых нод (k3s Ready или standalone с
   heartbeat), чей адрес (сообщённая сетевая карта, иначе адрес в кластере) лежит в подсети `/префикс` выключенного ПК;
   в журнале — `magic packet via node <имя>`, а если такой нет — `no live node in <подсеть>`.
   - Нода k3s: портал создаёт на ней короткий Job `llm-wake-*` (`nodeName` этой ноды, `hostNetwork`, образ ноды с
     `imagePullPolicy: Never`, команда `wol-send.sh <MAC> <broadcast>`, без привилегий, без токена служебной учётной
     записи и секретов, `backoffLimit 0`, не дольше 60 с, Kubernetes удаляет его через 5 минут). Выбран Job, а не
     сервис на ноде: на нодах k3s нет нашего процесса, который слушает порт, а новый порт потребовал бы своей
     аутентификации и открытия брандмауэра на каждом ПК; Job идёт через API Kubernetes, которым портал и так управляет.
   - Standalone-нода: команда ждёт в Redis (до 5 минут) и уходит в ответе на её следующий heartbeat (поле `wake`),
     `registrar.sh` запускает `wol-send.sh`.
   - `wol-send.sh` (в образе ноды) использует только perl-base, который есть в любом Ubuntu.
6. На страницах «Ноды» и «Состояние» у такой ноды написано «будим (попытка N)», после последней попытки — «не
   просыпается (попыток: 6)». Кнопка «Разбудить» шлёт пакет сразу (работает и при `WOL_ENABLED=false`) и, если
   `WOL_VIA_NODE` не `off`, ещё и через ноду той же подсети.

Что нужно заранее:
- **BIOS/UEFI:** включить Wake-on-LAN (Wake on LAN, Power On by PCI-E, Resume by PME и т. п.; название зависит от платы) и
  выключить ErP/EuP Ready и Deep Sleep: в этих режимах карта выключенного ПК обесточена.
- **Сеть кампуса:** маршрутизаторы пропускают directed broadcast в подсеть класса (`ip directed-broadcast` на интерфейсе
  подсети или аналог). По словам владельца, администраторы сети уже включают и выключают ПК в других подсетях, значит это
  работает. Если маршрутизатор пропускает пакет только на другой адрес или в подсети есть ретранслятор, задайте его:
  `WOL_BROADCAST_OVERRIDE=10.20.5.0/24=10.20.5.10` (пары подсеть=адрес через запятую, более узкая подсеть важнее).
- **Подсеть самого главного.** Чаще всего её закрывает запасной путь через живую ноду этой подсети (п. 5). Портал работает в контейнере `llm-main` (сеть Docker bridge). Пакет на широковещательный
  адрес другой подсети уходит из него как обычный маршрутизируемый unicast. Широковещательный пакет в собственную
  подсеть главного из контейнера bridge не уходит: Linux не пересылает его без `bc_forwarding`. Варианты: держать класс
  в другой подсети; запустить главный с сетью хоста; на хосте включить `sysctl net.ipv4.conf.all.bc_forwarding=1` и
  `net.ipv4.conf.<мост контейнера>.bc_forwarding=1` (Linux 4.19+, на живом главном не проверено); указать в
  `WOL_BROADCAST_OVERRIDE` ретранслятор в этой подсети. Unicast на последний адрес ноды проходит и так, но доходит до
  выключенного ПК, только пока коммутатор и маршрутизатор помнят его MAC (минуты после выключения).
- **Ноутбуки** часто не просыпаются по сети из полностью выключенного состояния (S5) и от батареи, по Wi-Fi почти никогда.
  Держите их на зарядке и на кабеле и проверяйте пробуждение заранее.
- **Образ ноды до 2-18.** Его registrar при остановке ещё отправляет DELETE и стирает карту: такую standalone-ноду
  портал будит только после сбоя (пропало питание). Обновите образ на ПК класса.

Проверка: выключите ПК («Завершение работы»). Через одну-две минуты на странице «Ноды» он станет «Не в сети», затем
«будим (попытка 1)», и ПК должен включиться. Если нет: `install-node.ps1 -Status` на этом ПК (строки «Wake-on-LAN» и
«быстрый запуск»), настройки BIOS, журнал контроллеров на главном
(`docker logs llm-main 2>&1 | grep wake-on-lan`).

## Нода на Windows: основной путь, WSL2 + Docker CE + mirrored-сеть

Ручные шаги того, что делает `install-node.ps1`. Этот путь выбран как наиболее вероятный для k3s с GPU (`docs/research/k3s-in-docker.md`, рецепт A). В режиме mirrored нода
внутри WSL2 видит LAN-адрес Windows-хоста, поэтому `--network host` даёт правильный адрес без публикации портов.
Требуется Windows 11 22H2 и новее (на Windows 10 mirrored недоступен).

1. **Драйвер.** Установите на Windows драйвер NVIDIA не ниже 570. В WSL драйвер не ставится.
2. **Конфигурация WSL.** `%UserProfile%\.wslconfig`:
   ```ini
   [wsl2]
   networkingMode=mirrored
   dnsTunneling=false
   memory=24GB
   swap=0
   [experimental]
   autoMemoryReclaim=gradual
   ```
   `memory` подберите под машину (модель и кеш страниц живут в памяти WSL). Затем `wsl --shutdown`.
3. **Брандмауэр.** Hyper-V firewall по умолчанию режет входящие в WSL (PowerShell от администратора):
   ```powershell
   New-NetFirewallHyperVRule -Name llm-inference -DisplayName "LLM inference" -Direction Inbound `
     -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 18080,18050
   ```
   Плюс обычное правило Windows Firewall на TCP 18080 и 18050 (скрипт `run-node.ps1` создаёт `NSU-LLM-Node`, если запущен от администратора).
4. **Docker CE в WSL.** Ubuntu 24.04 с `systemd=true` в `/etc/wsl.conf`, пакет `docker-ce` из официального репозитория Docker.
5. **NVIDIA Container Toolkit в WSL: версия не ниже 1.20.1** (1.19.1 даёт ошибку на WSL2):
   ```bash
   sudo apt-get install -y nvidia-container-toolkit=1.20.1-1 nvidia-container-toolkit-base=1.20.1-1 \
     libnvidia-container-tools=1.20.1-1 libnvidia-container1=1.20.1-1
   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
   ```
6. **Образ ноды** доставьте на машину (раздел «Распространение образов» ниже) и загрузите в Docker внутри WSL.
7. **Запуск ноды** (из PowerShell на Windows; команды docker идут через `wsl -d <дистрибутив> -u root -e docker …`, сеть по умолчанию host, данные внутри WSL):
   ```powershell
   .\node\run-node.ps1 -Main <IP главного> -Token <токен> -Wsl Ubuntu-24.04
   .\node\run-node.ps1 -Main <IP главного> -Token <токен> -Wsl default          # дистрибутив по умолчанию
   ```
   Скрипт проверяет `docker info`; для GPU запускает `docker run --rm --pull never --gpus all --entrypoint nvidia-smi nsu-llm-node:0.1.0-gpu`
   (локальный образ, без обращения к Docker Hub: без загруженного gpu-образа нода запускается как cpu).
   Режим standalone: добавьте `-Mode standalone`. Удаление: `.\node\run-node.ps1 -Uninstall -Main <IP главного> -Name <имя> -Token <токен> -Wsl Ubuntu-24.04`
   (`DELETE` ноды выполняется внутри WSL с сохранённым CA; правило брандмауэра `NSU-LLM-Node` удаляется).

WSL не стартует сам после перезагрузки Windows: `install-node.ps1` создаёт для этого задачу планировщика без входа
пользователя (раздел выше); при ручной установке нужна такая же задача. Диск WSL (ext4.vhdx) растёт и сам не сжимается; образ плюс модели занимают десятки ГБ.

### Лестница проверки GPU

Выполняйте по порядку и останавливайтесь на первом сбое (имя контейнера `llm-node`; режим k3s):

```bash
docker run --rm --pull never --gpus all --entrypoint nvidia-smi nsu-llm-node:0.1.0-gpu   # 1. GPU виден во внешнем контейнере (локальный образ)
docker exec llm-node sh -c 'ls -l /dev/dxg /usr/lib/wsl/lib/; nvidia-smi'      # 2. GPU виден внутри контейнера ноды (на WSL: /usr/lib/wsl/lib/nvidia-smi)
docker exec llm-node nvidia-container-cli info                                 # 3. определение GPU на WSL
docker exec llm-node k3s check-config                                          # 4. ядро
docker exec llm-node grep nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml   # 5. k3s нашёл runtime nvidia
docker exec llm-main k3s kubectl -n kube-system logs ds/nvidia-device-plugin-daemonset    # 6. device plugin (на главном; образ ноды, ничего не скачивает)
docker exec llm-main k3s kubectl get node <имя> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'   # 7. значение 1
# 8. тест-под: runtimeClassName: nvidia, образ nsu-llm-node:0.1.0-gpu (imagePullPolicy: Never), команда nvidia-smi
```

Если сбой на шагах 2-5 (вложенный nvidia-runtime не находит GPU): запасной вариант для запуска контейнера ноды вручную с
`--device /dev/dxg -v /usr/lib/wsl:/usr/lib/wsl:ro`; он не проверялся. Если k3s с GPU не работает совсем, переходите к режиму standalone.

**NVIDIA device plugin без nvcr.io.** `nvcr.io` из РФ отвечает 403, поэтому официальный образ плагина не используется: плагин v0.19.3
собирается из исходников тега (github.com, зависимости Go в `vendor/`) в стадии `device-plugin` файла `node/Dockerfile` и лежит в GPU-образе
ноды (`/opt/llm/bin/nvidia-device-plugin`). DaemonSet `nvidia-device-plugin-daemonset` запускает его из образа `nsu-llm-node:<версия>-gpu`
с `imagePullPolicy: Never`: этот образ уже лежит в каталоге образов k3s каждой GPU-ноды (`run-node` сохраняет его в `airgap/node.tar`), так что
отдельный архив не нужен. Официальный образ из зеркала реестра: `DEVICE_PLUGIN_IMAGE=<зеркало>/nvidia/k8s-device-plugin:v0.19.3` в `.env`
главного (тогда `IfNotPresent`). GPU-образ, собранный до этой версии, плагина не содержит: соберите образ ноды заново.

Лестница на машине класса не проводилась (результат: нет данных). Первым проверяйте шаги 1 и 2: если GPU не виден во внешнем контейнере,
вложенный k3s его тем более не увидит.

## Нода на Windows: запасной путь, Docker Desktop

Docker Desktop с WSL2-бэкендом, нода в bridge-сети с публикацией портов (`-p 18080:18080`, в режиме k3s ещё `-p 18050:18050`):

```powershell
.\node\run-node.ps1 -Main <IP главного> -Token <токен>                       # режим k3s, bridge
.\node\run-node.ps1 -Main <IP главного> -Token <токен> -NodeIp <LAN-адрес Windows>
```

Здесь `NODE_IP` должен быть LAN-адресом Windows-машины (скрипт берёт адрес маршрута до главного). Минусы: адрес контейнера внутри Docker Desktop
не маршрутизируется, внутренний адрес ноды в k3s будет адресом контейнера, шлюз ходит на внешний адрес (`NODE_IP`); GPU внутри вложенного
контейнера на Docker Desktop не проверялось. Режим «Enable host networking» Docker Desktop не заменяет mirrored-сеть: не использовать.

## Нода на Linux

```bash
sudo ./node/run-node.sh --main <IP главного> --token <токен>                          # k3s, bridge, адрес берётся из маршрута до главного
sudo ./node/run-node.sh --main <IP главного> --token <токен> --accel nvidia           # GPU: нужны драйвер >=570 и nvidia-container-toolkit >=1.20.1
sudo ./node/run-node.sh --main <IP главного> --token <токен> --mode standalone        # без Kubernetes
sudo ./node/run-node.sh --uninstall --main <IP главного> --name "$(hostname)" --token <токен>
```

На общем Docker-хосте (где уже есть другие контейнеры) используйте bridge (по умолчанию). `--network host` оставляет на хосте интерфейсы
`cni0`, `flannel.1` и цепочки iptables; `--uninstall --network host` убирает их временным привилегированным контейнером из образа ноды.
Ещё один отказной режим: при `--network host` на хосте с адресом IPv6 без маршрута собственное скачивание модели в llama-server
падает (`HTTPLIB failed: Could not establish connection`), а в bridge работает.

## Распространение образов (без реестра)

Основной путь теперь другой: образы выкладываются в `ghcr.io`, главный держит их копию в своём реестре (`https://<главный>/v2/`), а ПК класса
берут образ нода с главного по локальной сети: `install-node.ps1` по умолчанию, `class-deploy.ps1` без архивов, `run-node.sh --from-main`
(`docs/release.md`). Ниже запасной путь, он остаётся рабочим: архив `docker save`, `-ImageTar` / `-ImageSource tar`.

Реестр не обязателен. Соберите образы на одной машине и перенесите их архивом:

```bash
docker save --platform linux/amd64 nsu-llm-node:0.1.0-gpu | gzip > nsu-llm-node-0.1.0-gpu.tar.gz      # на машине сборки
docker save --platform linux/amd64 nsu-llm-node:0.1.0-cpu | gzip > nsu-llm-node-0.1.0-cpu.tar.gz
gzip -dc nsu-llm-node-0.1.0-gpu.tar.gz | docker load                                                  # на машине класса (в WSL: docker внутри WSL)
```

Размер GPU-образа 6,38 ГБ (до сжатия). В режиме k3s `run-node.*` сам делает `docker save` образа в `airgap/node.tar` и k3s импортирует его
при старте: поды ноды стартуют без реестра. Главный: системные образы k3s и образы Postgres, Redis и LiteLLM кладутся командой `run-main.sh --airgap`, образ главного
переносится так же через `docker save` и `docker load`.
Для 15 машин: сохраните архив на сетевой диск или раздайте `scp`, затем выполните `docker load` и `run-node.*` на каждой.

## Режим standalone как запасной

`--mode standalone` (`-Mode standalone`) не запускает k3s и не требует привилегий: нода регистрируется на главном по HTTPS, раз в 15 с
отправляет heartbeat и получает в ответе желаемую конфигурацию модели, а затем сама запускает и перезапускает `llama-server`.
В UI такая нода назначается на развёртывание так же, как k3s-нода. Режим проверен на GPU на `newbar` и на CPU на ноутбуке с Docker Desktop (нода зарегистрировалась сразу, получила развёртывание,
второй бэкенд появился в шлюзе через ~45 с после запуска); на машинах класса и в режиме standalone на `122b` он не запускался.
Для standalone нужен доступ ноды к порту 443 главного (регистрация, `/ca.crt`, удаление). Ограничение: standalone-ноды работают только с развёртываниями в режиме `replicas` на движке llama.cpp
(для `split` и vLLM портал такие ноды не использует).
Перевод ПК из standalone в k3s под тем же именем: k3s-нода важнее standalone-записи. Как только нода того же имени в кластере готова (Ready),
контроллер удаляет старую запись (в журнале портала строка `standalone record of <имя> removed`), назначение ноды сохраняется и становится
кластерным. Standalone-запись без heartbeat дольше `STANDALONE_PURGE_S` (по умолчанию 86400 с) тоже удаляется; вернувшаяся нода
регистрируется снова и сохраняет назначение.

## Обновление

После обновления на установке, где модель по умолчанию (`gemma-4-e4b`) уже была удалена и развёртываний нет, она создаётся
ещё один раз: признак «модель по умолчанию уже создавалась» появился только в этой версии, и для старой базы без развёртываний
его нельзя вывести. Удалите её повторно, если она не нужна: дальше она не вернётся.

Ключ бэкендов `BACKEND_API_KEY` создаётся при первом запуске новой версии главного сервера. Поды моделей пересоздаются
автоматически (в шаблоне пода меняется аннотация), standalone-ноды перезапускают `llama-server` при ближайшем heartbeat,
записи в шлюзе получают новый ключ без пересоздания. Смена ключа в `secrets.env` проходит так же.

**Смена ключа не бесшовная.** Пока поды пересоздаются, а standalone-ноды перезапускают `llama-server` по heartbeat, ключ у
шлюза, подов и нод на какое-то время расходится, и часть запросов получает 401. Меняйте `BACKEND_API_KEY` в тихое окно, когда
занятий нет.

**Развёртывания `split` после обновления.** Лидер каждой группы (`llm-<имя>-g<i>`) перезапускается один раз: в шаблон пода
добавлена аннотация `llm.nsu/group-members` (состав группы). Дальше состав группы не меняется, пока все её ноды живы. Если нода
группы пробыла NotReady дольше паузы пересборки, группа распадается: лидер удаляется, rpc-поды уцелевших нод группы перезапускаются,
и из свободных нод собираются новые полные группы. Пауза: `SPLIT_REGROUP_GRACE_S` (по умолчанию 600 с, у развёртывания можно задать свою); если есть запасная
нода того же ускорителя, группа пересобирается сразу. Пока группа пересобирается, её бэкенд не принимает запросы, остальные
группы продолжают отвечать.

**Ошибки шлюза в базе.** Отказы (429) и ошибки запросов больше не пишутся в `LiteLLM_SpendLogs` и не считаются запросами в
статистике портала (`disable_error_logs`): их видно только в логах LiteLLM (`docker exec llm-main k3s kubectl -n llm logs deploy/litellm`).

**Токен агента после обновления.** Установка, созданная до этой версии, при первом старте нового образа получает
`AGENT_TOKEN` (один раз, сохраняется в `secrets.env`; остальные секреты не меняются), и k3s начинает принимать его как токен
агента. Что происходит с нодами, подключёнными со старым (серверным) токеном (проверено на k3s v1.36.5 с существующими данными):
- **k3s-ноды продолжают работать**, в том числе после перезапуска: агент проходит проверку своим клиентским сертификатом.
- **Новую ноду со старым токеном k3s не примет** («not authorized»): после `--uninstall`, переустановки или на новой машине
  нужен токен агента из раздела «Ноды».
- **Standalone-ноды** обращаются к порталу со старым токеном ещё один релиз; портал пишет в лог
  `node at <адрес> uses the k3s server token`.

Замена на нодах: возьмите новую команду в разделе «Ноды» (или `run-main --print-token`) и выполните её на каждой ноде
(`run-node` пересоздаёт контейнер ноды). Когда все ноды переведены, задайте `NODE_LEGACY_TOKEN=false` в `.env` и перезапустите
главный: портал перестанет принимать серверный токен. Серверный токен `CLUSTER_TOKEN` при этом не меняется (сменить его на
существующем кластере нельзя). `AGENT_TOKEN` можно сменить в `.env`: k3s примет новое значение после перезапуска главного,
уже подключённые k3s-ноды продолжат работать, а новые и standalone-ноды должны получить новый токен.

Перед каждым стартом k3s главный удаляет `/var/lib/rancher/k3s/server/cred/passwd`. k3s восстанавливает файл из своей базы
и снова записывает в него оба токена. Без этого k3s с базой sqlite сохраняет новый токен агента только на диске, и уже
следующий перезапуск главного завершается ошибкой `passwd newer than datastore` (проверено на k3s v1.36.5).

При каждом старте k3s пишет в лог `passwd doesn't exist. continuing...` и `Updating bootstrap data on disk from datastore`. Это
ожидаемо: первая строка следует из удаления `passwd` перед стартом, вторая показывает, что k3s восстанавливает файлы из базы.
Ошибкой считается только `passwd newer than datastore`.

**Адрес API и hairpin** выбираются при старте главного (см. раздел про Docker Desktop): после изменения сети хоста перезапустите главный.

## Большая модель в split

Раздел для моделей, которые не помещаются ни на одну ноду (DeepSeek 671B и подобные), и для класса из 15 ПК с RTX 4070. Как устроено
размещение слоёв: `docs/research/big-models.md`; доставка весов: `docs/hf-mirror.md`; эксплуатация (запас, пауза, расписание, обслуживание): «Split в эксплуатации» в `README.md`.

**Настройки LLM в развёртывании.** В мастере и на странице развёртывания есть раздел «Генерация и модель» (только llama.cpp; vLLM для новых развёртываний не предлагается).
- «Рассуждение» (выключено, включено, авто) и «KV-кэш». Новые развёртывания получают `q8_0`: контекст занимает вдвое меньше памяти почти без потери качества; старые остаются на `f16`. `q4_0` вчетверо меньше, качество на длинных контекстах заметно ниже.
- «Параметры генерации по умолчанию»: температура, top-p, top-k, min-p, штраф за повторы. Это значения по умолчанию, клиент может передать свои.
- «Draft-модель»: малая модель предлагает токены, основная их проверяет. Файл выбирается как основной (Hugging Face или «Свои файлы»). Её веса и её контекст занимают память на каждой ноде, в split только на лидере. Вместе с изображениями draft работает, но после картинки подсказки draft хуже (llama.cpp b12033 предупреждает об этом в логе).
- «Изображения (mmproj)»: проектор предлагается сам, если в репозитории модели есть `mmproj*.gguf` (F16, затем BF16, затем остальные). Его размер входит в оценку памяти; в split он лежит на лидере.
- «Системный промпт» (до 8000 символов) добавляется, только если в запросе нет своего; виден пользователям с ключом через `/v1/model/info`. «Экспериментальная» развёртывание видят только администраторы. Оба поля не перезапускают поды.
- «Свои флаги llama-server»: добавляются в конец команды. Порт, ключ, пути к файлам, модель, контекст, KV-кэш, сэмплинг и устройства задаёт портал, такие флаги (и любые `*-file`, `*-path`, `*-dir`, `*-url`, `*-key*`) отклоняются; форма показывает причину сразу, нода проверяет флаги ещё раз.
- Блок «Команда llama-server» (свёрнут, с кнопкой копирования) показывает команду, которую получат поды, ключи и токены скрыты. «Создать похожее» на странице развёртывания открывает мастер с теми же настройками, без имени и нод.
- Изменение рассуждения, KV-кэша, сэмплинга, draft, проектора и своих флагов перезапускает поды по одной ноде.

**Какая память считается.**
- Видеопамять ноды (`llm.nsu/vram-mib`) минус 1,5 ГиБ запаса и compute-буфер CUDA: не меньше `GPU_COMPUTE_RESERVE_MIB` (по умолчанию 1024 МиБ), у очень широкой модели больше.
- RAM ноды. На Windows с WSL2 это память самой WSL, а не ПК: нода берёт её из `docker info` внутри WSL (метка `llm.nsu/ram-mib`). Поэтому
  `memory=` в `%UserProfile%\.wslconfig` определяет, сколько RAM нода может отдать модели: при `memory=24GB` метка покажет около 24 ГБ, даже если в ПК 32.
- Бюджет RAM ноды под модель: доля `ram_budget_pct` от этой метки, по умолчанию 70 %, допустимо от 10 до 90 %. Доля и сам режим
  («Использовать RAM нод, когда не хватает видеопамяти», `use_node_ram`) задаются в мастере развёртывания и на странице развёртывания.
  Слои в RAM считает процессор: генерация на них заметно медленнее, чем на видеокарте, и предупреждение мастера об этом стоит учитывать.
- Ёмкость группы: сумма бюджетов её устройств (видеокарта и RAM каждой ноды). Мастер показывает расчёт по выбранным нодам и отмечает развёртывание, которому не хватает памяти.

**Предел устройств.** Лидер llama.cpp собран на 64 устройства (`node/llama/patch-llama.sh`), портал допускает не больше 63 на группу. Нода с видеокартой
и RAM даёт два устройства, поэтому в группе до 31 такой ноды (до 63 нод, если каждая даёт одно устройство). Пятнадцати нод класса хватает с запасом.

**Диск лидера.** Лидер читает весь GGUF со своего диска и передаёт тензоры rpc-серверам (остальным нодам группы файл модели не нужен), а
rpc-сервер самой ноды лидера держит ещё и кэш тензоров. На ноде-лидере должно быть свободно не меньше
`размер модели + RPC_CACHE_MAX_GIB` (по умолчанию 64 ГиБ) плюс место под образ ноды (десятки ГБ). Лидер группы: нода с наименьшим именем в группе (под `llm-<имя>-g<i>`
на странице «Состояние»). На ПК класса модель и кэш лежат в диске WSL (`ext4.vhdx` на диске C: Windows), поэтому для DeepSeek 671B Q2 (около 230 ГБ)
нужен диск C: со свободными 300 ГБ и больше. На SSD в 256 или 512 ГБ отказ приходит через часы после старта: на середине скачивания или позже, когда кэш тензоров
дорастёт до лимита, а место на диске Windows уже кончилось. Назначьте на группу ноду с большим диском так, чтобы её имя шло первым, или возьмите модель меньше.

**Предупреждение про `df` в WSL.** Внутри WSL `df` показывает виртуальный диск `ext4.vhdx` с его предельным размером (по умолчанию 1 ТБ), а не свободное место
диска C:. Проверка `RPC_CACHE_MIN_FREE_PCT` (10 % свободно) поэтому в WSL не срабатывает, и от переполнения диска Windows защищает только `RPC_CACHE_MAX_GIB`.
Задайте его с учётом реально свободного места на C: (на ПК с малым диском, например, `RPC_CACHE_MAX_GIB=20`), а свободное место проверяйте в Windows
(`Get-PSDrive C`), а не командой `df`. Установщик и `node/diag.ps1` свободное место диска лидера не проверяют, а портал его не знает: проверка пока только ручная.

**Бюджет старта.** Загрузка 200 ГБ идёт часами: `startupProbe` пода даёт `max(3600, 600 + 2 × размер / MODEL_LOAD_MBPS × 1,5)` секунд (`MODEL_LOAD_MBPS` по умолчанию 40 МБ/с;
200 ГБ дают 4 ч 20 мин). Модель сначала скачивается на главный (зеркало, один раз), затем на лидера, затем проходит через лидера на rpc-серверы. Когда сразу качают
несколько подов, они делят канал главного (около 110 МБ/с на гигабите): уменьшите `MODEL_LOAD_MBPS`.

**Скорость.** Токены идут через сеть на каждом шаге, скорость определяют самая медленная нода и сеть: на 1 ГбЕ ждите единицы токенов в секунду, со слоями в RAM меньше.
Замеры: `docs/research/parallelism.md`. Модель, которая помещается на одну ноду, запускайте в `replicas`.

**GPU и CPU в одной группе.** С ускорителем «любой» (`accel=any`) группа split берёт и GPU-ноды, и CPU-ноды: rpc-сервер GPU-ноды отдаёт
видеокарту и RAM (`CUDA0,CPU`), CPU-ноды только RAM (`CPU`), слои раскладываются по устройствам в порядке `--rpc`. Группы собираются так: сначала
из нод одного ускорителя (GPU, затем CPU), затем из оставшихся вместе, GPU-ноды первыми; лидер на первой ноде группы. При ускорителе «любой»
предел устройств считается так, будто каждая нода группы с видеокартой (до 31 ноды с RAM нод).

**Сбои.** Ушедшая нода распускает только свою группу. Пока есть запасная нода того же ускорителя, что у ушедшей, группа пересобирается сразу; без неё группа ждёт
`SPLIT_REGROUP_GRACE_S` (по умолчанию 600 с), чтобы перезагрузка ПК не пересобирала модель на 15 нодах.

## Типичные проблемы

- Нода пересоздана под тем же именем и не присоединяется («Node password rejected»): выполните `--uninstall` (`-Uninstall`) с тем же
  `--name` и запустите снова. Если `--uninstall` не смог удалить узел на главном, выполните там
  `docker exec llm-main k3s kubectl delete node <имя>`. Обычный повторный запуск `run-node` этой ошибки не даёт: пароль ноды хранится на томе
  `llm-node-k3s`.
- Скорость ноды и рассуждения: потоки llama.cpp по умолчанию `nproc - 1` (одно занятое посторонним процессом ядро иначе тормозит все
  потоки: на `122b` 0,46 ток/с при `-t 4` против 5,6 ток/с при `-t 3`), рассуждения выключены (`--reasoning off`). Переопределение:
  `THREADS` и `THINKING` читает `serve-llama.sh`; для standalone `THREADS` (а также `HF_ENDPOINT`, `HF_TOKEN`) передают из своего окружения `run-node.sh` и `run-node.ps1`. Рассуждение и остальные настройки LLM задаются в развёртывании (раздел «Настройки LLM в развёртывании» ниже) и доходят и до подов k3s, и до standalone-нод.
- Входящее правило, блокирующее 443, ломает standalone-ноды и вход пользователей из LAN (см. «Главный на Docker Desktop (Windows)»).
- «Метки ноды изменились — выполните --uninstall и запустите снова»: поменялся ускоритель или объём VRAM.
- Нода не видна из главного: проверьте TCP 18080 (правило Windows Firewall и, для mirrored, Hyper-V firewall) и `NODE_IP`. Скрипты `node/diag.ps1` (на ПК класса) и `main/diag.sh` (на главном) найдут причину и напечатают команду исправления: см. «Сеть и DNS».
- Диск: образ ноды и кеш моделей (`llm-models`) занимают десятки ГБ; при нехватке места kubelet выселяет поды (DiskPressure).

## Массовая установка на 15 ПК: `class-deploy.ps1`

Скрипт `node/class-deploy.ps1` запускается с ПК администратора (Windows PowerShell 5.1) и по WinRM ставит, обновляет, удаляет ноды на ПК класса и показывает их состояние
одной таблицей. Сама установка на каждом ПК выполняется `node/install-node.ps1` (раздел «Нода на Windows одной командой»); `class-deploy.ps1` только раздаёт ему файлы и образ,
запускает его без вопросов (`-NonInteractive`) и собирает результат. Все действия идемпотентны: повторный запуск доводит ПК до нужного состояния.

```powershell
cd node
.\class-deploy.ps1                                # интерактивно: меню, мастер инвентаря, вопросы только о недостающем, план и подтверждение
.\class-deploy.ps1 -Action check                  # только достижимость по WinRM и что включить на недоступных ПК
.\class-deploy.ps1 -Action install -Only pc01     # сначала один ПК, затем остальные
.\class-deploy.ps1 -Action install -DryRun        # план и команды на каждый ПК, ничего не копируется и не запускается
.\class-deploy.ps1 -Action status                 # состояние всех нод
.\class-deploy.ps1 -Action update                 # новые скрипты и образ, затем повторная установка
.\class-deploy.ps1 -Action uninstall -Only pc15
```

### Что нужно заранее

1. **Образы ноды** `nsu-llm-node-<версия>-cpu.tar.gz` и `-gpu.tar.gz` на ПК администратора (сборка и `docker save`: «Распространение образов»). Скрипт сам ищет их рядом
   с собой и в текущей папке, причём только версии `$Version` из раздаваемого `install-node.ps1` (архив другой версии установщик не примет); иначе спросит путь или возьмёт
   из `-ImageTarCpu` / `-ImageTarGpu`. Нужен только тот образ, который нужен нодам из инвентаря.
2. **Токен агента** со страницы «Подключение ноды» портала и адрес главного.
3. **Учётная запись администратора ПК класса** (одна на все ПК). От неё запускается установка: под этой учёткой создаётся задача автозапуска и живёт дистрибутив WSL,
   поэтому её пароль нужен (им регистрируются разовая задача запуска и задача автозапуска; нигде не сохраняется). Это не должна быть SYSTEM, у неё должен быть непустой пароль
   и право «Вход в качестве пакетного задания» (у группы «Администраторы» оно есть по умолчанию, если GPO его не забирает).
4. **WinRM на каждом ПК** (следующий раздел).

### Включение WinRM

Скрипт проверяет порт 5985 (5986 с `-UseSSL`), `Test-WSMan` и вход, а для каждого недоступного ПК печатает причину и точные команды.

На каждом ПК класса, PowerShell от администратора, один раз (в домене то же делает групповая политика «Разрешить удалённое управление через WinRM»; удалённо можно через `psexec`):

```powershell
Enable-PSRemoting -Force -SkipNetworkProfileCheck
```

- **Домен.** Вход по имени ПК (не по IP) идёт через Kerberos и дополнительных настроек не требует; учётка доменная (`ДОМЕН\логин`) и входит в локальную группу «Администраторы» ПК.
- **Рабочая группа или адреса по IP.** Kerberos недоступен, WinRM по HTTP пускает только доверенные адреса. Скрипт сам печатает команду для ПК администратора и (в интерактивном режиме) предлагает выполнить её:
  ```powershell
  Set-Item WSMan:\localhost\Client\TrustedHosts -Value '192.168.3.101,192.168.3.102,...' -Force
  ```
  Если на ПК класса вход под **локальной** учёткой, не встроенным Administrator, на каждом ПК нужно ещё (иначе «Access is denied»):
  ```powershell
  New-ItemProperty -Path HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force
  ```
- **ПК администратора в домене, ПК класса в рабочей группе.** Вход по имени через Kerberos к ним не проходит. Скрипт видит неудачный вход, предлагает добавить эти ПК
  в TrustedHosts (или печатает команду с `-NonInteractive`) и повторяет вход.
- **Сетевой профиль «Общедоступная».** `-SkipNetworkProfileCheck` в команде выше обходит запрет; постоянное решение — профиль «Частная» или «Доменная» для сети класса.
- Для SMB-раздачи образа (ниже) на ПК должен быть открыт «Общий доступ к файлам и принтерам» (порт 445) и доступен админ-ресурс `C$`.

По HTTP с NTLM или Kerberos сам WinRM шифрует содержимое сессии, HTTPS для этого не нужен; `-UseSSL` — только если на ПК уже есть слушатель 5986 с сертификатом,
которому доверяет ПК администратора. Сессия открывается с профилем пользователя (без `-NoMachineProfile`): `install-node.ps1` пишет `%USERPROFILE%\.wslconfig`, WSL и DPAPI живут в профиле.

### Как установщик запускается на ПК

- **Каталог раздачи закрывается до копирования.** Права `C:\ProgramData` по умолчанию позволяют любому пользователю создать в нём каталог и стать его
  владельцем: ученик мог бы заранее создать `C:\ProgramData\NSU-LLM-stage` и подменить скрипты, которые потом запускаются от администратора.
  Поэтому перед раздачей скрипт выполняет на ПК `Secure-Dir` из `node\secure-dir.ps1` (тот же помощник использует `install-node.ps1`): владелец — группа
  «Администраторы», права без наследования от `ProgramData` (SYSTEM и «Администраторы» — полный доступ, «Пользователи» — только чтение и выполнение);
  недоверенный каталог или недоверенный объект внутри (чужой владелец, чужие права на запись, ссылка) не чинится на месте, а откладывается
  в `C:\ProgramData\NSU-LLM-stage.untrusted-<время>`; каталог создаётся заново сразу с нужными правами. Если отложить не удаётся или на месте каталога
  ссылка, ПК получает `ОШИБКА` с командой, как удалить каталог, и ничего туда не копируется. Файлы копируются во временные имена и ставятся на место
  на ПК (новый объект вместо старого). Установленную копию `C:\ProgramData\NSU-LLM\scripts\install-node.ps1` (когда в раздаче скрипта нет)
  скрипт запускает, только если `C:\ProgramData\NSU-LLM` доверенный (кроме `wsl\`). Файлы с секретами (`run-params.bin`) создаются новыми, сразу с DACL «только SYSTEM и „Администраторы“»
  (`Write-SecureFile -AdminOnly`): ни в какой момент они не доступны «Пользователям».
- **`-RunMode task` (по умолчанию).** По WinRM скрипт кладёт в `C:\ProgramData\NSU-LLM-stage` обёртку `class-run.ps1` (файл `node\class-run.ps1`: один и тот же
  запускатель разовых задач) и файл аргументов `run-params.bin` (зашифрован DPAPI LocalMachine, доступ только «Администраторам» и SYSTEM), регистрирует разовую задачу `\NSU-LLM\NSU-LLM-Deploy` от учётки администратора с её паролем
  (вход как пакетное задание, с наивысшими правами) и запускает её: `powershell -ExecutionPolicy Bypass -File class-run.ps1`. Так у установщика полноценный вход: профиль, DPAPI,
  `wsl.exe`, и нет «двойного прыжка» WinRM. Обёртка читает и сразу удаляет файл аргументов, вывод `install-node.ps1` пишет в `C:\ProgramData\NSU-LLM-stage\run.log`, код — в `run.done`.
  Скрипт читает `run.log` по ходу, по завершении удаляет задачу. Токен и пароль не попадают ни в командную строку задачи, ни в файлы открытым текстом.
  Если на ПК ещё идёт прошлый запуск, новый не стартует (в «Подробностях» — команда `Stop-ScheduledTask`).
- **`-RunMode session`.** Установщик выполняется прямо в сессии WinRM (с `Set-ExecutionPolicy -Scope Process Bypass`), вывод так же идёт в `run.log`. Запасной путь, если задача
  не создаётся (GPO запрещает пакетный вход).
- **Ход установки.** Шаги установщика (`[3/9] ...`) и строки `ОШИБКА` печатаются сразу с именем ПК, весь вывод по мере поступления пишется в журнал ПК на ПК администратора.
  Если задание оборвалось или не уложилось в `-TimeoutMin`, в журнал ПК добавляются последние 60 строк `run.log`. В режиме task при тайм-ауте установка на ПК продолжается:
  проверьте потом `-Action status -Only <имя>`.
- **Прокси.** `-Proxy http://proxy:3128` передаётся `install-node.ps1 -Proxy`: он сам настраивает прокси для `wsl --install/--update`, apt и Docker CE в WSL, демона Docker
  и контейнера ноды и собирает `NO_PROXY` (главный, адреса класса). Установщику без `-Proxy` (старая копия на ПК) выставляются только `HTTP_PROXY` и `HTTPS_PROXY`.
  Без прокси эти шаги требуют прямого выхода в интернет.

### Инвентарь и ответы

**`class-nodes.csv`** (рядом с запуском; `-Inventory` задаёт другой путь). Если файла нет, мастер спросит адреса (список или диапазон `192.168.3.101-115`), префикс имён, режим и ускоритель и создаст его:

```csv
name,address,mode,accel,docker
pc01,192.168.3.101,k3s,auto,wsl
pc02,192.168.3.102,k3s,auto,wsl
lab-gpu,192.168.3.120,k3s,nvidia,desktop
```

- `name` — имя ноды в кластере (латиница, цифры, дефис; уникально).
- `address` — IP или имя ПК для WinRM.
- `mode` — `k3s` (по умолчанию) или `standalone`.
- `accel` — `auto` (по наличию GPU на ПК: скрипт сам выбирает gpu- или cpu-образ), `cpu` или `nvidia`.
- `docker` — `wsl` (по умолчанию, и если колонки нет или ячейка пустая): нода в Docker CE внутри WSL с автозапуском без входа в Windows, даже если на ПК
  уже стоит Docker Desktop; `desktop` — оставить ноду в Docker Desktop (он запускается только после входа пользователя). Передаётся как `install-node.ps1 -Docker`.

**`class-answers.json`** — общие ответы для всех ПК: адрес главного, токен агента (зашифрован DPAPI, читается только вами на этом ПК), `AllowFrom`, пути к образам. Создаётся при первой установке;
дальше все действия идут без вопросов. `AllowFrom` по умолчанию собирается из подсетей /24 адресов инвентаря и адреса главного (с этих адресов ПК пускают 18080 и 18050). Файлы `class-nodes.csv`, `class-answers.json` и
`class-deploy-logs/` в git не попадают. Приоритет: параметры (`-Main`, `-Token`, `-AllowFrom`) → переменные `NSU_LLM_AGENT_TOKEN`, `NSU_LLM_ADMIN_USER`, `NSU_LLM_ADMIN_PASSWORD` → файл → вопрос.

### Что делает каждое действие

| Действие | На каждом ПК |
|---|---|
| `check` | Порт, `Test-WSMan`, вход. Ничего не меняет. |
| `status` | `install-node.ps1 -Status`: WSL, `.wslconfig`, GPU, дистрибутив, docker, контейнер ноды, задача автозапуска, брандмауэр, связь с главным. |
| `install` | Проверка (права администратора, Windows 11 22H2+, свободное место: два размера образа и 15 ГБ, минус образ, уже лежащий в раздаче), раздача файлов, `install-node.ps1 -NonInteractive`. После успеха раздача очищается (остаются `run.log` и метка образа `image-loaded.json`; архив образа — только с `-KeepStage`). |
| `update` | То же, что `install`: свежие скрипты. Архив образа не копируется и не передаётся, если нода установлена и метка `image-loaded.json` на ПК совпадает с архивом (имя, длина, время изменения): образ уже загружен. Иначе архив копируется (если его нет в раздаче или он другой) и передаётся установщику. Нода без установки получает полную установку. |
| `uninstall` | `install-node.ps1 -Uninstall`: нода удаляется с ПК и из кластера (нужны адрес главного и токен), затем удаляются раздаточные файлы. |

Порядок: проверка WinRM → ответы и учётка → разведка по ПК (права, GPU, место) → **план** (точная команда `install-node.ps1` для каждого ПК, токен и пароль скрыты) и подтверждение → раздача файлов →
запуск. Недоступные ПК пропускаются (в интерактивном режиме скрипт спросит, продолжать ли).

- **Параллельность.** Установка идёт на `-Throttle` ПК одновременно (по умолчанию 5), одно задание не дольше `-TimeoutMin` минут (по умолчанию 90). Раздача образа идёт по очереди:
  узкое место — сеть ПК администратора (1 ГбЕ около 110 МБ/с, gpu-образ 6,4 ГБ примерно минута на ПК, 15 ПК около 15 минут). Образ, уже лежащий на ПК и той же длины, не копируется повторно.
- **Способ копирования** `-CopyMode`: `auto` (SMB через `\\ПК\C$`, при недоступности WinRM-копирование), `smb`, `winrm` (`Copy-Item -ToSession`, медленнее и сильно нагружает память),
  `none` (файлы уже на ПК в `-StageDir`, по умолчанию `C:\ProgramData\NSU-LLM-stage`).
- **GPU.** Разведка читает `nvidia-smi --query-gpu=driver_version`. Для `accel=auto` с драйвером старее 570 раздаётся cpu-образ, в плане — «драйвер старее 570: cpu»
  (после обновления драйвера повторите установку). Если установщик сообщает «GPU не виден в WSL», в «Подробностях» будет совет: поставить `accel=cpu` для этого ПК
  в `class-nodes.csv` и повторить с `-Only <имя>`.
- **Повтор после удаления архива.** Метку `image-loaded.json` пишет скрипт после успешного запуска с архивом. Если образ потом пропал с ПК (удалили вручную),
  установщик без архива сообщит «укажите -ImageTar»: метка снимается, в «Подробностях» — команда повтора, и следующий запуск скопирует архив заново.
- **Перезагрузка.** Если WSL ставится впервые, `install-node.ps1` просит перезагрузку (код 3010). Скрипт помечает такой ПК «ПЕРЕЗАГРУЗКА», печатает `Restart-Computer ...` и команду повтора с `-Only`.
  Ответы сохранены на ПК, повтор идёт без вопросов.

### Отчёт и журналы

Итоговая таблица: `Имя / Адрес / Режим / Ускоритель / Результат / Время / Подробности`. Результаты: `ГОТОВО`, `ПЕРЕЗАГРУЗКА`, `ПРОБЛЕМЫ` (только `status`: число проблем и первые две),
`ОШИБКА` (в «Подробностях» строки `ОШИБКА:` и `Как исправить:` установщика), `НЕДОСТУПЕН` (WinRM), `ПРОПУЩЕН` (нет прав, мало места, старая Windows). Журнал каждого ПК —
`class-deploy-logs\<дата-время>\<имя>.log` (полный вывод установщика; токен и пароль заменены на `***`), общая таблица — `summary.txt` там же.
Код возврата: 0 всё хорошо, 2 нужна только перезагрузка, 1 есть ошибки, недоступные, пропущенные или проблемные ПК. Для автоматизации: `-NonInteractive` (все ответы из параметров, переменных или файла).

### Рекомендованный порядок первого развёртывания

1. `.\class-deploy.ps1 -Action check` — все ПК отвечают.
2. `.\class-deploy.ps1 -Action install -Only pc01` — один ПК до конца; проверить в портале, что нода появилась, и `-Action status -Only pc01`.
   Установщик впервые запускается через WinRM: если WSL в сессии WinRM ведёт себя иначе, чем в интерактивной сессии (это в тестах не проверялось), это проявится здесь, а не на 15 ПК сразу.
3. `.\class-deploy.ps1 -Action install` — остальные.
4. Повторите `-Action install -Only <имена с перезагрузкой>` после перезагрузки.

Тесты (без сети и WinRM; `Invoke-Command`, `New-PSSession`, `Copy-Item -ToSession`, `Test-WSMan`, SMB-диски и порты подменены; в `secure-dir.ps1` подменены только смена
владельца и `takeown` (точки подмены `Invoke-SdIcacls`, `Invoke-SdTakeOwn`, `New-SdAdminOnlySecurity`), остальное выполняется по-настоящему): `bash node/tests/test_class_deploy.sh`. Скрипт работает и из Windows PowerShell 5.1, запущенного из pwsh 7: пути модулей pwsh 7 из
`PSModulePath` убираются, командлеты `*-SecureString` не используются (токен в `class-answers.json` — DPAPI CurrentUser в прежнем формате).
