#!/usr/bin/env bash
# Настраивает дистрибутив WSL под ноду NSU LLM. Запускает install-node.ps1 от root внутри дистрибутива:
#   wsl -d NSU-LLM -u root -e env ACCEL=cpu|nvidia bash /mnt/c/.../wsl-setup.sh
# Идемпотентен: systemd в /etc/wsl.conf, Docker CE из официального репозитория Docker (без Docker Desktop),
# для nvidia — NVIDIA Container Toolkit 1.20.1 (1.19.1 падает на WSL2) и runtime nvidia в Docker.
# PROXY=http://host:port (пусто — без прокси): apt, curl и демон Docker идут через него, NO_PROXY_LIST — исключения.
# install-node.ps1 передаёт их через WSLENV, а не в командной строке; пароль прокси в вывод не попадает.
# Файлы с прокси (apt.conf.d, drop-in Docker) доступны на чтение только root.
# Последняя строка вывода: NSU_LLM_RESTART=1, если дистрибутив нужно перезапустить (включён systemd).
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
  nvidia-ctk runtime configure --runtime=docker >/dev/null || die "nvidia-ctk runtime configure --runtime=docker завершился ошибкой"
  [ "$restart" = 1 ] || systemctl restart docker || die "Docker не перезапустился после настройки runtime nvidia: journalctl -u docker -n 50"
fi

echo "NSU_LLM_RESTART=$restart"
