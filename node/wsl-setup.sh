#!/usr/bin/env bash
# Настраивает дистрибутив WSL под ноду NSU LLM. Запускает install-node.ps1 от root внутри дистрибутива:
#   wsl -d NSU-LLM -u root -e env ACCEL=cpu|nvidia bash /mnt/c/.../wsl-setup.sh
# Идемпотентен: systemd в /etc/wsl.conf, Docker CE из официального репозитория Docker (без Docker Desktop),
# для nvidia — NVIDIA Container Toolkit 1.20.1 (1.19.1 падает на WSL2) и runtime nvidia в Docker.
# PROXY=http://host:port (пусто — без прокси): apt, curl и демон Docker идут через него, NO_PROXY_LIST — исключения.
# install-node.ps1 передаёт их через WSLENV, а не в командной строке; пароль прокси в вывод не попадает.
# Файлы с прокси (apt.conf.d, drop-in Docker) доступны на чтение только root.
# Последняя строка вывода: NSU_LLM_RESTART=1, если дистрибутив нужно перезапустить (включён systemd).
# HOST_MOUNT=/mnt/c (диск Windows, на котором лежит диск дистрибутива): раз в минуту его свободное место пишется в state ноды (шаг 4).
# ROOT — префикс путей для тестов.
set -euo pipefail
ROOT="${ROOT:-}"
ACCEL="${ACCEL:-cpu}"
CTK="${NVIDIA_CTK_VERSION:-1.20.1-1}"
restart=0
say() { echo "wsl-setup: $*"; }
die() { echo "wsl-setup: ОШИБКА: $*" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
PROXY="${PROXY:-}"
NO_PROXY_LIST="${NO_PROXY_LIST:-}"
APT_PROXY="$ROOT/etc/apt/apt.conf.d/95nsu-llm-proxy"
DOCKER_PROXY="$ROOT/etc/systemd/system/docker.service.d/nsu-llm-proxy.conf"
docker_restart=0

# 0. Прокси: apt и curl (этот запуск), файл для apt и drop-in демона Docker; без PROXY файлы убираются.
if [ -n "$PROXY" ]; then
  export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
  export no_proxy="$NO_PROXY_LIST" NO_PROXY="$NO_PROXY_LIST"
  mkdir -p "$(dirname "$APT_PROXY")" "$(dirname "$DOCKER_PROXY")"
  ( umask 077; printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$PROXY" "$PROXY" > "$APT_PROXY" )
  # systemd раскрывает %-спецификаторы: в drop-in % удваивается (пароль прокси в URL-кодировке содержит %).
  sp="${PROXY//%/%%}"; snp="${NO_PROXY_LIST//%/%%}"
  want="$(printf '[Service]\nEnvironment=HTTP_PROXY=%s\nEnvironment=HTTPS_PROXY=%s\nEnvironment=NO_PROXY=%s\n' "$sp" "$sp" "$snp")"
  if [ "$(cat "$DOCKER_PROXY" 2>/dev/null)" != "$want" ]; then
    ( umask 077; printf '%s\n' "$want" > "$DOCKER_PROXY" ); docker_restart=1
    say "прокси $(printf '%s' "$PROXY" | sed -E 's#^(https?://[^:@/]+):[^@/]+@#\1:***@#') для apt и Docker"
  fi
else
  rm -f "$APT_PROXY"
  if [ -e "$DOCKER_PROXY" ]; then rm -f "$DOCKER_PROXY"; docker_restart=1; say "прокси для Docker убран"; fi
fi

# 1. systemd (Docker стартует как служба при запуске дистрибутива).
conf="$ROOT/etc/wsl.conf"
touch "$conf"
if ! grep -qx 'systemd=true' "$conf"; then
  sed -i '/^systemd[[:space:]]*=/d' "$conf"
  if grep -qx '\[boot\]' "$conf"; then sed -i 's/^\[boot\]$/[boot]\nsystemd=true/' "$conf"
  elif [ -s "$conf" ]; then printf '\n[boot]\nsystemd=true\n' >> "$conf"
  else printf '[boot]\nsystemd=true\n' >> "$conf"; fi
  say "включён systemd в /etc/wsl.conf"
  restart=1
fi
[ "$(cat "$ROOT/proc/1/comm" 2>/dev/null)" = systemd ] || restart=1

# 2. Docker CE.
# Хранилище образов: Docker CE 29+ на новой установке берёт containerd image store, а он держит ещё и сжатые слои (blobs) рядом с распакованными,
# образ ноды (несколько ГБ) занимает диск дважды. features.containerd-snapshotter=false в daemon.json возвращает классическое хранилище overlay2
# (только распакованные слои). Пишется до того, как Docker впервые скачает образ. Если Docker уже стоит и в хранилище есть образы, настройка не
# меняется: смена хранилища спрятала бы их (образы: docker image ls; тогда переустановите дистрибутив или загрузите образ заново).
DAEMON_JSON="$ROOT/etc/docker/daemon.json"
classic_store() {
  grep -Eq '"containerd-snapshotter"[[:space:]]*:[[:space:]]*false' "$DAEMON_JSON" 2>/dev/null && return 0
  if [ -x "$ROOT/usr/bin/docker" ]; then
    # Docker уже стоит: переключать хранилище можно, только убедившись, что в нём нет образов. Демон не отвечает (только что запущен, systemd поднимается) —
    # узнать это нельзя, настройка откладывается до следующего запуска установщика.
    if ! docker info >/dev/null 2>&1; then
      say "ВНИМАНИЕ: демон Docker не отвечает, наличие образов неизвестно: containerd-snapshotter=false не записан. Запустите установку ещё раз, когда Docker заработает."
      return 0
    fi
    if [ -n "$(docker image ls -q 2>/dev/null || true)" ]; then
      say "ВНИМАНИЕ: в Docker уже есть образы в хранилище containerd: настройка containerd-snapshotter=false не применена (образы станут невидимы). Удалите образы (docker image prune -a) и запустите установку снова."
      return 0
    fi
  fi
  mkdir -p "$(dirname "$DAEMON_JSON")"
  if [ ! -s "$DAEMON_JSON" ]; then
    printf '{
  "features": {
    "containerd-snapshotter": false
  }
}
' > "$DAEMON_JSON"
  elif command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import json, sys; d = json.load(sys.stdin); d.setdefault("features", {})["containerd-snapshotter"] = False; json.dump(d, sys.stdout, indent=2); print()' < "$DAEMON_JSON" > "$DAEMON_JSON.new" 2>/dev/null; then
      mv -f "$DAEMON_JSON.new" "$DAEMON_JSON"
    else
      rm -f "$DAEMON_JSON.new"; say "ВНИМАНИЕ: $DAEMON_JSON не разобран, containerd-snapshotter=false не записан"; return 0
    fi
  else
    say "ВНИМАНИЕ: в $DAEMON_JSON уже есть настройки, а python3 для слияния нет: добавьте \"features\": {\"containerd-snapshotter\": false} вручную"
    return 0
  fi
  say "Docker: классическое хранилище образов (containerd-snapshotter=false), слои не хранятся дважды"
  [ -x "$ROOT/usr/bin/docker" ] && docker_restart=1   # демон уже работает: перечитает daemon.json после перезапуска
  return 0
}
classic_store
if [ ! -x "$ROOT/usr/bin/docker" ]; then
  say "установка Docker CE из download.docker.com"
  apt_fail() { die "не удалось установить Docker CE: нет доступа к download.docker.com или к зеркалу Ubuntu. Проверьте интернет и прокси, затем запустите install-node.ps1 снова."; }
  apt-get update -q || apt_fail
  apt-get install -y ca-certificates curl gnupg || apt_fail
  install -m 0755 -d "$ROOT/etc/apt/keyrings" "$ROOT/etc/apt/sources.list.d"
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$ROOT/etc/apt/keyrings/docker.asc" || apt_fail
  codename="$(. "$ROOT/etc/os-release" && echo "${VERSION_CODENAME:-noble}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    > "$ROOT/etc/apt/sources.list.d/docker.list"
  apt-get update -q || apt_fail
  apt-get install -y docker-ce docker-ce-cli containerd.io || apt_fail
fi
systemctl enable docker >/dev/null 2>&1 || true
# Новый drop-in прокси применяется перезапуском демона (если дистрибутив и так перезапускается, это сделает systemd).
if [ "$docker_restart" = 1 ] && [ "$restart" = 0 ]; then
  systemctl daemon-reload || true
  systemctl restart docker || die "Docker не перезапустился после настройки прокси: journalctl -u docker -n 50"
fi

# 3. NVIDIA Container Toolkit (только для GPU-ноды), версия закреплена.
if [ "$ACCEL" = nvidia ]; then
  if [ "$(dpkg-query -W -f='${Version}' nvidia-container-toolkit 2>/dev/null || true)" != "$CTK" ]; then
    say "установка NVIDIA Container Toolkit $CTK"
    ctk_fail() { die "не удалось установить NVIDIA Container Toolkit $CTK: нет доступа к nvidia.github.io. Проверьте интернет, затем запустите install-node.ps1 снова (или -Accel cpu)."; }
    install -m 0755 -d "$ROOT/usr/share/keyrings" "$ROOT/etc/apt/sources.list.d"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --batch --yes --dearmor -o "$ROOT/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg" || ctk_fail
    echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/\$(ARCH) /" \
      > "$ROOT/etc/apt/sources.list.d/nvidia-container-toolkit.list"
    apt-get update -q || ctk_fail
    apt-get install -y --allow-downgrades nvidia-container-toolkit="$CTK" nvidia-container-toolkit-base="$CTK" \
      libnvidia-container-tools="$CTK" libnvidia-container1="$CTK" || ctk_fail
  fi
  daemon_was="$(cksum < "$DAEMON_JSON" 2>/dev/null || true)"
  nvidia-ctk runtime configure --runtime=docker >/dev/null || die "nvidia-ctk runtime configure --runtime=docker завершился ошибкой"
  # nvidia-ctk переписывает daemon.json: ключ классического хранилища образов должен остаться (иначе образ снова хранится дважды)
  if ! grep -Eq '"containerd-snapshotter"[[:space:]]*:[[:space:]]*false' "$DAEMON_JSON" 2>/dev/null; then
    say "ВНИМАНИЕ: после nvidia-ctk в $DAEMON_JSON пропал ключ features.containerd-snapshotter=false, записываю его снова"
    classic_store
  fi
  # Перезапуск только при новых настройках: повторный запуск установщика на работающей ноде иначе ронял бы её контейнер с моделью.
  if [ "$restart" = 0 ] && { [ "$daemon_was" != "$(cksum < "$DAEMON_JSON" 2>/dev/null || true)" ] || ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; }; then
    systemctl restart docker || die "Docker не перезапустился после настройки runtime nvidia: journalctl -u docker -n 50"
  fi
fi

# 4. Свободное место диска Windows. Контейнер ноды его не видит: df внутри WSL показывает виртуальный диск (до 1 ТБ), а не диск Windows, который
# заполняет ext4.vhdx. Служба раз в минуту пишет df /mnt/<диск> в <state ноды>/host-disk (JSON: mount, free_bytes, total_bytes, at), node-metrics.sh
# отправляет free_bytes порталу как disk_free_bytes (панель «Ноды» предупреждает о малом месте). Каталога state ещё нет (ноду не запускали) — запись пропускается.
HOST_MOUNT="${HOST_MOUNT:-/mnt/c}"
case "$HOST_MOUNT" in /mnt/[a-z]) ;; *) HOST_MOUNT=/mnt/c ;; esac
HOST_DISK_STATE="${HOST_DISK_STATE:-/var/lib/nsu-llm-node/state/host-disk}"
hd_bin="$ROOT/usr/local/sbin/nsu-llm-host-disk"
hd_dir="$ROOT/etc/systemd/system"
hd_script="$(cat <<EOS
#!/bin/sh
# NSU LLM: свободное место диска Windows ($HOST_MOUNT) для отчёта ноды; запускает nsu-llm-host-disk.timer (wsl-setup.sh)
out='$HOST_DISK_STATE'
[ -d "\$(dirname "\$out")" ] || exit 0
set -- \$(df -Pk '$HOST_MOUNT' 2>/dev/null | awk 'NR == 2 { print \$4 * 1024, \$2 * 1024 }')
[ "\$#" = 2 ] || exit 0
printf '{"mount":"%s","free_bytes":%s,"total_bytes":%s,"at":%s}\\n' '$HOST_MOUNT' "\$1" "\$2" "\$(date +%s)" > "\$out.tmp" && mv -f "\$out.tmp" "\$out"
EOS
)"
hd_service='[Unit]
Description=NSU LLM: free space of the Windows disk for the node report

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nsu-llm-host-disk'
hd_timer='[Unit]
Description=NSU LLM: free space of the Windows disk, every minute

[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=10s

[Install]
WantedBy=timers.target'
hd_changed=0
mkdir -p "$(dirname "$hd_bin")" "$hd_dir"
if [ "$(cat "$hd_bin" 2>/dev/null)" != "$hd_script" ]; then printf '%s\n' "$hd_script" > "$hd_bin"; chmod 0755 "$hd_bin"; hd_changed=1; fi
if [ "$(cat "$hd_dir/nsu-llm-host-disk.service" 2>/dev/null)" != "$hd_service" ]; then printf '%s\n' "$hd_service" > "$hd_dir/nsu-llm-host-disk.service"; hd_changed=1; fi
if [ "$(cat "$hd_dir/nsu-llm-host-disk.timer" 2>/dev/null)" != "$hd_timer" ]; then printf '%s\n' "$hd_timer" > "$hd_dir/nsu-llm-host-disk.timer"; hd_changed=1; fi
systemctl enable nsu-llm-host-disk.timer >/dev/null 2>&1 || true
if [ "$hd_changed" = 1 ] && [ "$(cat "$ROOT/proc/1/comm" 2>/dev/null)" = systemd ]; then
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl restart nsu-llm-host-disk.timer >/dev/null 2>&1 || true
  say "служба nsu-llm-host-disk: свободное место $HOST_MOUNT раз в минуту в $HOST_DISK_STATE"
fi

# Проверка: хранилище, которое выбрал Docker, классическое (Driver overlay2). С containerd store `docker info` показывает overlayfs.
if [ -z "$ROOT" ] && command -v docker >/dev/null 2>&1; then
  sd="$(docker info --format '{{.Driver}}' 2>/dev/null || true)"
  if [ -n "$sd" ] && [ "$sd" != overlay2 ]; then say "ВНИМАНИЕ: хранилище образов Docker «$sd», а не overlay2: образы хранятся дважды (см. /etc/docker/daemon.json, features.containerd-snapshotter)"; fi
fi

echo "NSU_LLM_RESTART=$restart"
