# k3s внутри Docker: server (MAIN) + agent (NODE), GPU, кросс-подсети

Дата исследования: 2026-11-02. Метки: **[V]** = проверено по первоисточнику (docs / исходники / GitHub API) в этой сессии; **[UNVERIFIED]** = логический вывод или память, на железе/в доках не подтверждено.

---

## 0. Итоговые решения (TL;DR)

| Параметр | Решение |
|---|---|
| Версия k3s | `v1.36.5+k3s1` (канал `stable` на 2026-11-02 [V], образ `rancher/k3s:v1.36.5-k3s1` есть на Docker Hub, опубликован 2026-09-30 [V]). Консервативный фолбэк: `v1.35.9-k3s1` [V тег существует]. В Docker-тегах `+` заменяется на `-`; в URL GitHub-релиза `+` = `%2B`. Не использовать `latest` (в docs: "latest isn't maintained"). |
| Состав k3s 1.36.5 | containerd v2.3.4-k3s1.36, runc 1.4.2, flannel v0.28.4, CoreDNS 1.14.7 [V, release notes] |
| База образов | MAIN и NODE: `ubuntu:24.04` + бинарь `k3s` с GitHub releases (а НЕ `FROM rancher/k3s`, т.к. тот `FROM scratch`, без apt, без glibc-инструментов; для nvidia-runtime нужен Ubuntu/Debian — k3d docs: "NVIDIA container runtime is not yet supported on Alpine" [V]) |
| Запуск контейнеров | `--privileged`, `--tmpfs /run --tmpfs /var/run`, `ulimit nproc/nofile=65535`, **named volumes** на `/var/lib/rancher/k3s` и `/var/lib/kubelet` (вложенный overlayfs поверх overlay2/containerd-снапшоттера Docker не работает) |
| Сеть агента | Linux demo-сервер (shared Docker host): **bridge + `-p`**, не `--network host`. Prod WSL2: Docker CE внутри WSL2 + `networkingMode=mirrored` + `--network host` (вся «хост»-сеть = изолированная WSL VM); фолбэк — Docker Desktop + `-p`. |
| Инференс | hostNetwork-pods + `--node-external-ip=<LAN-IP хоста>` на агенте; gateway берёт `ExternalIP` из `Node.status.addresses`, а не `InternalIP`. Оверлей (flannel) оставляем дефолтным только чтобы ноды были `Ready`. |
| CIDR | `--cluster-cidr=100.64.0.0/16 --service-cidr=100.65.0.0/16 --cluster-dns=100.65.0.10` (запасной вариант `198.18.0.0/16` + `198.19.0.0/16`) |
| Отключить | `--disable=traefik,servicelb,metrics-server,local-storage` + `--disable-network-policy` (coredns оставить) |
| Образы 10 ГБ | pull-through registry на MAIN + `registries.yaml` на нодах (надёжнее spegel в нашей топологии); альтернатива — `docker save` в `/var/lib/rancher/k3s/agent/images/` |
| GPU (WSL2) | Нет задокументированного готового рецепта k3s-in-Docker на WSL2. Все компоненты по отдельности подтверждены (см. §3), собранная цепочка — **[UNVERIFIED]**; нужен smoke-test (§3.6). |

---

## 1. k3s server/agent в Docker

### 1.1 Официальная база [V]

- docs.k3s.io/advanced: пример `docker run --privileged --name k3s-server-1 --hostname k3s-server-1 -p 6443:6443 -d rancher/k3s:v1.24.10-k3s1 server`. Про tmpfs/volumes в docs ничего нет.
- Реальный эталон — `docker-compose.yml` в репо k3s-io/k3s [V, прочитан через gh api]: `privileged: true`, `tmpfs: [/run, /var/run]`, `ulimits nproc=65535 nofile=65535`, `restart: always`, named volume на `/var/lib/rancher/k3s`, `K3S_TOKEN` обязателен, у агента `K3S_URL=https://server:6443`.
- Dockerfile образа `rancher/k3s` [V]: `FROM scratch`, `VOLUME /var/lib/kubelet /var/lib/rancher/k3s /var/lib/cni /var/log`, `ENTRYPOINT ["/bin/k3s"]`, `CMD ["agent"]`.

### 1.2 Флаги `docker run`

```bash
# --- MAIN (server), Docker Desktop / Windows ---
docker run -d --name llm-main --hostname llm-main \
  --privileged --restart unless-stopped \
  --tmpfs /run --tmpfs /var/run \
  --ulimit nproc=65535 --ulimit nofile=65535:65535 \
  -v llm-main-k3s:/var/lib/rancher/k3s \
  -v llm-main-kubelet:/var/lib/kubelet \
  -p 6443:6443 \
  -p 9000:9000 \                      # порт gateway (пример)
  -e K3S_TOKEN='<длинный-случайный-секрет>' \
  llm-main:TAG \
  server \
    --node-name llm-main \
    --tls-san 192.168.3.21 --tls-san main.example.campus \
    --node-external-ip 192.168.3.21 \
    --cluster-cidr 100.64.0.0/16 --service-cidr 100.65.0.0/16 --cluster-dns 100.65.0.10 \
    --disable traefik,servicelb,metrics-server,local-storage \
    --disable-network-policy \
    --write-kubeconfig-mode 644

# --- NODE (agent), Linux demo 192.168.3.200, bridge-режим ---
docker run -d --name llm-node --hostname ubuntu200 \
  --privileged --restart unless-stopped \
  --tmpfs /run --tmpfs /var/run \
  --ulimit nproc=65535 --ulimit nofile=65535:65535 \
  -v llm-node-k3s:/var/lib/rancher/k3s \
  -v llm-node-kubelet:/var/lib/kubelet \
  -p 18080:18080 -p 18050:18050 \        # порты инференса 18080 и RPC 18050 (НЕ 80/443/8000/8080)
  -e K3S_URL=https://192.168.3.21:6443 -e K3S_TOKEN='<тот же секрет>' \
  llm-node:TAG \
  agent --node-name ubuntu200 --node-external-ip 192.168.3.200 \
        --node-label llm.role=worker --node-label llm.gpu=false
# GPU-вариант: добавить  --gpus all  (Linux: nvidia-container-toolkit на хосте)
```

Обоснование:

- `--privileged`: kubelet/containerd внутри требуют mount, cgroup rw, /dev/kmsg, модули. Подтверждено docs/compose [V]. `--cgroupns=host` в официальных примерах **не используется** [V]; при `--privileged` cgroup монтируется rw. Необходимость `--cgroupns=host` на cgroup v2 — [UNVERIFIED], не нужна по официальному compose.
- **k3s должен быть PID 1**: на cgroup v2 k3s при `os.Getpid()==1` делает `EvacuateCgroup2("init")` (перенос процессов из корневого cgroup в `init`, иначе нельзя включить `subtree_control`) [V, исходник `pkg/cli/cmds/init_linux.go`, `pkg/cli/server/server.go`]. Если в MAIN рядом запускается gateway (supervisord/shell-скрипт как PID 1), k3s не PID 1 → эвакуации нет → ошибка вида `failed to evacuate root cgroup: mkdir /sys/fs/cgroup/init: read-only file system` / невозможность включить контроллеры (issue k3s-io/k3s#4873 [V]; поведение "evacuation only runs if k3s is pid 1" подтверждено комментарием maintainer'а). Решения: (a) entrypoint-скрипт делает эвакуацию сам (как k3d: `mkdir /sys/fs/cgroup/init; xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs; echo +cpu +cpuset +io +memory +pids > /sys/fs/cgroup/cgroup.subtree_control` — последовательность по памяти, **[UNVERIFIED]**), затем `exec k3s ...`; (b) запускать gateway вторым контейнером (compose) — проще и чище, но в задаче "MAIN = один образ"; (c) k3s PID 1 в entrypoint (`exec /usr/local/bin/k3s "$@"`), а gateway стартует фоном до `exec`... (тогда gateway в корневом cgroup до эвакуации переедет в `init` вместе со всеми — это нормально: эвакуируются все процессы корня).
- Нужен `docker-init`/reaper? При PID 1 k3s сам делает "subprocess reaping if necessary" [V, server.go].
- `/var/lib/rancher/k3s` и `/var/lib/kubelet` — **только named volumes**, не bind-mount с Windows (9p/virtiofs) и не слой контейнера: overlayfs-снапшоттер containerd (`--snapshotter` default `overlayfs` [V]) не заводится на overlay-rootfs. Для хостов с ZFS/btrfs — `--snapshotter=native` или fuse-overlayfs [UNVERIFIED].
- Постоянное имя ноды (`--node-name`/`K3S_NODE_NAME`) + постоянный volume: агент хранит node-password, сервер — хэш; пересоздание контейнера без volume при том же имени → отказ регистрации. Лечение: `kubectl delete node <name>` (docs: "deleting the node removes its node password secret" [V, uninstall page]) либо `--with-node-id`.
- Сменить CA/сервер (пересоздать volume MAIN) → у агентов надо тоже удалить volume.

### 1.3 Порты (таблица docs.k3s.io/installation/requirements [V])

| Proto | Порт | Откуда → куда | Когда |
|---|---|---|---|
| TCP | 6443 | agents → servers | supervisor + kube-apiserver (**обязательно**) |
| TCP | 10250 | all ↔ all | kubelet API/metrics (нужен metrics-server'у напрямую; для logs/exec НЕ нужен, см. 1.4) |
| UDP | 8472 | all ↔ all | только flannel VXLAN (дефолт); не публиковать в интернет |
| UDP | 51820 / 51821 | all ↔ all | только flannel wireguard-native (IPv4 / IPv6) |
| TCP | 5001 + 6443 | all ↔ all | только embedded registry (spegel) |
| TCP | 2379-2380 | server ↔ server | только HA etcd |

Дополнительно по нашему дизайну: gateway(MAIN) → NODE `<IP>:<порты инференса>` (hostNetwork). В prod firewall: node→main `6443/tcp`; main→node `18080/tcp` (инференс), нода↔нода `18050/tcp` (RPC режима split) (пример); остальное не требуется, если оверлей не используется.

### 1.4 Reverse tunnel и `--egress-selector-mode`

- docs.k3s.io/installation/requirements [V]: "K3s uses reverse tunneling such that the nodes make outbound connections to the server and all kubelet traffic runs through that tunnel." → **inbound к агенту для `kubectl logs/exec/port-forward` не нужен**: агент открывает websocket к `server:6443` (architecture page [V]: "Agent nodes are registered with a websocket connection initiated by the `k3s agent` process"; client-side LB в агенте).
- Таблица портов формально всё равно перечисляет 10250 "All → All", но это для metrics-server и прямых клиентов kubelet.
- `--egress-selector-mode` (default `agent`) [V, docs.k3s.io/cli/server + basic-network-options]:
  - `disabled` — apiserver не использует туннели; серверу нужна прямая связность с агентами, иначе `kubectl exec/logs` ломаются.
  - `agent` (дефолт) — apiserver ходит к kubelet'ам через туннели агентов. Туннелируется трафик к **нодам/kubelet**; трафик к endpoint'ам сервисов (webhooks, aggregated API на подах) — НЕ туннелируется, идёт по pod-сети.
  - `pod` — дополнительно маршрутизирует соединения к endpoint'ам сервисов, отслеживая Nodes (docs: "watching Nodes"; в одной редакции "Nodes and Endpoints"); не работает с CNI со своим IPAM.
  - `cluster` — маршрутизирует kubelet- и endpoint-соединения, отслеживая Pods и Endpoints; самый переносимый, больше накладных расходов.
- Для нашей топологии (оверлей между подсетями не гарантирован): `agent` достаточно, пока на агентах нет webhook/aggregated-API подов; если появятся — `cluster`. Менять надо на всех servers одинаково.

### 1.5 Версия/подводные камни в 1.36.5 [V release notes]

- Traefik chart 40.x с breaking change — мы traefik отключаем.
- Фикс "deploy controller not re-applying manifests when mtime is pinned to the epoch" (#14568) — актуально для воспроизводимых сборок (`SOURCE_DATE_EPOCH`) с манифестами в образе.
- containerd 2.x: если понадобится кастомный шаблон containerd, в k3s ≥1.3x он называется `config-v3.toml.tmpl` — [UNVERIFIED]; рекомендация: шаблоны не использовать, полагаться на автодетект runtime.
- Диагностика ядра Docker Desktop/WSL: `k3s check-config` (подкоманда есть в 1.36, судя по release notes) [V косвенно].

---

## 2. Server в Docker Desktop (NAT-VM на Windows): могут ли LAN-агенты присоединяться

**Да, по опубликованному 6443** — но с условиями.

Join-путь: агент → `https://192.168.3.21:6443` (Windows-хост; Docker Desktop публикует порт на Windows через свой backend). Нужно:

1. Inbound-правило Windows Firewall на TCP 6443 (netsh/PowerShell). Создаёт ли Docker Desktop его сам — [UNVERIFIED].
2. `--tls-san 192.168.3.21` (+ DNS-имя) на сервере: иначе сертификат supervisor не содержит адрес, по которому ходят агенты (SAN см. `--tls-san` в docs [V]).
3. `--node-external-ip 192.168.3.21` на сервере: `--advertise-address` по умолчанию = `node-external-ip/node-ip` [V]. Иначе в Endpoints `kubernetes` публикуется IP контейнера (172.17.x.x), и агентский LB, который "starts with the host from the `--server` address and syncs in the kube-apiserver addresses" [V, architecture], получит недостижимый адрес. Что именно он делает с недостижимыми (оставляет ли исходный `K3S_URL` как fallback) — [UNVERIFIED], поэтому SAN+external-ip обязательны.
4. Токен: фиксированный `K3S_TOKEN` (без CA-пина) проще; "secure" токен `K10<ca-hash>::server:pass` пинит CA.

Что ломается за NAT (Docker Desktop VM / bridge-контейнер):

| Компонент | Поведение | Статус |
|---|---|---|
| `kubectl logs/exec` к агентам | работают через reverse tunnel | [V docs] |
| flannel VXLAN (UDP 8472) между нодами | public-ip ноды = node-ip = IP контейнера/VM (172.x), другим нодам недостижим → pod↔pod между нодами, доступ подов агентов к CoreDNS/ClusterIP на MAIN не работает | вывод, [UNVERIFIED на железе], но согласуется с docs: flannel использует node-ip, `--flannel-external-ip` "only takes effect when `--node-external-ip` is set" [V] |
| `--node-ip` | нельзя выставить в LAN-IP Windows/Linux-хоста из bridge-контейнера (kubelet проверяет, что IP принадлежит локальному интерфейсу) | [UNVERIFIED] (поведение kubelet из памяти) |
| `InternalIP` ноды | = IP контейнера. Всё, что к нему ходит напрямую (metrics-server, Prometheus), ломается | вывод |
| `--node-external-ip` | не валидируется на локальность → корректно описывает достижимый адрес | [V flag description] |
| host-gw | требует L2 между нодами [V docs] — нам не подходит |
| wireguard-native | нужен модуль WireGuard в ядре на каждой ноде [V docs]; WSL2 kernel 5.15+ включает wireguard (по стороннему гайду) [V частично]; UDP 51820 надо публиковать и пробивать NAT | [UNVERIFIED] |
| Tailscale integration (`--vpn-auth`) | experimental, k3s ≥ 1.27.3/1.26.6/1.25.11 [V docs] — вариант для Docker-in-NAT, но требует аккаунт/headscale | опционально |

Официальный "multicloud" рецепт [V, docs.k3s.io/networking/distributed-multicloud]: сервер `--node-external-ip=<EXT> --flannel-backend=wireguard-native --flannel-external-ip`; агент `--node-external-ip=<EXT>`; `K3S_URL` на внешний IP сервера; "each node needs a unique, reachable IP"; ports должны быть открыты на internal и external адресах. Для Docker-bridge это означает публикацию `-p 51820:51820/udp` на **каждом** хосте.

**Рекомендация**: не строить на оверлее. Оставить дефолтный flannel vxlan только ради состояния `Ready` (без CNI-конфига kubelet репортит NotReady — поведение известное, [UNVERIFIED в docs]; поэтому `--flannel-backend=none` без собственного CNI не использовать), всё нужное для работы (инференс) — hostNetwork, адрес берётся из ExternalIP. Системные поды (coredns и т.п.) живут на MAIN-ноде; инференс-поды — `nodeSelector: llm.role=worker`.

Что отключать: `--disable traefik,servicelb,metrics-server,local-storage` (valid items в help: coredns, servicelb, traefik, local-storage, metrics-server, runtimes [V]); `--disable-network-policy` (меньше правил iptables); coredns оставить. `servicelb` (klipper) на LB-сервисах создаёт поды-прокси — не нужен.

---

## 3. Agent в Docker Desktop / WSL2 + NVIDIA GPU

### 3.1 Что подтверждено по компонентам

| Факт | Источник |
|---|---|
| k3s автоопределяет `nvidia-container-runtime` в `$PATH` при старте и пишет runtime `nvidia` в containerd config; RuntimeClass `nvidia` создаётся k3s; в поде нужен `runtimeClassName: nvidia`, если не задан `--default-runtime` | docs.k3s.io/advanced [V] + исходник `pkg/agent/containerd/runtimes.go`: ищутся бинари `nvidia-container-runtime` (→`nvidia`), `nvidia-container-runtime-experimental`, `nvidia-container-runtime.cdi` (→`nvidia-cdi`) [V] |
| Проверка: `grep nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml` | docs [V] |
| k3d CUDA-гайд (Linux): Dockerfile `FROM nvcr.io/nvidia/cuda:12.9.2-base-ubuntu22.04`, ставит nvidia-container-toolkit, копирует содержимое `rancher/k3s` (`COPY --from=k3s / / --exclude=/bin` + `COPY --from=k3s /bin /bin`), кладёт device-plugin DaemonSet в `/var/lib/rancher/k3s/server/manifests/`, `ENTRYPOINT ["/bin/k3s"]`, `CMD ["agent"]`; запуск `k3d cluster create gputest --image=$IMAGE --gpus=1` | k3d.io/stable/usage/advanced/cuda [V] |
| Docker Desktop `--gpus` работает только на Windows с WSL2 backend; только Linux-контейнеры | docs.docker.com/desktop/features/gpu [V]; про privileged/nested в доке ничего нет |
| WSL: GPU через `/dev/dxg` (не `/dev/nvidia*`), либы в `/usr/lib/wsl/lib`; драйвер ставится только в Windows; NVML ограничен; pinned memory ограничен; фильтр GPU по индексу невозможен | docs.nvidia.com/cuda/wsl-user-guide [V] |
| device-plugin на WSL2: до 0.14 не поддерживался; поддержка через CDI. Фикс "wsl: report a single `all` device to kubelet" (PR #1671, смёржен 2026-04-15) бэкпортирован в **v0.19.1** (2026-04-23) и далее v0.19.2/0.19.3/0.20.x | github.com/NVIDIA/k8s-device-plugin (releases, PR #1671, #1699) [V] |
| nvidia-container-toolkit **v1.20.0**: WSL2 CDI discovery берёт все .so/.bin/.dll из driver store (issue #1864, PR #1890). Регрессия на WSL2 в **1.19.1** (SIGSEGV, "fix: downgrade to 1.18.0-1"). Актуально: 1.20.1 (2026-09-19). Авто-режим runtime по умолчанию = `jit-cdi` для NVML- и WSL-платформ | releases + `internal/info/auto.go` [V] |
| Установка: пакеты `nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1` с `=1.20.1-1` | docs.nvidia.com install-guide [V] |
| NVIDIA OpenShell (k3s-в-Docker с GPU на WSL2) пытался решать это в `cluster-entrypoint.sh` через `nvidia-ctk cdi generate` + ручной libdxcore + режим `cdi` + label `feature.node.kubernetes.io/pci-10de.present=true`; PR #441 закрыт без merge ("semi-tested-and-untested"), PR #608 про device-plugin v0.19.1 | github.com/NVIDIA/OpenShell [V] — **это лучший прецедент, но не готовый рецепт** |
| Известные ловушки WSL2 GPU-ноды в k8s: NFD не видит PCI → ручной label `feature.node.kubernetes.io/pci-10de.present=true` (актуально для GPU Operator/NFD; сам device-plugin DaemonSet NFD не требует — [UNVERIFIED]); `runtimeClassName: nvidia` обязателен; MTU 1500 | wirekube WSL2 guide [V] |

### 3.2 Рекомендуемый рецепт A (самый вероятный): Docker Engine внутри WSL2 + mirrored networking

На каждой класс-машине (Windows 11 22H2+; на Win10 mirrored недоступен — [V: "Windows 11 22H2 and higher"]):

1. Windows: драйвер NVIDIA (WSL-совместимый) в Windows; **в WSL драйвер не ставить** [V].
2. `%UserProfile%\.wslconfig`:
   ```ini
   [wsl2]
   networkingMode=mirrored
   dnsTunneling=false          # по wirekube-гайду: иначе SNAT/timeouts (там про Cilium; для flannel — [UNVERIFIED])
   memory=24GB                 # подобрать
   swap=0
   [experimental]
   autoMemoryReclaim=gradual
   ```
   `wsl --shutdown`, затем inbound Hyper-V firewall (админ-PowerShell) [V, MS docs]:
   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   # или точечно:
   New-NetFirewallHyperVRule -Name llm-inference -DisplayName "LLM inference" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 18080,18050
   ```
   Плюс обычное правило Windows Firewall на те же порты.
3. WSL Ubuntu 24.04, `/etc/wsl.conf` → `[boot] systemd=true`; `apt install docker-ce` (официальный репо Docker); `nvidia-container-toolkit=1.20.1-1` **в WSL**; `nvidia-ctk runtime configure --runtime=docker && systemctl restart docker`.
4. Smoke 1: `docker run --rm --gpus all ubuntu nvidia-smi` (внутри WSL `nvidia-smi` лежит в `/usr/lib/wsl/lib/`) [V из CUDA-on-WSL guide].
5. Запуск NODE:
   ```bash
   docker run -d --name llm-node --privileged --gpus all --network host \
     --restart unless-stopped --tmpfs /run --tmpfs /var/run \
     --ulimit nproc=65535 --ulimit nofile=65535:65535 \
     -v llm-node-k3s:/var/lib/rancher/k3s -v llm-node-kubelet:/var/lib/kubelet \
     -e K3S_URL=https://<main-ip>:6443 -e K3S_TOKEN=... \
     llm-node:TAG agent --node-name <hostname> --node-label llm.role=worker --node-label llm.gpu=true
   ```
   При mirrored + `--network host` в WSL-VM нода видит LAN-IP Windows-хоста → `node-ip` корректный без `-p` и `--node-external-ip`. Весь flannel/iptables/cni0 остаётся внутри WSL-VM.
6. Dockerfile NODE (эскиз):
   ```dockerfile
   FROM ubuntu:24.04
   ARG K3S_VERSION=v1.36.5+k3s1
   ARG NVIDIA_CTK_VERSION=1.20.1-1
   RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg iproute2 kmod \
    && curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
    && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
       | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
       > /etc/apt/sources.list.d/nvidia-container-toolkit.list \
    && apt-get update \
    && apt-get install -y nvidia-container-toolkit=${NVIDIA_CTK_VERSION} nvidia-container-toolkit-base=${NVIDIA_CTK_VERSION} \
                          libnvidia-container-tools=${NVIDIA_CTK_VERSION} libnvidia-container1=${NVIDIA_CTK_VERSION}
   RUN curl -fsSL -o /usr/local/bin/k3s "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION//+/%2B}/k3s" \
    && chmod +x /usr/local/bin/k3s       # сверить sha256 из sha256sum-amd64.txt релиза
   VOLUME /var/lib/kubelet /var/lib/rancher/k3s /var/lib/cni /var/log
   ENTRYPOINT ["/usr/local/bin/k3s"]
   CMD ["agent"]
   ```
   Не нужен полный `nvidia/cuda` базовый образ: драйверные либы (`libcuda`, `libnvidia-ml`, на WSL ещё `libdxcore` и `/dev/dxg`) внешний `--gpus all` инжектит во внешний контейнер, а внутренний nvidia-runtime пробрасывает их в поды — [UNVERIFIED для вложенного случая]. `nvidia/cuda:*-base-ubuntu24.04` как база — безопасный запасной вариант (так делает k3d guide). Для Docker-тегов `+` в `ARG` bash-подстановка `${K3S_VERSION//+/%2B}` работает в `RUN` только под bash — использовать `SHELL ["/bin/bash","-c"]` или передавать готовый `K3S_URL_VERSION`.

   CPU-вариант (demo Linux без GPU) использует тот же образ: runtime `nvidia` будет найден, но не используется; DaemonSet device-plugin обязан иметь `nodeSelector: llm.gpu: "true"`, иначе на CPU-ноде будет CrashLoop.

### 3.3 Device plugin (манифест в MAIN-образе)

Положить в `/var/lib/rancher/k3s/server/manifests/nvidia-device-plugin.yaml` (см. §6 про volume-ловушку). Базис — статический `deployments/static/nvidia-device-plugin.yml` релиза, образ `nvcr.io/nvidia/k8s-device-plugin:v0.19.3` (v0.19.1 — минимальная с WSL-фиксом [V]; v0.20.1 свежайшая, WSL-фикс в ней по хронологии должен быть, но не проверялась — [UNVERIFIED]). В статическом манифесте **нет** `runtimeClassName` [V] — добавить:

```yaml
spec:
  template:
    spec:
      runtimeClassName: nvidia
      nodeSelector: { llm.gpu: "true" }
      # остальное как в upstream: tolerations nvidia.com/gpu, priorityClassName system-node-critical,
      # hostPath /var/lib/kubelet/device-plugins, securityContext allowPrivilegeEscalation=false, drop ALL
```
`RuntimeClass nvidia` создаёт сам k3s при обнаружении рантайма [V docs]; k3d-манифест создавал свой, но в k3s ≥ документации это встроено.

### 3.4 Рецепт B: Docker Desktop (WSL2 backend) + `-p`

То же, но вместо `--network host`: bridge + `--node-external-ip <LAN-IP Windows>` + `-p 18080:18080 -p 18050:18050`. Минусы: `InternalIP` ноды = IP контейнера, flannel public-ip недостижим (допустимо, оверлей не используем). Docker Desktop "Enable host networking" (4.34+) **не заменяет** mirrored: он работает на L4, процессы в контейнере "can't bind to the host's IP addresses" [V docs.docker.com host driver], на форуме Docker подтверждено, что DD-движок не использует mirrored-интерфейс WSL (форум 147994) [V поиск, анекдотично] → k3s увидит IP VM, не LAN-IP. Не использовать.

### 3.5 Известные блокеры/режимы отказа (по убыванию вероятности)

1. **Вложенный nvidia-runtime не находит GPU** (NVML "Failed to initialize NVML", `nvidia-container-cli: initialization error`): нужны во внешнем контейнере `/dev/dxg` и `/usr/lib/wsl/lib/{libcuda.so.1,libdxcore.so,libnvidia-ml.so.1,...}`. Если `--gpus all` их не инжектит в privileged-контейнер на Docker Desktop — фолбэк `--device /dev/dxg -v /usr/lib/wsl:/usr/lib/wsl:ro` [UNVERIFIED]. В Docker Desktop у `--privileged --gpus all` комбинация в доке не описана [V: "doesn't mention privileged"].
2. **libdxcore.so / CDI-спека** (toolkit #1739, PR OpenShell #441): `nvidia-ctk cdi generate` на WSL может не включить libdxcore; в 1.20.x переработан discovery (PR #1890), issue #1864 закрыт 2026-07-07 [V]. Поэтому пинить `>=1.20.1`, не 1.19.1.
3. **device-plugin на WSL**: имя CDI-устройства (`all` vs UUID/index) — исправлено в плагине ≥0.19.1 [V]; на WSL репортится ровно 1 `nvidia.com/gpu` (мульти-GPU WSL всё равно не разделяется) [V]. Старый плагин (<0.14) падает с "error getting GPU device minor number: Not Supported" [V issue #332].
4. **Обход без device-plugin** (страховка): под с `runtimeClassName: nvidia` и `env NVIDIA_VISIBLE_DEVICES=all`, без `resources.limits.nvidia.com/gpu`; учёт слотов делаем сами (nodeSelector + 1 под на ноду). [UNVERIFIED для вложенного WSL]. Для 4070 с одним GPU на машину это приемлемо.
5. **cgroup v2 / evacuation** — см. §1.2 (k3s не PID 1).
6. **Mirrored-mode**: порты, занятые Windows, недоступны WSL и наоборот (`ignoredPorts` в `.wslconfig`); `dnsTunneling`; требования Win11 22H2+; Hyper-V firewall по умолчанию режет inbound [V MS docs]; Docker Desktop 4.40–4.41 не тянет образы при mirrored + system proxy (форум) [V поиск].
7. **Ограничения CUDA на WSL2**: нет полного Unified Memory, ограниченный pinned memory, NVML без части запросов [V NVIDIA WSL guide]. vLLM на RTX 4070 12 ГБ, как правило, стартует, но `--cpu-offload-gb`/swap-space и pinned-память — **[UNVERIFIED]**; для надёжности иметь CPU-фолбэк (llama.cpp).
8. Диск: образ vLLM ~10 ГБ + распаковка (content + snapshot) ≈ 2x; kubelet `image-gc-high-threshold` 85 % и DiskPressure-eviction на маленьких дисках; ext4.vhdx WSL растёт, но не сжимается сам.
9. Автозапуск после перезагрузки Windows: WSL не стартует сам (в гайде wirekube — scheduled task at logon) [V]; `restart: unless-stopped` не поможет, пока WSL не запущен.

### 3.6 Smoke-test лестница (по шагам, останавливаться на первом падении)

```bash
docker run --rm --gpus all ubuntu nvidia-smi                       # 1. внешний GPU
docker exec llm-node sh -c 'ls -l /dev/dxg /usr/lib/wsl/lib/ ; nvidia-smi'   # 2. GPU виден внутри k3s-контейнера (WSL: /usr/lib/wsl/lib/nvidia-smi)
docker exec llm-node nvidia-container-cli info                     # 3. WSL-детект
docker exec llm-node k3s check-config                              # 4. ядро
docker exec llm-node grep nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml   # 5. runtime найден
kubectl -n kube-system logs ds/nvidia-device-plugin-daemonset      # 6. плагин
kubectl get node <n> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'                  # 7. =1
# 8. тест-под: runtimeClassName: nvidia, image nvidia/cuda:12.6.2-base-ubuntu24.04, command: nvidia-smi
```

---

## 4. Agent на Linux с NVIDIA GPU (`--gpus all`)

Это путь, который задокументирован (k3d CUDA) [V].

Хост: драйвер NVIDIA + `nvidia-container-toolkit` + `nvidia-ctk runtime configure --runtime=docker` + restart docker [V install-guide].

```bash
docker run -d --name llm-node --privileged --gpus all \
  --tmpfs /run --tmpfs /var/run --ulimit nproc=65535 --ulimit nofile=65535:65535 \
  -v llm-node-k3s:/var/lib/rancher/k3s -v llm-node-kubelet:/var/lib/kubelet \
  -p 18080:18080 -p 18050:18050 \
  -e K3S_URL=https://<main>:6443 -e K3S_TOKEN=... \
  llm-node:TAG agent --node-external-ip <host-LAN-IP> --node-label llm.role=worker --node-label llm.gpu=true
```

Образ — тот же NODE (§3.2 Dockerfile). Отличия от k3d-гайда: у k3d база `nvcr.io/nvidia/cuda:12.9.2-base-ubuntu22.04` + копирование файловой системы `rancher/k3s`; у нас — `ubuntu:24.04` + одиночный бинарь k3s (бинарь самораспаковывается в `/var/lib/rancher/k3s/data/<hash>/` при первом старте — [UNVERIFIED-деталь], общеизвестное поведение).

Device plugin: `nvcr.io/nvidia/k8s-device-plugin:v0.19.3` (см. §3.3). k3d-гайд использует `v0.15.0-rc.2` (устарел) [V].

Возможные проблемы: после `systemctl daemon-reload` на хосте Docker-контейнеры с GPU теряют доступ ("Failed to initialize NVML: Unknown Error") — классический баг cgroup-device-rules (NVIDIA/nvidia-container-toolkit), для privileged-контейнера смягчён, но не исключён — [UNVERIFIED]. Имя CDI/legacy: на Linux при `NVIDIA_VISIBLE_DEVICES` режим runtime `auto` → `jit-cdi` (toolkit 1.20) [V]; legacy-хуки требуют `nvidia-container-cli` во внутреннем контейнере (поставляется `libnvidia-container-tools`).

---

## 5. CIDR для кампуса с 10.x

Факты [V]: дефолты `--cluster-cidr 10.42.0.0/16`, `--service-cidr 10.43.0.0/16`, `--cluster-dns 10.43.0.10` ("should be in your service-cidr range"); max service-cidr — /12 (IPv4); при смене маски cluster-cidr корректировать `node-cidr-mask-size-ipv4`; если `--cluster-dns` не задан, k3s берёт 10-й адрес service-CIDR (исходник `pkg/cli/server/server.go`: `GetIndexedIP(svcCIDR, 10)`). `kubernetes` ClusterIP = первый адрес service-CIDR.

Предлагаемо:

```
--cluster-cidr=100.64.0.0/16   # 256 нод по /24
--service-cidr=100.65.0.0/16
--cluster-dns=100.65.0.10
```
Запас: `198.18.0.0/16` и `198.19.0.0/16` (RFC 2544 benchmark space).

Предостережения:

- **Задать до первого старта сервера**; изменить на живом кластере = пересоздание datastore (volume) [вывод; docs говорят это про dual-stack: "must be configured at cluster creation"].
- `100.64.0.0/10` = CGNAT/RFC 6598: конфликт с Tailscale (100.x), некоторыми кампусными/ISP CGNAT и VPN. Проверить `ip route`/traceroute в кампусе на 100.64–100.65. Не использовать 172.16/12 (Docker bridge-пулы 172.17–31, подсеть WSL2 NAT часто 172.x), 192.168/16 (LAN demo 192.168.3.0/24; Docker 2-й пул), 10/8 (кампус; 192.168.65.0/24 — внутренняя сеть Docker Desktop VM).
- Реальный риск 10.42/10.43 нереализуется как "полный перехват" 10.43.0.0/16: kube-proxy делает DNAT только для существующих ClusterIP; но (a) любой новый Service с `ClusterIP == 10.43.57.27` перехватит DNS кампуса, (b) flannel добавляет маршруты на /24 каждой ноды из 10.42.0.0/16 — хосты кампуса 10.42.x.x окажутся недостижимыми с ноды. [вывод, UNVERIFIED эмпирически]
- hostNetwork-поды cluster-CIDR не используют. CoreDNS форвардит на `/etc/resolv.conf` ноды (в Docker — Docker DNS/хостовой, где может быть 10.43.57.27) — внутри пода-CoreDNS этот адрес должен быть маршрутизируем к кампусу; со сменой service-CIDR на 100.65 коллизии нет.
- `dnsPolicy` hostNetwork-подов по умолчанию `Default` (DNS ноды) — service-DNS не нужен; если нужен — `ClusterFirstWithHostNet`, но тогда требуется pod-сеть до CoreDNS (на агентах без рабочего оверлея не заработает).

---

## 6. Поставка манифестов и образов в MAIN-образе

### 6.1 Автодеплой [V, docs.k3s.io/installation/packaged-components + add-ons/helm]

- `/var/lib/rancher/k3s/server/manifests/*.yaml` применяются как `kubectl apply` при старте и при изменении файла; удаление файла **не** удаляет ресурсы; каждый файл = `AddOn` CR в `kube-system`; имя AddOn = basename (уникально, без `_`); можно несколько ресурсов через `---`; `*.skip` отключает файл; `--disable X` удаляет компонент и **файлы манифестов**; пакетные (coredns, traefik, local-storage, metrics-server) перезаписываются при каждом старте. Порядок применения в docs не указан (не полагаться на алфавитный).
- HelmChart CRD (`helm.cattle.io/v1`): `spec.chart`, `spec.repo`, `spec.version`, `spec.targetNamespace`, `spec.createNamespace`, `spec.valuesContent`, `spec.set`, `spec.chartContent` (base64 tgz для оффлайна), `spec.failurePolicy` (default `reinstall`), `backOffLimit` 1000, `timeout` 300s; `HelmChartConfig` для оверрайда пакетных чартов. Статика `/var/lib/rancher/k3s/server/static/` доступна внутри кластера через API (`%{KUBERNETES_API}%`).
- **Ловушка VOLUME**: `/var/lib/rancher/k3s` — volume. Файлы, добавленные `COPY` в образ, попадают в named volume только при **первом** создании пустого volume (copy-up); при обновлении образа с новым volume-содержимым старый volume их не обновит. [UNVERIFIED: точное поведение BuildKit с VOLUME-инструкцией из базы; k3d кладёт манифесты именно туда и это работает на свежих volumes.] Надёжный способ: хранить манифесты в `/opt/llm/manifests/` образа, а entrypoint при каждом старте делает `cp -f /opt/llm/manifests/*.yaml /var/lib/rancher/k3s/server/manifests/` (mtime обновится → deploy-controller переприменит; см. фикс #14568 для epoch-mtime — копировать с `touch`).
- Для нескольких серверов файлы надо синхронизировать вручную [V] — у нас один сервер.

### 6.2 Airgap-образы [V, docs.k3s.io/installation/airgap]

- `/var/lib/rancher/k3s/agent/images/` — tar/tar.zst импортируются на **каждом старте** (kubelet не стартует, пока не обработаны все); пустой файл `.cache.json` в каталоге включает пропуск неизменённых архивов (по size+mtime); при кеше удалённые GC образы не восстанавливаются (лечение: `ctr image import`, `touch`, очистка `.cache.json`).
- k3s-системные образы (pause, coredns, flannel и т.п.) в `k3s-airgap-images-amd64.tar.zst` — нужны только если у нод нет доступа к docker.io.
- Класть vLLM-образ (~10 ГБ) прямо в NODE-образ **не рекомендуется** (образ раздувается, каждый pull NODE = 10 ГБ+).

### 6.3 Embedded registry mirror (spegel) [V, docs.k3s.io/installation/registry-mirror]

- Включение: `--embedded-registry` (или `embedded-registry: true` в config.yaml) на **серверах**; на **каждой** ноде (включая агентов) в `/etc/rancher/k3s/registries.yaml`:
  ```yaml
  mirrors:
    docker.io:
    "*":        # или перечислить реестры; "*" в кавычках
  ```
- Каждая нода поднимает локальный OCI-registry на 6443 и P2P на 5001; **"All nodes must reach each other by internal IP on TCP 5001 and 6443"**. Если не достучаться — pull медленнее, т.к. сначала пробуется распределённый реестр, потом fallback на upstream.
- Ограничения: тег `latest` не раздаётся (`K3S_P2P_ENABLE_LATEST=true` unsupported); tag poisoning — использовать digest; read-only; доверие равных нод; образы из airgap/pre-import пиннятся и раздаются; `ctr -n k8s.io image import` для ручной загрузки.
- **Для нашей топологии spegel работает плохо** [вывод]: внутренние IP нод в bridge/NAT-режиме — адреса контейнеров/WSL-VM, недостижимые из другой подсети; в prod (15 машин в одной подсети, но Docker Desktop NAT) пиры друг до друга по internal IP не дотянутся → каждая нода всё равно идёт в интернет. Работает только при `--network host`/mirrored и открытых 5001/6443 между машинами класса.
- Предпочтительный вариант: **pull-through кеш на MAIN** (`registry:2` с `REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io`, один upstream на инстанс) + на нодах (`COPY registries.yaml /etc/rancher/k3s/registries.yaml` в NODE-образ):
  ```yaml
  mirrors:
    docker.io:
      endpoint: ["http://<main-ip>:5000"]
  ```
  (по умолчанию containerd после зеркала откатывается на upstream; формат `registries.yaml` с `endpoint:` подтверждён docs registry-mirror [V]; детали `configs:` для insecure-HTTP — [UNVERIFIED], см. страницу Private Registry Configuration). Второй вариант — предзагрузка tar в volume `/var/lib/rancher/k3s/agent/images/` (флешка/SMB).

---

## 7. Чистое удаление агента на Linux-хосте с другими контейнерами

### 7.1 Режим bridge + `-p` (рекомендуется для общего Docker-хоста)

Весь netns k3s (cni0, flannel.1, veth, kube-proxy iptables, ipvs, routes) живёт в netns контейнера и исчезает вместе с ним. Mount-namespace тоже контейнерный (`/var/lib/kubelet/pods/*`, `/run/netns/cni-*`, `/run/k3s`), shim'ы containerd — в PID-ns контейнера → убиваются при остановке.

```bash
docker rm -f llm-node
docker volume rm llm-node-k3s llm-node-kubelet     # иначе останутся containerd-образы/контент (десятки ГБ) и node-password
# на MAIN:
kubectl delete node ubuntu200                      # убирает Node и secret с node-password [V docs uninstall]
```
Остаточное состояние хоста (не удаляется, безвредно): загруженные модули ядра (`br_netfilter`, `overlay`, `vxlan`, `nf_conntrack`, `ip_vs*` и др. — k3s в privileged-контейнере делает modprobe в ядро хоста) [UNVERIFIED список]; глобальные sysctl, если какие-то не namespaced (`net.bridge.bridge-nf-call-iptables`, `net.ipv4.ip_forward`) — [UNVERIFIED, namespaced или нет зависит от ядра]; Docker сам убирает свои DNAT-правила `-p` при `rm`. Проверка: `ip link`, `iptables-save | grep -E 'KUBE-|CNI-|FLANNEL'` — должны быть пусты.

### 7.2 Режим `--network host` (на shared Docker-хосте — не рекомендуется)

Остаются в netns хоста: интерфейсы `cni0`, `flannel.1`, `flannel-v6.1`, `kube-ipvs0`, `flannel-wg`, `flannel-wg-v6`, veth'ы подов, маршруты на pod-CIDR, цепочки iptables `KUBE-*`, `CNI-*`, `FLANNEL-*` (и `KUBE-ROUTER-*` + ipsets при включённой network policy), conntrack-записи. Официальный перечень — из `k3s-killall.sh` (install.sh k3s-io/k3s, прочитан целиком [V]):

```bash
ip link show | grep 'master cni0' | while read ignore iface ignore; do iface=${iface%%@*}; [ -z "$iface" ] || ip link delete $iface; done
ip link delete cni0; ip link delete flannel.1; ip link delete flannel-v6.1
ip link delete kube-ipvs0; ip link delete flannel-wg; ip link delete flannel-wg-v6
ip netns show 2>/dev/null | grep cni- | xargs -r -n 1 ip netns delete
rm -rf /var/lib/cni/
iptables-save  | grep -v KUBE- | grep -v CNI- | grep -iv flannel | iptables-restore
ip6tables-save | grep -v KUBE- | grep -v CNI- | grep -iv flannel | ip6tables-restore
# + umount /var/lib/kubelet/pods/*, /var/lib/kubelet/plugins/*, /run/k3s/*, /run/netns/cni-* (если смонтированы в хост-ns)
```
Предостережения для хоста с Docker:

- Перед `iptables-restore` сделать `iptables-save > /root/iptables.bak`: restore заменяет таблицы целиком → гонка с Docker (старт/стоп контейнеров во время выполнения) может потерять его правила; делать в тихий момент. Фильтр `grep -iv flannel`/`KUBE-` заденет и чужие правила, содержащие эти подстроки (комментарии, имена) — реальная, хоть и малая, вероятность. Свидетельство небезопасности слепой фильтрации — k3s Slack ("install script's iptables filtering breaks networking of the instance") [V поиск, анекдот].
- Если контейнер убит, а интерфейсы остались — `docker run --rm --privileged --network host` выполнить скрипт выше из временного контейнера либо руками с хоста.
- `docker restart` Docker'а пересоздаст свои цепочки, но не удалит k3s-овские.

### 7.3 Ломает ли k3s-agent с host-сетью Docker хоста?

Прямых подтверждений "ломает" в сессии не найдено; риски [частично V, частично вывод]:

1. **Конфликты портов** на хосте: kubelet 10250 (+10248), kube-proxy 10249/10256, локальный LB агента `127.0.0.1:6444` (`--lb-server-port` [V]), flannel VXLAN UDP 8472, cni — проверить `ss -lntup` до запуска; наши занятые 80/443/8000/8080 k3s не использует, но hostNetwork-поды инференса должны брать другие порты (vLLM default 8000 занят!).
2. **iptables-бэкенд**: k3s в контейнере использует свои бинари iptables (wrapper выбирает legacy/nft по детекту хоста); смесь legacy и nft на одном хосте — известный источник "пропавших" правил/сети (k3s discussion #9852: `iptables -P FORWARD ACCEPT` как обход после смены на nft) [V поиск]. Ubuntu 24.04 по умолчанию iptables-nft; Docker 29 по умолчанию использует iptables (через nft-shim); экспериментальный `--firewall-backend=nftables` в Docker 29 [V docs.docker.com/linuxiac]: если он включён на demo-сервере — совместимость с kube-proxy/flannel **[UNVERIFIED]**. Проверить: `docker info | grep -i firewall`, `iptables --version`.
3. **FORWARD policy**: Docker ставит `FORWARD DROP` (flannel troubleshooting) [V поиск]; flannel/k3s добавляют ACCEPT-правила для pod-CIDR (`FLANNEL-FWD`), так что ломаться должен pod-трафик, а не docker0; при наличии ufw/firewalld с DROP-политикой pod-трафик ломается (k3s discussion #9852/issue #977) [V поиск].
4. `br_netfilter` + `bridge-nf-call-iptables=1` в глобальном netns влияют на трафик через `docker0` (Docker обычно и так это включает) [частично V поиск, не официально].
5. Маршруты pod-CIDR добавляются в таблицу хоста: поэтому важен непересекающийся `100.64.0.0/16`.
6. Привилегии: privileged + host-сеть + доступ к /dev = эффективно root на хосте.

**Вывод**: на хосте, где живёт чужое (демо-сервер 192.168.3.200), использовать bridge+`-p`; host-сеть оставить для WSL2-VM (изолирована, одноцелевая).

---

## 8. Реестр рисков (топ)

1. **GPU внутри k3s-in-Docker на WSL2 — нет документированного working-рецепта** (OpenShell-прецедент не смёржен/"untested"). Компоненты по отдельности есть (toolkit 1.20.1 JIT-CDI WSL, device-plugin ≥0.19.1), цепочка не проверена. Митигация: smoke-лестница §3.6; фолбэк 1 — NVIDIA_VISIBLE_DEVICES без device-plugin; фолбэк 2 — k3s-agent нативно в WSL2 (без Docker-слоя) или CPU-инференс.
2. **Сеть**: Docker Desktop/WSL2-NAT ломает flannel между подсетями и `InternalIP`. Митигация: hostNetwork + ExternalIP + published ports; не полагаться на оверлей; Docker CE в WSL2 + mirrored для prod.
3. **k3s не PID 1 → падает kubelet на cgroup v2** (MAIN с gateway в одном контейнере). Митигация: свой evacuation в entrypoint или k3s как PID 1.
4. **Windows/Hyper-V firewall**: 6443 (MAIN inbound) и порты инференса (NODE inbound) молча режутся; mirrored-mode требует Win11 22H2+.
5. **10 ГБ образ на 15 нодах**: spegel не сработает при NAT/разных подсетях; нужен pull-through registry на MAIN или предзагрузка; диск/ DiskPressure.
6. **Общий Docker-хост**: host-сеть k3s оставляет интерфейсы/iptables; смесь legacy/nft; Docker 29 nftables-режим не проверен. Митигация: bridge + `-p`, диапазон портов вне 80/443/8000/8080.
7. **CIDR**: 100.64/10 может конфликтовать с Tailscale/CGNAT; менять нельзя после первого старта.
8. **Node-password / CA**: пересоздание контейнера без volume при том же `--node-name` → отказ в регистрации; пересоздание MAIN-volume требует чистки всех агентов.
9. **Версии**: toolkit 1.19.1 — регрессия на WSL2; k3s 1.36.5 — Traefik chart-breaking (мы отключили traefik).

## 9. Реестр [UNVERIFIED]

- Вложенный `--gpus all` + `--privileged` на Docker Desktop (инъекция `/dev/dxg` и `/usr/lib/wsl/lib` во внешний контейнер); fallback через `--device`/`-v`.
- Работа `nvidia-container-runtime` (jit-cdi, WSL-платформа) во вложенном контейнере.
- Поведение агентского LB при недостижимом advertise-адресе; необходимость `--tls-san` при публикации через Docker Desktop (практически — да).
- kubelet отвергает `--node-ip` не из локальных интерфейсов; NotReady при `--flannel-backend=none` без CNI.
- Работа VXLAN/WireGuard flannel через опубликованные UDP-порты Docker NAT.
- VOLUME copy-up поведение для манифестов, добавленных в производный образ.
- `config-v3.toml.tmpl` для containerd 2.x в k3s; формат `registries.yaml` `configs:` для HTTP-реестра.
- Совместимость Docker 29 `firewall-backend=nftables` с k3s kube-proxy/flannel на одном хосте.
- Namespaced ли `net.bridge.*` sysctl; список автозагружаемых модулей.
- Наличие правила Windows Firewall, создаваемого Docker Desktop для опубликованных портов.
- device-plugin v0.20.1 содержит WSL-фикс (хронологически должен).
- vLLM на WSL2 с 12 ГБ (pinned memory/Unified Memory ограничения).
- Последовательность команд cgroup-эвакуации в самописном entrypoint.

## 10. Источники

- k3s: https://docs.k3s.io/installation/requirements , /networking/basic-network-options , /networking/distributed-multicloud , /cli/server , /cli/agent , /advanced , /installation/registry-mirror , /installation/airgap , /installation/packaged-components , /add-ons/helm , /architecture , /installation/uninstall , /faq
- k3s каналы: https://update.k3s.io/v1-release/channels ; релиз v1.36.5+k3s1: https://github.com/k3s-io/k3s/releases/tag/v1.36.5%2Bk3s1 ; Docker Hub `rancher/k3s`
- k3s исходники: `docker-compose.yml`, `package/Dockerfile`, `install.sh` (killall), `pkg/cli/cmds/init_linux.go`, `pkg/cli/server/server.go`, `pkg/agent/containerd/runtimes.go`
- k3s issues: #4873 (k3s container on Docker Desktop cgroup v2), #10094 (embedded registry bind-address), discussion #9852 (iptables nft/FORWARD), #977
- k3d CUDA: https://k3d.io/stable/usage/advanced/cuda/
- NVIDIA: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html ; https://docs.nvidia.com/cuda/wsl-user-guide/index.html ; https://github.com/NVIDIA/k8s-device-plugin/issues/332 , /pull/1671 , releases v0.19.1–v0.20.1 ; https://github.com/NVIDIA/nvidia-container-toolkit (releases v1.20.0/1.20.1, issue #1864, PR #1890, `internal/info/auto.go`, issue #1739 для libdxcore); NVIDIA/OpenShell PR #441, #608
- Docker: https://docs.docker.com/desktop/features/gpu/ , https://docs.docker.com/engine/network/drivers/host/ ; Docker Engine 29 nftables: https://www.docker.com/blog/docker-engine-version-29/
- Microsoft: https://learn.microsoft.com/en-us/windows/wsl/networking (mirrored mode, Hyper-V firewall)
- Сторонние: https://inerplat.github.io/wirekube/guides/wsl2-gpu-node ; форум Docker "Host networking not working on Docker Desktop in WSL2 with mirrored mode" (147994)
