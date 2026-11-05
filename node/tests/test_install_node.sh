#!/usr/bin/env bash
# Offline tests for the unattended Windows node installer (install-node.ps1, node-boot.ps1, wsl-setup.sh).
# No real WSL, Docker, firewall, scheduled task or power change: system-changing steps run with -DryRun,
# wsl / powercfg / nvidia-smi are fake commands first in PATH. Needs Windows PowerShell for the .ps1 part.
export MSYS_NO_PATHCONV=1
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# The test tree gets a clean protected ACL (SYSTEM, Administrators, this user): a TEMP that other accounts can write to
# would make every install dir under it untrusted for install-node.ps1 (that is what its trust check is for).
if command -v icacls >/dev/null 2>&1; then
  ME_SID="$(/c/Windows/System32/whoami.exe /user /fo csv /nh 2>/dev/null | tr -d '\r"' | awk -F, '{print $2}')"
  [ -n "$ME_SID" ] && icacls "$(cygpath -w "$TMP")" /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*${ME_SID}:(OI)(CI)F" >/dev/null
fi
FAILS=0

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILS=$((FAILS + 1)); }
before() { local s="${1%%"$2"*}"; echo ${#s}; }   # position of the first occurrence (length of the text before it)
contains() { # name haystack needle
  case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 (missing: $3)"; echo "$2" | sed 's/^/    | /' ;; esac
}
lacks() {
  case "$2" in *"$3"*) fail "$1 (unexpected: $3)"; echo "$2" | sed 's/^/    | /' ;; *) pass "$1" ;; esac
}

# ------------------------------------------------------------------ wsl-setup.sh (inside the distro, as root)
SETUP="$NODE/wsl-setup.sh"
if [ -f "$SETUP" ] && bash -n "$SETUP"; then pass "bash -n wsl-setup.sh"; else fail "bash -n wsl-setup.sh"; fi
mkdir -p "$TMP/sbin"
for c in apt-get curl gpg dpkg systemctl nvidia-ctk; do
  cat > "$TMP/sbin/$c" <<'EOS'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$FAKE_SETUP_LOG"
case "$(basename "$0") $1" in
  "dpkg --print-architecture") echo amd64 ;;
esac
exit 0
EOS
  chmod +x "$TMP/sbin/$c"
done
cat > "$TMP/sbin/dpkg-query" <<'EOS'
#!/usr/bin/env bash
echo "dpkg-query $*" >> "$FAKE_SETUP_LOG"
[ -n "${FAKE_CTK:-}" ] && { printf '%s' "$FAKE_CTK"; exit 0; }
exit 1
EOS
chmod +x "$TMP/sbin/dpkg-query"
newroot() { # fresh fake root: os-release, PID 1 = $1
  rm -rf "$TMP/root"; mkdir -p "$TMP/root/etc" "$TMP/root/proc/1"
  printf 'ID=ubuntu\nVERSION_CODENAME=noble\n' > "$TMP/root/etc/os-release"
  echo "$1" > "$TMP/root/proc/1/comm"
}
setup() { FAKE_SETUP_LOG="$TMP/setup.log" ROOT="$TMP/root" PATH="$TMP/sbin:$PATH" bash "$SETUP" 2>&1; }
if [ -f "$SETUP" ]; then
  newroot init; : > "$TMP/setup.log"
  out="$(ACCEL=cpu setup)"; rc=$?
  [ $rc -eq 0 ] && pass "setup fresh distro succeeds" || { fail "setup fresh distro succeeds"; echo "$out"; }
  contains "setup writes the classic image store before Docker is installed (no second copy of the layers)" "$(cat "$TMP/root/etc/docker/daemon.json" 2>/dev/null)" '"containerd-snapshotter": false'
  contains "setup enables systemd" "$(cat "$TMP/root/etc/wsl.conf" 2>/dev/null)" "systemd=true"
  contains "setup asks for a distro restart when systemd is not PID 1" "$out" "NSU_LLM_RESTART=1"
  log="$(cat "$TMP/setup.log")"
  contains "setup installs docker-ce from the Docker repo" "$log" "apt-get install -y docker-ce docker-ce-cli containerd.io"
  contains "setup docker repo for the distro codename" "$(cat "$TMP/root/etc/apt/sources.list.d/docker.list" 2>/dev/null)" "https://download.docker.com/linux/ubuntu noble stable"
  contains "setup enables docker at boot" "$log" "systemctl enable docker"
  lacks "setup cpu skips the NVIDIA toolkit" "$log" "nvidia-container-toolkit"
  # second run: systemd running, docker present -> nothing to install, no restart
  mkdir -p "$TMP/root/usr/bin"; printf '#!/bin/sh\n' > "$TMP/root/usr/bin/docker"; chmod +x "$TMP/root/usr/bin/docker"
  echo systemd > "$TMP/root/proc/1/comm"; : > "$TMP/setup.log"
  out="$(ACCEL=cpu setup)"
  contains "setup rerun needs no restart" "$out" "NSU_LLM_RESTART=0"
  lacks "setup rerun installs nothing" "$(cat "$TMP/setup.log")" "apt-get install"
  # B3: the free space of the Windows disk is written for the node report (a unit and a timer in the distro)
  hd="$TMP/root/usr/local/sbin/nsu-llm-host-disk"
  contains "setup: the host-disk script reads df of the Windows disk" "$(cat "$hd" 2>/dev/null)" "df -Pk '/mnt/c'"
  contains "setup: the host-disk unit runs the script" "$(cat "$TMP/root/etc/systemd/system/nsu-llm-host-disk.service" 2>/dev/null)" "ExecStart=/usr/local/sbin/nsu-llm-host-disk"
  contains "setup: the host-disk timer fires every minute" "$(cat "$TMP/root/etc/systemd/system/nsu-llm-host-disk.timer" 2>/dev/null)" "OnUnitActiveSec=60"
  contains "setup: the host-disk timer is enabled" "$(cat "$TMP/setup.log")" "systemctl enable nsu-llm-host-disk.timer"
  lacks "setup rerun: the host-disk files are unchanged, the timer is not restarted" "$(cat "$TMP/setup.log")" "systemctl restart nsu-llm-host-disk.timer"
  rm -f "$hd"; : > "$TMP/setup.log"
  out="$(ACCEL=cpu setup)"
  contains "setup: systemd is up and the script was missing (an upgrade): the timer is restarted" "$(cat "$TMP/setup.log")" "systemctl restart nsu-llm-host-disk.timer"
  : > "$TMP/setup.log"
  out="$(ACCEL=cpu setup)"
  lacks "setup rerun: the host-disk files are unchanged, the timer is not restarted" "$(cat "$TMP/setup.log")" "systemctl restart nsu-llm-host-disk.timer"
  mkdir -p "$TMP/hoststate"
  cat > "$TMP/sbin/df" <<'EOS'
#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo 'C:\ 488000000 400000000 88000000 82% /mnt/c'
EOS
  chmod +x "$TMP/sbin/df"
  out="$(HOST_DISK_STATE="$TMP/hoststate/host-disk" ACCEL=cpu setup)"
  PATH="$TMP/sbin:$PATH" sh "$hd" 2>/dev/null
  hdj="$(cat "$TMP/hoststate/host-disk" 2>/dev/null)"
  contains "host-disk script: the free bytes of the Windows disk" "$hdj" '"free_bytes":90112000000'
  contains "host-disk script: the total bytes" "$hdj" '"total_bytes":499712000000'
  contains "host-disk script: the mount and a timestamp" "$hdj" '"mount":"/mnt/c","free_bytes"'
  rm -f "$TMP/hoststate/host-disk"; rmdir "$TMP/hoststate"
  # B3: Docker keeps the classic image store (overlay2) so an image is not stored twice
  cat > "$TMP/sbin/docker" <<'EOS'
#!/bin/sh
# fake docker: FAKE_DOCKER_DOWN=1 the daemon does not answer; FAKE_DOCKER_IMAGES=1 one image is in the store
[ "${FAKE_DOCKER_DOWN:-0}" = 1 ] && exit 1
case "$1 $2" in "image ls") [ "${FAKE_DOCKER_IMAGES:-0}" = 1 ] && echo abc123def456 ;; esac
exit 0
EOS
  chmod +x "$TMP/sbin/docker"
  printf '{"log-driver":"json-file"}\n' > "$TMP/root/etc/docker/daemon.json"
  if command -v python3 >/dev/null 2>&1; then
    out="$(ACCEL=cpu setup)"
    contains "setup merges the image store setting into an existing daemon.json" "$(cat "$TMP/root/etc/docker/daemon.json")" '"containerd-snapshotter": false'
    contains "setup keeps the other daemon.json keys" "$(cat "$TMP/root/etc/docker/daemon.json")" '"log-driver": "json-file"'
  else
    echo "skip - python3 not found, the daemon.json merge test is skipped"
  fi
  rm -f "$TMP/root/etc/docker/daemon.json"
  out="$(FAKE_DOCKER_DOWN=1 ACCEL=cpu setup)"
  [ ! -e "$TMP/root/etc/docker/daemon.json" ] && pass "setup does not touch the image store while the daemon does not answer (images unknown)" || fail "setup does not touch the image store while the daemon does not answer"
  contains "setup says the daemon does not answer" "$out" "демон Docker не отвечает"
  out="$(FAKE_DOCKER_IMAGES=1 ACCEL=cpu setup)"
  [ ! -e "$TMP/root/etc/docker/daemon.json" ] && pass "setup leaves the image store alone when Docker already holds images" || fail "setup leaves the image store alone when Docker already holds images"
  contains "setup says why the image store is not changed" "$out" "в Docker уже есть образы"
  out="$(ACCEL=cpu setup)"
  contains "setup sets the classic store when Docker answers and holds no images" "$(cat "$TMP/root/etc/docker/daemon.json" 2>/dev/null)" '"containerd-snapshotter": false'
  : > "$TMP/setup.log"
  out="$(ACCEL=cpu setup)"
  lacks "setup rerun with the store already classic: no restart for it" "$(cat "$TMP/setup.log")" "systemctl restart docker"
  PATH="$TMP/sbin:$PATH" sh "$hd" 2>/dev/null
  [ ! -e "$TMP/hoststate/host-disk" ] && pass "host-disk script: no state dir yet (the node was never started): nothing is written" || fail "host-disk script: no state dir yet"
  out="$(HOST_MOUNT=/mnt/d ACCEL=cpu setup)"
  contains "setup: HOST_MOUNT=/mnt/d watches the other drive" "$(cat "$hd")" "df -Pk '/mnt/d'"
  out="$(HOST_MOUNT='/mnt/c; rm -rf /' ACCEL=cpu setup)"
  contains "setup: a malformed HOST_MOUNT falls back to /mnt/c (it is baked into a script)" "$(cat "$hd")" "df -Pk '/mnt/c'"
  lacks "setup: and is never written into the script" "$(cat "$hd")" "rm -rf"
  [ "$(grep -c '^systemd=true' "$TMP/root/etc/wsl.conf")" = 1 ] && pass "setup keeps one systemd line" || fail "setup keeps one systemd line"
  # an existing wsl.conf with systemd=false is fixed in place, other settings kept
  printf '[boot]\nsystemd=false\n[network]\nhostname=keepme\n' > "$TMP/root/etc/wsl.conf"
  out="$(ACCEL=cpu setup)"
  conf="$(cat "$TMP/root/etc/wsl.conf")"
  contains "setup flips systemd=false" "$conf" "systemd=true"
  lacks "setup leaves no systemd=false" "$conf" "systemd=false"
  contains "setup keeps other wsl.conf keys" "$conf" "hostname=keepme"
  contains "setup restart after wsl.conf change" "$out" "NSU_LLM_RESTART=1"
  # nvidia: pinned toolkit 1.20.1-1, docker runtime configured
  : > "$TMP/setup.log"
  out="$(ACCEL=nvidia setup)"
  log="$(cat "$TMP/setup.log")"
  contains "setup nvidia installs the pinned toolkit" "$log" "nvidia-container-toolkit=1.20.1-1 nvidia-container-toolkit-base=1.20.1-1 libnvidia-container-tools=1.20.1-1 libnvidia-container1=1.20.1-1"
  contains "setup nvidia configures the docker runtime" "$log" "nvidia-ctk runtime configure --runtime=docker"
  contains "setup nvidia restarts docker" "$log" "systemctl restart docker"
  out="$(ACCEL=nvidia setup)"
  lacks "setup nvidia: a toolkit that keeps daemon.json intact gives no image store warning" "$out" "пропал ключ"
  # a rerun on a working node: same daemon.json, the daemon already knows the runtime: Docker (and the node with its model) stays up
  cp "$TMP/sbin/docker" "$TMP/docker.keep"
  cat > "$TMP/sbin/docker" <<'EOS'
#!/bin/sh
case "$1" in info) echo '{"nvidia":{"path":"nvidia-container-runtime"},"runc":{"path":"runc"}}' ;; esac
exit 0
EOS
  : > "$TMP/setup.log"
  out="$(FAKE_CTK=1.20.1-1 ACCEL=nvidia setup)"
  lacks "setup nvidia rerun: nothing changed, Docker is not restarted" "$(cat "$TMP/setup.log")" "systemctl restart docker"
  cp "$TMP/docker.keep" "$TMP/sbin/docker"
  : > "$TMP/setup.log"
  out="$(FAKE_CTK=1.20.1-1 ACCEL=nvidia setup)"
  contains "setup nvidia rerun: the daemon does not know the runtime yet: Docker is restarted" "$(cat "$TMP/setup.log")" "systemctl restart docker"
  cat > "$TMP/sbin/nvidia-ctk" <<'EOS'
#!/usr/bin/env bash
echo "nvidia-ctk $*" >> "$FAKE_SETUP_LOG"
printf '{"runtimes":{"nvidia":{"path":"nvidia-container-runtime"}}}\n' > "$ROOT/etc/docker/daemon.json"
exit 0
EOS
  out="$(ACCEL=nvidia setup)"
  contains "setup nvidia: a toolkit that drops the image store key is noticed" "$out" "пропал ключ features.containerd-snapshotter=false"
  contains "setup nvidia: and the key is written again" "$(cat "$TMP/root/etc/docker/daemon.json")" '"containerd-snapshotter": false'
  if command -v python3 >/dev/null 2>&1; then contains "setup nvidia: the toolkit's runtimes key survives the merge" "$(cat "$TMP/root/etc/docker/daemon.json")" '"runtimes"'; fi
  cat > "$TMP/sbin/nvidia-ctk" <<'EOS'
#!/usr/bin/env bash
echo "nvidia-ctk $*" >> "$FAKE_SETUP_LOG"
exit 0
EOS
  : > "$TMP/setup.log"
  out="$(FAKE_CTK=1.20.1-1 ACCEL=nvidia setup)"
  lacks "setup nvidia toolkit already pinned: no reinstall" "$(cat "$TMP/setup.log")" "apt-get install"
  # systemd treats % as a specifier: it is doubled in the drop-in
  out="$(PROXY='http://u:p%40x@proxy.nsu.ru:3128' ACCEL=cpu setup)"
  contains "setup proxy % escaped for systemd" "$(cat "$TMP/root/etc/systemd/system/docker.service.d/nsu-llm-proxy.conf" 2>/dev/null)" "p%%40x"
  lacks "setup proxy password not printed" "$out" "p%40x"
  out="$(ACCEL=cpu setup)"
  # proxy: apt, curl and the Docker daemon get it; dropping it removes the files again
  echo systemd > "$TMP/root/proc/1/comm"; : > "$TMP/setup.log"
  out="$(PROXY=http://proxy.nsu.ru:3128 NO_PROXY_LIST=localhost,10.9.0.5,10.1.2.0/24 ACCEL=cpu setup)"
  contains "setup proxy for apt" "$(cat "$TMP/root/etc/apt/apt.conf.d/95nsu-llm-proxy" 2>/dev/null)" 'Acquire::https::Proxy "http://proxy.nsu.ru:3128";'
  dpx="$(cat "$TMP/root/etc/systemd/system/docker.service.d/nsu-llm-proxy.conf" 2>/dev/null)"
  contains "setup proxy for the docker daemon" "$dpx" "Environment=HTTPS_PROXY=http://proxy.nsu.ru:3128"
  contains "setup docker daemon NO_PROXY" "$dpx" "Environment=NO_PROXY=localhost,10.9.0.5,10.1.2.0/24"
  contains "setup proxy restarts docker" "$(cat "$TMP/setup.log")" "systemctl restart docker"
  : > "$TMP/setup.log"
  out="$(PROXY=http://proxy.nsu.ru:3128 NO_PROXY_LIST=localhost,10.9.0.5,10.1.2.0/24 ACCEL=cpu setup)"
  lacks "setup same proxy: no docker restart" "$(cat "$TMP/setup.log")" "systemctl restart docker"
  out="$(ACCEL=cpu setup)"
  [ ! -e "$TMP/root/etc/apt/apt.conf.d/95nsu-llm-proxy" ] && pass "setup without proxy removes the apt proxy" || fail "setup without proxy removes the apt proxy"
  [ ! -e "$TMP/root/etc/systemd/system/docker.service.d/nsu-llm-proxy.conf" ] && pass "setup without proxy removes the docker proxy" || fail "setup without proxy removes the docker proxy"
  # apt failure explains itself
  printf '#!/usr/bin/env bash\nexit 100\n' > "$TMP/sbin/apt-get"; chmod +x "$TMP/sbin/apt-get"; rm -f "$TMP/root/usr/bin/docker"
  out="$(ACCEL=cpu setup)"; rc=$?
  [ $rc -ne 0 ] && pass "setup apt failure fails" || fail "setup apt failure fails"
  contains "setup apt failure names the repository" "$out" "download.docker.com"
fi

# ------------------------------------------------------------------ PowerShell part
PS="$(command -v powershell || true)"
if [ -z "$PS" ]; then
  echo "skip - Windows PowerShell not found, .ps1 tests skipped"
else
  BASHW="$(cygpath -w "$(command -v bash)")"
  FB="$TMP/fakebin"; mkdir -p "$FB"
  # Every fake command is a .bat that hands its arguments to a bash script (no double quotes in our arguments).
  for c in wsl powercfg nvidia-smi docker curl; do
    printf '@echo off\r\n"%s" "%s" %%*\r\nexit /b %%ERRORLEVEL%%\r\n' "$BASHW" "$(cygpath -w "$FB/$c.sh")" > "$FB/$c.bat"
  done
  cat > "$FB/wsl.sh" <<'EOS'
#!/usr/bin/env bash
# fake wsl.exe; state from FAKE_WSL_VERSION (none = not installed), FAKE_DISTROS, FAKE_RUNNING, FAKE_WSL_GPU, FAKE_IMAGES
echo "wsl $*" >> "$FAKE_LOG"
ver="${FAKE_WSL_VERSION:-2.6.1.0}"
if [ "$ver" = none ]; then echo "Windows Subsystem for Linux is not installed."; exit 1; fi
case "$1" in
  --version) echo "WSL version: $ver"; echo "Kernel version: 6.6.87.2-1"; exit 0 ;;
  -l|--list)
    if [ "$*" = "-l --running -q" ]; then for d in ${FAKE_RUNNING:-}; do echo "$d"; done; else for d in ${FAKE_DISTROS:-}; do echo "$d"; done; fi
    exit 0 ;;
  -d)
    shift; d="$1"; shift
    case " ${FAKE_DISTROS:-} " in *" $d "*) ;; *) echo "There is no distribution with the supplied name."; exit 1 ;; esac
    [ "$1" = -u ] && shift 2
    [ "$1" = -e ] && shift
    case "$*" in
      true) exit 0 ;;
      "/usr/lib/wsl/lib/nvidia-smi -L") [ "${FAKE_WSL_GPU:-0}" = 1 ] && { echo "GPU 0: NVIDIA GeForce RTX 4070 (UUID: GPU-x)"; exit 0; }; exit 9 ;;
      "systemctl is-active docker") [ "${FAKE_DOCKER:-1}" = 1 ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
      "docker image inspect"*) case " ${FAKE_IMAGES:-} " in *" ${*: -1} "*) echo '[]'; exit 0 ;; esac; exit 1 ;;
      "docker images"*) for i in ${FAKE_IMAGES:-}; do echo "$i"; done; exit 0 ;;
      "docker ps"*) echo "${FAKE_CONTAINER:-Up 5 minutes}"; exit 0 ;;
      "cat /proc/1/comm") echo systemd; exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOS
  cat > "$FB/powercfg.sh" <<'EOS'
#!/usr/bin/env bash
echo "powercfg $*" >> "$FAKE_LOG"
if [ "$1" = /query ]; then
  printf '  Minimum Possible Setting: 0x00000000\n  Maximum Possible Setting: 0xffffffff\n'
  printf '  Possible Settings increment: 0x00000001\n  Possible Settings units: Seconds\n'
  printf 'Current AC Power Setting Index: 0x%s\nCurrent DC Power Setting Index: 0x%s\n' "${FAKE_POWER_AC:-00000708}" "${FAKE_POWER_DC:-00000384}"
fi
exit 0
EOS
  cat > "$FB/nvidia-smi.sh" <<'EOS'
#!/usr/bin/env bash
echo "nvidia-smi $*" >> "$FAKE_LOG"
[ -n "${FAKE_WIN_GPU:-}" ] || exit 9
echo "$FAKE_WIN_GPU"
EOS
  cat > "$FB/docker.sh" <<'EOS'
#!/usr/bin/env bash
# fake Windows docker CLI (Docker Desktop); FAKE_DD_ENGINE=0: the engine is not running
echo "docker $*" >> "$FAKE_LOG"
[ "${FAKE_DD_ENGINE:-1}" = 1 ] || { echo "error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified." >&2; exit 1; }
case "$*" in
  info*) echo "Docker Desktop"; exit 0 ;;
  "image inspect"*) case " ${FAKE_IMAGES:-} " in *" ${*: -1} "*) echo '[]'; exit 0 ;; esac; exit 1 ;;
  images*) for i in ${FAKE_IMAGES:-}; do echo "$i"; done; exit 0 ;;
  ps*) echo "${FAKE_CONTAINER:-Up 5 minutes}"; exit 0 ;;
esac
exit 0
EOS
  cat > "$FB/curl.sh" <<'EOS'
#!/usr/bin/env bash
# fake curl.exe for the main probe: FAKE_READYZ ok|503|html|down, FAKE_K3S up|down; prints the HTTP code like -w
echo "curl $*" >> "$FAKE_LOG"
out=''; prev=''; url=''
for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; url="$a"; done
case "$url" in
  *:6443*) [ "${FAKE_K3S:-up}" = up ] && { printf 401; exit 0; }; printf 000; exit 28 ;;
esac
case "$url" in
  */api/node-image) # the public name of the node image: FAKE_NODE_IMAGE is the JSON body (unset: an older main without the endpoint)
    if [ -n "${FAKE_NODE_IMAGE:-}" ]; then [ -n "$out" ] && printf '%s' "$FAKE_NODE_IMAGE" > "$out"; printf 200; else printf 404; fi; exit 0 ;;
esac
case "${FAKE_READYZ:-ok}" in
  ok) [ -n "$out" ] && printf '{"db":true,"litellm":true}' > "$out"; printf 200 ;;
  503) [ -n "$out" ] && printf '{"db":true,"litellm":false}' > "$out"; printf 503 ;;
  html) [ -n "$out" ] && printf '<html>router</html>' > "$out"; printf 200 ;;
  down) printf 000; exit 7 ;;
esac
exit 0
EOS
  chmod +x "$FB"/*.sh
  export NSU_LLM_CURL="$(cygpath -w "$FB/curl.bat")"
  INST="$(cygpath -w "$NODE/install-node.ps1")"
  IDIR="$TMP/inst"
  # base run: dry run, explicit answers, a GPU on Windows and in WSL, the node image present in the distro
  inst() {
    PATH="$FB:$PATH" FAKE_LOG="$(cygpath -w "$TMP/fake.log")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$INST" \
      -InstallDir "$(cygpath -w "$IDIR")" -WslConfigPath "$(cygpath -w "$TMP/wslconfig")" "$@" 2>&1
  }
  full() { inst -DryRun -NonInteractive -Main 10.9.0.5 -Token SECRETTOKEN7 -NodeIp 10.1.2.3 -Name pc01 -MemoryGB 28 -TaskPassword pw "$@"; }
  # hermetic: run-node.ps1 keeps its state (state/labels, ca.crt, airgap) in a temp dir, never in the real C:\ProgramData
  mkdir -p "$TMP/nodedata"; export NSU_LLM_DATA_DIR="$(cygpath -w "$TMP/nodedata")"
  export FAKE_WIN_GPU='NVIDIA GeForce RTX 4070, 576.02, 12282' FAKE_WSL_GPU=1 FAKE_DISTROS='NSU-LLM' FAKE_RUNNING='NSU-LLM' \
    FAKE_IMAGES='nsu-llm-node:0.2.1-gpu nsu-llm-node:0.2.1-cpu'
  # Docker Desktop is detected by its docker-desktop distro or its exe; the dev machine has the exe, so point it away
  export NSU_LLM_DOCKER_DESKTOP_EXE='C:\no\Docker Desktop.exe'
  # a developer may run the tests over ssh: the installer must not take this session for the remote one unless a test says so
  export NSU_LLM_ASSUME_REMOTE=0
  # plenty of free space by default (the real disk of the machine running the tests must not matter); the disk tests set their own
  export NSU_LLM_TEST_FREE_GB=500
  # Network adapters with IPv4 addresses (Get-NetAdapter + Get-NetIPAddress), and Fast Startup, for Wake-on-LAN
  NIC_ETH='{"Name":"Ethernet","Description":"Intel(R) Ethernet Connection I219-V","Mac":"00-1A-2B-3C-4D-5E","Ip":"10.1.2.3","Prefix":24,"Hardware":true,"Virtual":false,"Media":"802.3","PmWake":"Disabled","MagicAdv":"0","S5":"0","Armed":false}'
  NIC_WSL='{"Name":"vEthernet (WSL (Hyper-V firewall))","Description":"Hyper-V Virtual Ethernet Adapter","Mac":"00-15-5D-01-02-03","Ip":"172.20.0.1","Prefix":20,"Hardware":false,"Virtual":true,"Media":"802.3"}'
  NIC_VPN='{"Name":"VPN","Description":"Cisco AnyConnect Virtual Miniport Adapter","Mac":"00-05-9A-3C-7A-00","Ip":"10.200.0.5","Prefix":32,"Hardware":false,"Virtual":false,"Media":"802.3"}'
  export NSU_LLM_TEST_NICS="[$NIC_WSL,$NIC_ETH,$NIC_VPN]" NSU_LLM_TEST_HIBERBOOT=1

  [ -f "$NODE/install-node.ps1" ] && pass "exists install-node.ps1" || fail "exists install-node.ps1"
  [ -f "$NODE/node-boot.ps1" ] && pass "exists node-boot.ps1" || fail "exists node-boot.ps1"
  for f in install-node.ps1 node-boot.ps1; do
    [ "$(head -c 3 "$NODE/$f" 2>/dev/null | od -An -tx1 | tr -d ' ')" = efbbbf ] && pass "$f has a UTF-8 BOM (PowerShell 5.1 reads Cyrillic)" || fail "$f has a UTF-8 BOM"
  done

  : > "$TMP/fake.log"
  out="$(full)"; rc=$?
  [ $rc -eq 0 ] && pass "dry run succeeds" || { fail "dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "summary before actions" "$out" "Будет сделано"
  contains "summary shows the detected GPU" "$out" "RTX 4070"
  contains "detected accel nvidia" "$out" "nvidia"
  lacks "token never printed" "$out" "SECRETTOKEN7"
  lacks "task password never printed" "$out" " pw "
  # .wslconfig: keys from the Microsoft docs (wsl-config), merged into the user's file
  contains "wslconfig memory" "$out" "memory=28GB"
  contains "wslconfig mirrored" "$out" "networkingMode=mirrored"
  contains "wslconfig vmIdleTimeout" "$out" "vmIdleTimeout=-1"
  contains "wslconfig instanceIdleTimeout" "$out" "instanceIdleTimeout=-1"
  contains "wslconfig change restarts the WSL VM" "$out" "wsl --shutdown"
  # distro already present: no install
  lacks "existing distro is not reinstalled" "$out" "wsl --install"
  contains "setup runs inside the distro as root" "$out" "wsl -d NSU-LLM -u root -e env ACCEL=nvidia HOST_MOUNT=/mnt/"
  contains "setup gets the Windows disk of the distro to watch" "$out" "bash /mnt/"
  # power
  contains "standby off on AC" "$out" "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0"
  contains "standby off on DC" "$out" "powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0"
  contains "hibernate idle off" "$out" "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE 0"
  contains "hibernation off" "$out" "powercfg /hibernate off"
  # firewall (SEC-3, H2): run-node sets both firewalls; 18080 only for main, 18050 (RPC without authentication) only for main and,
  # later, the nodes of the split group main names; the subnet of the class is never opened
  contains "hyper-v rule for WSL" "$out" "New-NetFirewallHyperVRule -Name NSU-LLM-Node "
  contains "hyper-v rule VMCreatorId" "$out" "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"
  contains "hyper-v inference rule: main only" "$out" "-LocalPorts 18080 -RemoteAddresses 10.9.0.5 -Action Allow"
  contains "hyper-v RPC rule of its own: main only until the group is known" "$out" "New-NetFirewallHyperVRule -Name NSU-LLM-Node-RPC -DisplayName 'NSU LLM Node' -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 18050 -RemoteAddresses 10.9.0.5"
  contains "windows inference rule: main only" "$out" "-LocalPort 18080 -RemoteAddress 10.9.0.5 -Action Allow"
  contains "windows RPC rule: main only until the group is known" "$out" "-DisplayName NSU-LLM-Node-RPC -Direction Inbound -Protocol TCP -LocalPort 18050 -RemoteAddress 10.9.0.5 -Action Allow"
  lacks "the subnet of the class is not opened" "$out" "10.1.2.0/24"
  lacks "the ports never share a rule" "$out" "18080,18050"
  contains "the summary names the RPC rule" "$out" "TCP 18050 (RPC split) только с главного и нод текущей группы split"
  # boot task
  contains "boot task registered" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Node"
  contains "boot task at startup" "$out" "-AtStartup"
  contains "boot task runs node-boot.ps1" "$out" "node-boot.ps1"
  contains "boot task stores the password (runs without logon)" "$out" "-Password ***"
  # run-node.ps1 -Wsl is reused, not duplicated
  contains "run-node through WSL" "$out" "wsl -d NSU-LLM -u root -e docker run"
  contains "run-node gpu image" "$out" "nsu-llm-node:0.2.1-gpu"
  contains "run-node node ip" "$out" "NODE_IP=10.1.2.3"
  contains "answers file would be saved" "$out" "install.json"
  log="$(cat "$TMP/fake.log")"
  lacks "dry run changes no power setting" "$log" "/setacvalueindex"
  lacks "dry run installs nothing in WSL" "$log" "--install"
  lacks "dry run starts no setup" "$log" "env ACCEL"

  # standalone: only 18080
  out="$(full -Mode standalone)"
  contains "standalone hyper-v rule only 18080" "$out" "-LocalPorts 18080 "
  lacks "standalone no 18050 in hyper-v rule" "$out" "-LocalPorts 18080,18050"

  # explicit -AllowFrom wins
  out="$(full -AllowFrom 192.168.50.0/23,10.9.0.5)"
  contains "explicit AllowFrom" "$out" "-RemoteAddresses 192.168.50.0/23,10.9.0.5"
  contains "explicit AllowFrom does not widen the RPC rule" "$out" "-LocalPort 18050 -RemoteAddress 10.9.0.5 -Action Allow"
  # a list saved by an older version opened the whole subnet of the node (and so the RPC port to every student): dropped with a warning
  mkdir -p "$IDIR"; printf '{"Main":"10.9.0.5","AllowFrom":"10.1.2.0/24,10.9.0.5"}' > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw -MemoryGB 28)"
  contains "a saved subnet of an older version is dropped with a warning" "$out" "открывал порты всей подсети ноды"
  lacks "a saved subnet is not applied to the rules" "$out" "-RemoteAddress 10.1.2.0/24"
  contains "main only after the saved subnet is dropped" "$out" "-LocalPort 18080 -RemoteAddress 10.9.0.5 -Action Allow"
  printf '{"Main":"10.9.0.5","AllowFrom":"10.9.0.5,10.9.0.6"}' > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw -MemoryGB 28)"
  contains "a saved list of single addresses is kept for the inference port" "$out" "-LocalPort 18080 -RemoteAddress 10.9.0.5,10.9.0.6 -Action Allow"
  contains "but the RPC port stays main only" "$out" "-LocalPort 18050 -RemoteAddress 10.9.0.5 -Action Allow"
  rm -rf "$IDIR"
  out="$(full -CaSha256 zzz)"; rc=$?
  [ $rc -ne 0 ] && pass "a malformed -CaSha256 stops the install" || fail "a malformed -CaSha256 stops the install"
  contains "a malformed -CaSha256 is explained" "$out" "нужны 64 шестнадцатеричные цифры"

  # ---------------------------------------------------------------- Wake-on-LAN (task 2-18)
  out="$(full)"
  # the adapter that carries the node address, not the WSL vEthernet or the VPN
  contains "wol: the physical NIC of the node address" "$out" "Wake-on-LAN: сетевая карта Ethernet (Intel(R) Ethernet Connection I219-V), MAC 00-1A-2B-3C-4D-5E, 10.1.2.3/24"
  contains "wol: magic packet on the NIC" "$out" "Set-NetAdapterPowerManagement -Name Ethernet -WakeOnMagicPacket Enabled"
  contains "wol: magic packet driver property" "$out" "Set-NetAdapterAdvancedProperty -Name Ethernet -RegistryKeyword *WakeOnMagicPacket -RegistryValue 1"
  contains "wol: wake from shutdown driver property" "$out" "Set-NetAdapterAdvancedProperty -Name Ethernet -RegistryKeyword S5WakeOnLan -RegistryValue 1"
  contains "wol: the device may wake the computer" "$out" "powercfg /deviceenablewake 'Intel(R) Ethernet Connection I219-V'"
  contains "wol: fast startup off" "$out" "HiberbootEnabled -Value 0"
  contains "wol: power button does nothing by default" "$out" "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 0"
  contains "wol: power button on battery too" "$out" "powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 0"
  contains "wol: the node gets the NIC" "$out" "WOL_MAC=00-1A-2B-3C-4D-5E"
  contains "wol: the node gets the NIC address" "$out" "WOL_IP=10.1.2.3"
  contains "wol: the node gets the prefix" "$out" "WOL_PREFIX=24"
  lacks "wol: never the WSL adapter" "$out" "00-15-5D-01-02-03"
  contains "wol: power button saved in the answers" "$out" "PowerButton=nothing"
  out="$(full -Mode standalone)"
  contains "wol: standalone too" "$out" "WOL_MAC=00-1A-2B-3C-4D-5E"
  # already set up: nothing to change (idempotent)
  done_nic='{"Name":"Ethernet","Description":"Intel(R) Ethernet Connection I219-V","Mac":"00-1A-2B-3C-4D-5E","Ip":"10.1.2.3","Prefix":24,"Hardware":true,"Virtual":false,"Media":"802.3","PmWake":"Enabled","MagicAdv":"1","S5":"1","Armed":true}'
  out="$(NSU_LLM_TEST_NICS="[$done_nic]" NSU_LLM_TEST_HIBERBOOT=0 FAKE_POWER_AC=00000000 FAKE_POWER_DC=00000000 full)"
  lacks "wol rerun: no adapter change" "$out" "Set-NetAdapterPowerManagement"
  lacks "wol rerun: no wake arming" "$out" "deviceenablewake"
  lacks "wol rerun: fast startup already off" "$out" "HiberbootEnabled -Value 0"
  lacks "wol rerun: power button already does nothing" "$out" "PBUTTONACTION 0"
  contains "wol rerun: the node still gets the NIC" "$out" "WOL_MAC=00-1A-2B-3C-4D-5E"
  # a driver without the advanced properties: only what the adapter has
  bare='{"Name":"Ethernet 2","Description":"Realtek PCIe GbE Family Controller","Mac":"00-E0-4C-68-00-01","Ip":"10.1.2.3","Prefix":23,"Hardware":true,"Virtual":false,"Media":"802.3","PmWake":"Disabled"}'
  out="$(NSU_LLM_TEST_NICS="[$bare]" full)"
  contains "wol: adapter named with spaces is quoted" "$out" "Set-NetAdapterPowerManagement -Name 'Ethernet 2' -WakeOnMagicPacket Enabled"
  lacks "wol: no advanced property the driver lacks" "$out" "Set-NetAdapterAdvancedProperty"
  contains "wol: prefix of the adapter" "$out" "WOL_PREFIX=23"
  # the node address sits on a virtual adapter (Hyper-V switch, VPN) or on nothing: explained, the install goes on
  out="$(NSU_LLM_TEST_NICS="[$NIC_WSL,$NIC_VPN]" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token SECRETTOKEN7 -NodeIp 10.200.0.5 -Name pc01 -MemoryGB 28 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "wol: virtual adapter does not stop the install" || fail "wol: virtual adapter does not stop the install (rc=$rc)"
  contains "wol: virtual adapter explained" "$out" "Wake-on-LAN не настраивается"
  contains "wol: virtual adapter named" "$out" "Cisco AnyConnect"
  lacks "wol: virtual adapter not passed" "$out" "WOL_MAC"
  lacks "wol: virtual adapter not changed" "$out" "Set-NetAdapterPowerManagement"
  contains "wol: power button still applies" "$out" "PBUTTONACTION 0"
  out="$(NSU_LLM_TEST_NICS='[]' full)"
  contains "wol: no adapter with the address explained" "$out" "Wake-on-LAN не настраивается"
  wifi='{"Name":"Wi-Fi","Description":"Intel(R) Wi-Fi 6 AX201","Mac":"00-1A-2B-3C-4D-60","Ip":"10.1.2.3","Prefix":24,"Hardware":true,"Virtual":false,"Media":"Native 802.11","PmWake":"Disabled"}'
  out="$(NSU_LLM_TEST_NICS="[$wifi]" full)"
  contains "wol: wi-fi warned" "$out" "Wi-Fi"
  contains "wol: wi-fi still set up" "$out" "WOL_MAC=00-1A-2B-3C-4D-60"
  # -PowerButton keep leaves the button alone
  out="$(full -PowerButton keep)"
  lacks "wol: -PowerButton keep" "$out" "PBUTTONACTION"
  contains "wol: -PowerButton keep saved" "$out" "PowerButton=keep"
  printf '{"Main":"10.9.0.5","Mode":"k3s","Name":"pc01","MemoryGB":28,"PowerButton":"keep"}' > "$TMP/pb.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -AnswersFile "$(cygpath -w "$TMP/pb.json")" -NodeIp 10.1.2.3 -TaskPassword pw)"
  lacks "wol: PowerButton from the answers file" "$out" "PBUTTONACTION"
  # interactive: the question has the default «д»; «н» keeps the button
  out="$(printf '10.9.0.5\nTOKEN9\n\n\n\npw\nн\nд\n' | inst -DryRun -NodeIp 10.1.2.3 -Name pc01)"
  contains "wol: power button question" "$out" "Кнопка питания ничего не делает"
  contains "wol: power button question default" "$out" "(д/н) [д]"
  lacks "wol: answered н keeps the button" "$out" "PBUTTONACTION"

  # fresh machine: no distro, online install; offline source uses --from-file
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full -ImageTar 'C:\img\nsu-llm-node-0.2.1-gpu.tar.gz')"
  contains "fresh distro install" "$out" "wsl --install Ubuntu-24.04 --name NSU-LLM --location"
  contains "fresh distro without launching the OOBE" "$out" "--no-launch"
  contains "image tar loaded into the distro" "$out" "docker load -i /mnt/c/img/nsu-llm-node-0.2.1-gpu.tar.gz"
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full -DistroSource 'C:\img\ubuntu-24.04.wsl' -ImageTar 'C:\img\n.tar')"
  contains "offline distro install" "$out" "[dry-run] wsl --install --from-file C:\\img\\ubuntu-24.04.wsl --name NSU-LLM"
  # no archive and no image: the image comes from main (registry https://<main>/v2/), pulled inside the distro after Docker is set up
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full)"; rc=$?
  [ $rc -eq 0 ] && pass "fresh distro without an image tar: dry run succeeds, the image comes from main" || { fail "fresh distro without an image tar: dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "no archive: the plan pulls the image from main" "$out" "скачать образ ноды nsu-llm-node:0.2.1-gpu с главного: https://10.9.0.5/v2/"
  contains "pull runs pull-image.sh in the distro as root" "$out" "-u root -e bash /mnt/"
  contains "pull-image.sh gets the main and the image" "$out" "pull-image.sh --main 10.9.0.5 --image nsu-llm-node:0.2.1-gpu"
  lacks "no archive: no docker load" "$out" "docker load"
  # the root of main is checked before Docker pins it (pull-image.sh alone would take it on trust): fingerprint, then --ca
  contains "the root of main is verified before the pull" "$out" "проверить корень CA главного (https://10.9.0.5/ca.crt)"
  contains "the fingerprint question is announced when none is given" "$out" "(не задан: печатается и подтверждается)"
  contains "the verified root goes to pull-image.sh" "$out" "--image nsu-llm-node:0.2.1-gpu --no-tofu --ca /var/lib/nsu-llm-node/state/ca.crt"
  [ "$(before "$out" 'проверить корень CA главного')" -lt "$(before "$out" 'pull-image.sh --main')" ] && pass "the root is checked before the pull" || fail "the root is checked before the pull"
  out2="$(full -CaSha256 "$(printf 'ab%.0s' $(seq 32))" -ImageSource main)"
  contains "-CaSha256 is shown in the plan of the check" "$out2" "по отпечатку $(printf 'ab%.0s' $(seq 32))"
  lacks "the pull-image.sh of the old version gets no --record" "$out" "--record"
  # a pull-image.sh that knows --record records the image digest in the node state (the node reports it to main)
  mkdir -p "$IDIR/scripts"; printf '# pull-image.sh [--record FILE]
' > "$IDIR/scripts/pull-image.sh"
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full)"
  contains "pull-image.sh with --record: the digest file in the node state" "$out" "--image nsu-llm-node:0.2.1-gpu --no-tofu --ca /var/lib/nsu-llm-node/state/ca.crt --record /var/lib/nsu-llm-node/state/image.digest"
  rm -rf "$IDIR"

  # the tag comes from main (GET /api/node-image = NODE_IMAGE_TAG), not from a constant of the scripts (found on the 122b bed: main ran
  # 0.1.1, install-node pulled 0.1.1-gpu and run-node then checked the GPU with 0.2.1-gpu)
  NI='{"tag":"0.1.1","cpu":"nsu-llm-node:0.1.1-cpu","nvidia":"nsu-llm-node:0.1.1-gpu"}'
  out="$(FAKE_NODE_IMAGE="$NI" FAKE_DISTROS='' FAKE_RUNNING='' full)"; rc=$?
  [ $rc -eq 0 ] && pass "main names another tag: dry run succeeds" || { fail "main names another tag: dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "main's tag: the pull is for the image main names" "$out" "pull-image.sh --main 10.9.0.5 --image nsu-llm-node:0.1.1-gpu"
  contains "main's tag: the plan says so" "$out" "скачать образ ноды nsu-llm-node:0.1.1-gpu с главного"
  contains "main's tag: run-node starts that very image" "$out" "nsu-llm-node:0.1.1-gpu"
  lacks "main's tag: no stale 0.2.1 image anywhere in the plan" "$out" "nsu-llm-node:0.2.1"
  contains "the installer asked main without a token" "$(cat "$TMP/fake.log")" "https://10.9.0.5/api/node-image"
  lacks "the token is not sent to the public endpoint" "$(grep 'api/node-image' "$TMP/fake.log")" "SECRETTOKEN7"
  lacks "main names no digest: the pull is by tag, as before" "$out" "--digest"
  DG="sha256:$(printf 'ab%.0s' $(seq 32))"
  NI2='{"tag":"0.1.1","cpu":"nsu-llm-node:0.1.1-cpu","nvidia":"nsu-llm-node:0.1.1-gpu","digest":{"cpu":"","nvidia":"'"$DG"'"}}'
  out="$(FAKE_NODE_IMAGE="$NI2" FAKE_DISTROS='' FAKE_RUNNING='' full)"
  contains "main names a digest: the first pull goes by it" "$out" "--image nsu-llm-node:0.1.1-gpu --no-tofu --ca /var/lib/nsu-llm-node/state/ca.crt --digest $DG"
  out="$(FAKE_NODE_IMAGE='{"cpu":"nsu-llm-node:0.1.1-cpu","nvidia":"nsu-llm-node:0.1.1-gpu","digest":{"nvidia":"sha256:zz"}}' FAKE_DISTROS='' FAKE_RUNNING='' full)"
  lacks "a malformed digest is ignored" "$out" "--digest"
  out="$(FAKE_NODE_IMAGE='{"cpu":"evil:1","nvidia":"nsu-llm-node:../x"}' FAKE_DISTROS='' FAKE_RUNNING='' full)"
  contains "a name that is not ours is ignored: IMAGE_VERSION of the scripts" "$out" "pull-image.sh --main 10.9.0.5 --image nsu-llm-node:0.2.1-gpu"
  [ "$(head -n1 "$NODE/IMAGE_VERSION" | tr -d '[:space:]')" = "0.2.1" ] && pass "IMAGE_VERSION is the one shared fallback" || fail "IMAGE_VERSION is the one shared fallback"
  for f in install-node.ps1 run-node.ps1 run-node.sh; do
    ! grep -nE '^[$]Version = .[0-9]|^VERSION="[0-9]' "$NODE/$f" >/dev/null && pass "$f has no version constant of its own" || fail "$f has no version constant of its own"
  done
  [ "$(before "$out" 'wsl-setup.sh')" -lt "$(before "$out" 'pull-image.sh --main')" ] && pass "the pull comes after Docker is set up in the distro" || fail "the pull comes after Docker is set up in the distro"
  # -ImageSource tar keeps the old rule: no archive, no image -> stop with the fix
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full -ImageSource tar)"; rc=$?
  [ $rc -ne 0 ] && pass "-ImageSource tar without an archive fails" || fail "-ImageSource tar without an archive fails"
  contains "-ImageSource tar: the fix names -ImageTar" "$out" "-ImageTar"
  # -ImageSource main pulls even when the image is already there (refresh after an update of main) and ignores archives
  out="$(full -ImageSource main -ImageTar 'C:\img\nsu-llm-node-0.2.1-gpu.tar.gz')"
  contains "-ImageSource main pulls although the image exists" "$out" "pull-image.sh --main 10.9.0.5 --image nsu-llm-node:0.2.1-gpu"
  lacks "-ImageSource main ignores the archive" "$out" "docker load"
  contains "-ImageSource main is saved with the answers" "$out" "ImageSource=main"
  out="$(full)"
  lacks "an existing image and no -ImageSource: nothing is pulled" "$out" "pull-image.sh --main"

  # WSL missing -> install the platform, reboot, rerun; old WSL -> update
  out="$(FAKE_WSL_VERSION=none full)"; rc=$?
  contains "wsl platform install" "$out" "wsl --install --no-distribution"
  contains "reboot requested" "$out" "Перезагрузите"
  # 3010 (ERROR_SUCCESS_REBOOT_REQUIRED); bash sees the low 8 bits: 3010 & 255 = 194
  [ $rc -eq 194 ] && pass "reboot exit code 3010" || fail "reboot exit code 3010 (rc=$rc)"
  out="$(FAKE_WSL_VERSION=2.3.26.0 full)"
  contains "old wsl updated" "$out" "wsl --update"

  # GPU: driver too old / no GPU in WSL -> cpu with an explanation; -Accel nvidia then fails
  out="$(FAKE_WIN_GPU='NVIDIA GeForce RTX 4070, 552.44, 12282' full)"
  contains "old driver falls back to cpu" "$out" "nsu-llm-node:0.2.1-cpu"
  contains "old driver explained" "$out" "570"
  out="$(FAKE_WIN_GPU='NVIDIA GeForce RTX 4070, 552.44, 12282' full -Accel nvidia)"; rc=$?
  [ $rc -ne 0 ] && pass "-Accel nvidia with an old driver fails" || fail "-Accel nvidia with an old driver fails"
  out="$(FAKE_WIN_GPU='' full)"
  contains "no nvidia-smi -> cpu" "$out" "nsu-llm-node:0.2.1-cpu"
  lacks "cpu node gets no toolkit" "$out" "ACCEL=nvidia"
  out="$(FAKE_WSL_GPU=0 full)"
  contains "no GPU in WSL -> cpu" "$out" "ACCEL=cpu"

  # non-interactive without the main: clear error with the command to run
  out="$(inst -DryRun -NonInteractive -Token T -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -ne 0 ] && pass "missing -Main fails" || fail "missing -Main fails"
  contains "missing -Main named" "$out" "-Main"
  contains "missing -Main error in Russian" "$out" "Не задан"

  # interactive: answers on stdin; the questions carry defaults; the answers are read back next time
  out="$(printf '10.9.0.5\nTOKEN9\n\n\n\npw\nд\n' | inst -DryRun -NodeIp 10.1.2.3 -Name pc01)"; rc=$?
  [ $rc -eq 0 ] && pass "interactive dry run succeeds" || { fail "interactive dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "asks for the main" "$out" "Адрес главного сервера"
  contains "asks for the token" "$out" "Токен кластера"
  contains "mode question has a default" "$out" "[k3s]"
  contains "allow-from question has main as the default (not the subnet)" "$out" "[10.9.0.5]"
  lacks "allow-from question does not offer the subnet" "$out" "10.1.2.0/24"
  lacks "interactive token not echoed" "$out" "TOKEN9"
  out="$(printf '10.9.0.5\nTOKEN9\n\n\n\npw\n\nн\n' | inst -DryRun -NodeIp 10.1.2.3 -Name pc01)"; rc=$?
  [ $rc -ne 0 ] && pass "declined summary stops" || fail "declined summary stops"
  lacks "declined summary does nothing" "$out" "[dry-run]"
  mkdir -p "$IDIR"
  printf '{"Main":"10.9.0.7","Mode":"standalone","AllowFrom":"10.1.2.0/24,10.9.0.7","MemoryGB":20,"Name":"pc02"}' > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "answers file run succeeds" || { fail "answers file run succeeds"; echo "$out" | sed 's/^/    | /'; }
  contains "answers file main" "$out" "MAIN_HOST=10.9.0.7"
  contains "answers file mode" "$out" "MODE=standalone"
  contains "answers file memory" "$out" "memory=20GB"
  contains "answers file name" "$out" "--hostname pc02"
  rm -f "$IDIR/install.json"
  # -AnswersFile: a prepared file elsewhere (mass deployment); a missing one is a clear error
  printf '{"Main":"10.9.0.8","Mode":"k3s","Name":"pc03","MemoryGB":24}' > "$TMP/class.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -AnswersFile "$(cygpath -w "$TMP/class.json")" -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "-AnswersFile run succeeds" || { fail "-AnswersFile run succeeds"; echo "$out" | sed 's/^/    | /'; }
  contains "-AnswersFile main" "$out" "MAIN_HOST=10.9.0.8"
  contains "-AnswersFile name" "$out" "--hostname pc03"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -AnswersFile 'C:\no\such.json' -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -ne 0 ] && pass "missing -AnswersFile fails" || fail "missing -AnswersFile fails"
  contains "missing -AnswersFile explained" "$out" "Файл ответов"

  # the main is probed before the summary with HTTPS GET /readyz (200 + the portal's JSON), not a bare TCP connect
  : > "$TMP/fake.log"
  out="$(full)"
  contains "main ready" "$out" "портал готов"
  plog="$(cat "$TMP/fake.log")"
  contains "probe is https /readyz" "$plog" "https://10.9.0.5/readyz"
  contains "probe bypasses a proxy" "$plog" "--noproxy"
  contains "probe also checks k3s 6443 over TLS" "$plog" "https://10.9.0.5:6443/"
  out="$(FAKE_READYZ=down full)"; rc=$?
  contains "unreachable main warned" "$out" "недоступен по HTTPS"
  [ $rc -eq 0 ] && pass "unreachable main does not stop the install" || fail "unreachable main does not stop the install (rc=$rc)"
  out="$(FAKE_READYZ=503 full)"
  contains "main not ready shows the parts" "$out" "litellm=False"
  out="$(FAKE_READYZ=html full)"
  contains "something else on 443 is named" "$out" "не портал NSU LLM"
  out="$(FAKE_K3S=down full)"
  contains "k3s port down warned" "$out" "6443"
  out="$(FAKE_K3S=down full -Mode standalone)"
  lacks "standalone does not need 6443" "$out" "6443 не отвечает"

  # ---------------------------------------------------------------- secrets: DPAPI LocalMachine, old CurrentUser format still read
  mkdir -p "$IDIR"
  enc="$("$PS" -NoProfile -Command "Add-Type -AssemblyName System.Security; [Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes('MACHINETOK9'), \$null, 'LocalMachine'))" | tr -d '\r')"
  printf '{"Main":"10.9.0.5","Mode":"k3s","Name":"pc01","TokenMachine":"%s"}' "$enc" > "$IDIR/install.json"
  out="$(inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "LocalMachine token read from install.json" || { fail "LocalMachine token read from install.json (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  lacks "decrypted token not printed" "$out" "MACHINETOK9"
  # the old format is what ConvertFrom-SecureString writes: hex of a CurrentUser DPAPI blob of the UTF-16 string
  # (built with ProtectedData: the SecureString cmdlets do not load when PSModulePath comes from pwsh 7)
  old="$("$PS" -NoProfile -Command "Add-Type -AssemblyName System.Security; [BitConverter]::ToString([Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::Unicode.GetBytes('OLDTOK7'), \$null, 'CurrentUser')).Replace('-', '').ToLower()" | tr -d '\r')"
  printf '{"Main":"10.9.0.5","Mode":"k3s","Name":"pc01","TokenProtected":"%s"}' "$old" > "$IDIR/install.json"
  out="$(inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "old CurrentUser token format still read" || { fail "old CurrentUser token format still read (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  rm -f "$IDIR/install.json"
  # round trip of the helpers, and the stored form is not the secret
  rt="$("$PS" -NoProfile -Command "\$src = Get-Content -Raw -Encoding UTF8 '$INST'; \$a = \$src.IndexOf('function Protect-Secret'); \$b = \$src.IndexOf('# end secrets'); Invoke-Expression \$src.Substring(\$a, \$b - \$a); \$e = Protect-Secret 'ROUNDTRIP5'; if (\$e -match 'ROUNDTRIP5') { 'PLAIN' } else { Unprotect-Secret \$e }" 2>&1 | tr -d '\r')"
  [ "$rt" = ROUNDTRIP5 ] && pass "Protect-Secret/Unprotect-Secret round trip" || fail "Protect-Secret/Unprotect-Secret round trip (got: $rt)"
  # the answers file is created restricted to Administrators and SYSTEM; the install dir is secured before any write
  out="$(full)"
  contains "answers file created admin-only" "$out" "только Administrators и SYSTEM"
  contains "secured dir: owner Administrators, protected DACL" "$out" "владелец Administrators, SYSTEM и Administrators полный доступ, Users чтение, без наследования от ProgramData"
  # dry run lists the saved answers with absolute paths and the node address (task mode loses nothing)
  contains "saved answers keep the node address" "$out" "NodeIp=10.1.2.3"
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full -ImageTar 'rel\nsu-llm-node-0.2.1-gpu.tar.gz' -DistroSource 'rel\ubuntu.wsl')"
  contains "ImageTar saved absolute" "$out" "ImageTar=$(cygpath -w "$PWD")\\rel\\nsu-llm-node-0.2.1-gpu.tar.gz"
  contains "DistroSource saved absolute" "$out" "DistroSource=$(cygpath -w "$PWD")\\rel\\ubuntu.wsl"

  # ---------------------------------------------------------------- secure-dir.ps1 (shared with class-deploy): unit checks as a normal user
  SDS="$(cygpath -w "$NODE/secure-dir.ps1")"
  [ -f "$NODE/secure-dir.ps1" ] && pass "exists secure-dir.ps1" || fail "exists secure-dir.ps1"
  [ "$(head -c 3 "$NODE/secure-dir.ps1" 2>/dev/null | od -An -tx1 | tr -d ' ')" = efbbbf ] && pass "secure-dir.ps1 has a UTF-8 BOM" || fail "secure-dir.ps1 has a UTF-8 BOM"
  SDT="$TMP/sdt"; mkdir -p "$SDT"
  cat > "$TMP/sd-test.ps1" <<'EOS'
param([string]$Lib, [string]$Dir)
$ErrorActionPreference = 'Stop'
# Seam: a non-elevated test cannot make Administrators the owner, and Users-read-only would lock it out: same DACL plus this account.
function New-SdDirSecurity([switch]$NoUsers, [switch]$NoOwner, [string[]]$ExtraSids = @()) {
    $global:DirSecCalls += , [pscustomobject]@{ NoUsers = [bool]$NoUsers; NoOwner = [bool]$NoOwner }
    $sec = New-Object Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)
    $inh = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)) {
        $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier $sid), 'FullControl', $inh, 'None', 'Allow')))
    }
    if (-not $NoUsers) { $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-545'), 'ReadAndExecute', $inh, 'None', 'Allow'))) }
    return $sec
}
$global:DirSecCalls = @()
. $Lib
function Chk([string]$n, [bool]$ok) { if ($ok) { "ok:$n" } else { "fail:$n" } }
function Rules([string]$p) { @((Get-SdAcl $p).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value }) }
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
# a dir with only SYSTEM, Administrators and me (an administrator) is trusted
$good = Join-Path $Dir 'good'; New-Item -ItemType Directory -Path $good | Out-Null
& icacls $good /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*${me}:(OI)(CI)F" | Out-Null
Chk 'trusted dir' (Test-TrustedPath $good)
# a write ACE for Users or Everyone makes it untrusted, also on a child
$bad = Join-Path $Dir 'bad'; New-Item -ItemType Directory -Path $bad | Out-Null
& icacls $bad /grant "*S-1-1-0:(OI)(CI)M" | Out-Null
Chk 'everyone-writable dir untrusted' (-not (Test-TrustedPath $bad))
$kid = Join-Path $good 'kid.ps1'; Set-Content -Path $kid -Value 'x'
& icacls $kid /grant "*S-1-5-32-545:(M)" | Out-Null
Chk 'users-writable child untrusted' (-not (Test-TrustedPath $good))
& icacls $kid /remove:g "*S-1-5-32-545" | Out-Null
Chk 'read-only Users ACE is fine' (Test-TrustedPath $good)
# a junction anywhere in the tree is untrusted
$j = Join-Path $good 'link'
& cmd /c mklink /J $j $bad | Out-Null
Chk 'junction untrusted' (-not (Test-TrustedPath $good))
& cmd /c rmdir $j | Out-Null
# Write-SecureFile -AdminOnly: protected DACL with only SYSTEM and Administrators from the moment of creation
$f = Join-Path $Dir 'secret.json'
Write-SecureFile -Path $f -Text '{"a":1}' -AdminOnly
$acl = [IO.File]::GetAccessControl($f)
Chk 'secret protected' $acl.AreAccessRulesProtected
Chk 'secret only SYSTEM and Administrators' (((Rules $f | Sort-Object -Unique) -join ',') -eq 'S-1-5-18,S-1-5-32-544')
# Write-SecureFile replaces an existing file with a new one (an old open handle does not see the new content)
$plain = Join-Path $good 'plain.txt'; Set-Content -Path $plain -Value 'old'
$h = [IO.File]::Open($plain, 'Open', 'ReadWrite', 'ReadWrite, Delete')
Write-SecureFile -Path $plain -Text 'new'
$h.Close()
Chk 'replaced file has the new text' (([IO.File]::ReadAllText($plain)) -eq 'new')

# --- Secure-Dir: an untrusted dir is moved aside and recreated with its SD, never fixed in place
$u = Join-Path $Dir 'nsu'; New-Item -ItemType Directory -Path $u | Out-Null
& icacls $u /grant "*S-1-1-0:(OI)(CI)M" | Out-Null
Set-Content -Path (Join-Path $u 'marker.txt') -Value 'student'
$out = (Secure-Dir -Path $u -DryRun *>&1 | Out-String -Width 4096)
Chk 'dry run: untrusted dir would be moved aside' ($out -match 'отложить его в')
Chk 'dry run changed nothing' ((-not (Test-TrustedPath $u)) -and (Test-Path (Join-Path $u 'marker.txt')))
$global:DirSecCalls = @()
Secure-Dir -Path $u | Out-Null
$aside = @(Get-ChildItem -LiteralPath $Dir -Directory -Filter 'nsu.untrusted-*')
Chk 'untrusted dir moved aside' ($aside.Count -eq 1 -and (Test-Path (Join-Path $aside[0].FullName 'marker.txt')))
Chk 'dir recreated empty and trusted' ((Test-TrustedPath $u) -and -not (Get-ChildItem -LiteralPath $u -Force))
Chk 'dir recreated in one step with its SD (owner Administrators)' (@($global:DirSecCalls | Where-Object { -not $_.NoOwner }).Count -eq 1)
Chk 'the student file is not in the recreated dir' (-not (Test-Path (Join-Path $u 'marker.txt')))
Remove-Item -LiteralPath $aside[0].FullName -Recurse -Force
# a handle that blocks the rename: Secure-Dir stops with an error instead of fixing the dir in place
$u2 = Join-Path $Dir 'nsu2'; New-Item -ItemType Directory -Path $u2 | Out-Null
& icacls $u2 /grant "*S-1-1-0:(OI)(CI)M" | Out-Null
$hold = [IO.Directory]::Exists($u2); $lock = [IO.File]::Open((Join-Path $u2 'lock.txt'), 'Create', 'ReadWrite', 'None')
$err = ''; try { Secure-Dir -Path $u2 | Out-Null } catch { $err = $_.Exception.Message }
$lock.Close()
Chk 'blocked rename: stops with an error, or the dir was moved aside' (($err -match 'отложить его в сторону не удалось' -and -not (Test-TrustedPath $u2)) -or (-not $err -and (Test-TrustedPath $u2)))
# a missing dir is created with its SD at once (no window with inherited rights)
$n = Join-Path $Dir 'fresh'
$global:DirSecCalls = @()
Secure-Dir -Path $n | Out-Null
Chk 'missing dir created with its SD' ((Test-TrustedPath $n) -and (Get-SdAcl $n).AreAccessRulesProtected -and $global:DirSecCalls.Count -eq 1)
# a reparse point as the dir itself is refused
$j2 = Join-Path $Dir 'rootlink'; & cmd /c mklink /J $j2 $good | Out-Null
$err = ''; try { Secure-Dir -Path $j2 -DryRun | Out-Null } catch { $err = $_.Exception.Message }
Chk 'junction as the dir refused' ($err -match 'ссылка')
& cmd /c rmdir $j2 | Out-Null

# --- wsl\ is an excluded subtree: the root stays (the distro disk cannot move), untrusted children are moved aside
$t = Join-Path $Dir 'inst'; New-Item -ItemType Directory -Path (Join-Path $t 'wsl') | Out-Null
& icacls $t /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*${me}:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
$vhd = Join-Path $t 'wsl\ext4.vhdx'; Set-Content -Path $vhd -Value 'disk'
& icacls $vhd /grant "*S-1-5-83-0:(M)" | Out-Null                      # the Hyper-V VM ACE
Chk 'wsl contents make the tree untrusted without -Exclude' (-not (Test-TrustedPath $t))
Chk 'wsl contents ignored with -Exclude wsl' (Test-TrustedPath $t -Exclude 'wsl')
Set-Content -Path (Join-Path $t 'state.json') -Value '{}'
$planted = Join-Path $t 'node-boot.ps1'; Set-Content -Path $planted -Value 'x'; & icacls $planted /grant "*S-1-1-0:(M)" | Out-Null
& icacls $t /grant "*S-1-5-32-545:(CI)(AD)" | Out-Null                 # a student ACE on the root itself (ProgramData-style create)
Chk 'other children still judged with -Exclude wsl' (-not (Test-TrustedPath $t -Exclude 'wsl'))
$out = (Secure-Dir -Path $t -Exclude 'wsl' -DryRun *>&1 | Out-String -Width 4096)
Chk 'dry run: says wsl contents are left alone' ($out -match 'содержимое wsl не трогается')
Chk 'dry run: planted child would be moved aside' ($out -match [regex]::Escape("отложить $planted"))
Chk 'dry run: wsl loses Users access' ($out -match 'wsl: без наследования, убрать .*S-1-5-32-545')
Secure-Dir -Path $t -Exclude 'wsl' | Out-Null
Chk 'root kept (distro disk inside)' (Test-Path $vhd)
Chk 'planted child moved aside' (-not (Test-Path $planted) -and @(Get-ChildItem -LiteralPath $Dir -Directory -Filter 'inst.untrusted-*' | Where-Object { Test-Path (Join-Path $_.FullName 'node-boot.ps1') }).Count -eq 1)
Chk 'trusted child kept' (Test-Path (Join-Path $t 'state.json'))
Chk 'student ACE on the root gone (DACL replaced whole)' (-not ((Get-SdAcl $t).GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq 'S-1-5-32-545' -and ([int64]$_.FileSystemRights -band 0x4) }))
Chk 'wsl: no Users, Everyone or CREATOR OWNER left' (-not (Rules (Join-Path $t 'wsl') | Where-Object { @('S-1-5-32-545', 'S-1-1-0', 'S-1-5-11', 'S-1-3-0') -contains $_ }))
Chk 'wsl contents untouched: the VM ACE stays on the disk' ((Rules $vhd) -contains 'S-1-5-83-0')
Chk 'disk no longer readable by Users' (-not ((Rules $vhd) -contains 'S-1-5-32-545'))
Chk 'tree trusted afterwards' (Test-TrustedPath $t -Exclude 'wsl')
# New-SecureDir -NoUsers: wsl\ before wsl --install has no Users entry at all
$w2 = Join-Path $Dir 'w2\wsl'; New-Item -ItemType Directory -Path (Split-Path $w2) | Out-Null
New-SecureDir $w2 -NoUsers
Chk 'New-SecureDir -NoUsers: no Users entry' (-not ((Rules $w2) -contains 'S-1-5-32-545'))
# wsl\ as a junction: refused
Remove-Item -LiteralPath (Join-Path $t 'wsl') -Recurse -Force
& cmd /c mklink /J (Join-Path $t 'wsl') $bad | Out-Null
Chk 'wsl junction untrusted' (-not (Test-TrustedPath $t -Exclude 'wsl'))
$err = ''; try { Secure-Dir -Path $t -Exclude 'wsl' -DryRun | Out-Null } catch { $err = $_.Exception.Message }
Chk 'wsl junction refused by Secure-Dir' ($err -match 'ссылка')
& cmd /c rmdir (Join-Path $t 'wsl') | Out-Null
EOS
  sdo="$("$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$TMP/sd-test.ps1")" -Lib "$SDS" -Dir "$(cygpath -w "$SDT")" 2>&1 | tr -d '\r')"
  n_ok=0
  while IFS= read -r l; do
    case "$l" in ok:*) pass "secure-dir: ${l#ok:}"; n_ok=$((n_ok + 1)) ;; fail:*) fail "secure-dir: ${l#fail:}" ;; esac
  done <<< "$sdo"
  [ "$n_ok" -ge 34 ] && pass "secure-dir: all checks ran ($n_ok)" || { fail "secure-dir: checks missing (ran $n_ok ok)"; echo "$sdo" | grep -v '^ok:' | sed 's/^/    | /' | head -12; }

  # the distro disk under wsl\ (with an ACE for the Hyper-V VM, here: Everyone) does not make the install dir untrusted
  mkdir -p "$IDIR/wsl"; echo disk > "$IDIR/wsl/ext4.vhdx"
  icacls "$(cygpath -w "$IDIR/wsl/ext4.vhdx")" /grant '*S-1-1-0:(M)' >/dev/null
  printf '{"Main":"10.9.0.7","Mode":"k3s","Name":"pc01"}' > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"
  contains "wsl disk ACL: install dir still trusted" "$out" "MAIN_HOST=10.9.0.7"
  lacks "wsl disk ACL: no untrusted warning" "$out" "не доверенный"
  contains "wsl contents left alone by Secure-Dir" "$out" "содержимое wsl не трогается"
  lacks "wsl contents never moved aside" "$out" "\wsl в "
  rm -rf "$IDIR"

  # -Status shows the ACL of wsl\ (the distro disk holds the token): open to Users/Everyone is a problem
  mkdir -p "$IDIR/wsl"; printf '{"Main":"127.0.0.1","Name":"pc01","Mode":"k3s"}' > "$IDIR/install.json"
  out="$(inst -Status)"
  contains "status: wsl ACL printed" "$out" "wsl: владелец"
  lacks "status: clean wsl ACL is not a problem" "$out" "доступен не только администраторам"
  icacls "$(cygpath -w "$IDIR/wsl")" /grant '*S-1-1-0:(OI)(CI)R' >/dev/null
  out="$(inst -Status)"
  contains "status: wsl readable by Everyone is a problem" "$out" "диск дистрибутива доступен не только администраторам"
  rm -rf "$IDIR"

  # wsl\ is created admin-only BEFORE wsl --install --location (otherwise ext4.vhdx would inherit Users read)
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full -ImageTar 'C:\img\nsu-llm-node-0.2.1-gpu.tar.gz')"
  mk="$(echo "$out" | grep -n 'New-SecureDir .*wsl -NoUsers' | head -1 | cut -d: -f1)"; wi="$(echo "$out" | grep -n 'dry-run\] wsl --install Ubuntu' | head -1 | cut -d: -f1)"
  [ -n "$mk" ] && [ -n "$wi" ] && [ "$mk" -lt "$wi" ] && pass "wsl dir created admin-only before wsl --install" || fail "wsl dir created admin-only before wsl --install ($mk, $wi)"

  # Find-ImageTar looks only next to the script, and only at archives an administrator controls
  NC="$TMP/nodecopy"; mkdir -p "$NC/node"
  cp "$NODE"/install-node.ps1 "$NODE"/run-node.ps1 "$NODE"/node-boot.ps1 "$NODE"/wsl-setup.sh "$NODE"/pull-image.sh "$NODE"/secure-dir.ps1 "$NODE"/class-run.ps1 "$NODE"/IMAGE_VERSION "$NC/node/"
  echo img > "$NC/nsu-llm-node-0.2.1-gpu.tar.gz"                     # the parent (C:\ProgramData from the stage): not searched
  echo img > "$NC/node/nsu-llm-node-0.2.1-gpu.tar.gz"
  icacls "$(cygpath -w "$NC/node/nsu-llm-node-0.2.1-gpu.tar.gz")" /grant '*S-1-1-0:(M)' >/dev/null
  NCI="$(cygpath -w "$NC/node/install-node.ps1")"
  ncrun() { PATH="$FB:$PATH" FAKE_LOG="$(cygpath -w "$TMP/fake.log")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$NCI" \
      -InstallDir "$(cygpath -w "$IDIR")" -WslConfigPath "$(cygpath -w "$TMP/wslconfig")" -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -MemoryGB 28 -TaskPassword pw "$@" 2>&1; }
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' ncrun)"
  contains "image tar writable by others is skipped" "$out" "пропущен: его создал или может менять не администратор"
  lacks "image tar in the parent dir is never used" "$out" "nodecopy/nsu-llm-node-0.2.1-gpu.tar.gz"
  lacks "no untrusted image loaded" "$out" "docker load"
  icacls "$(cygpath -w "$NC/node/nsu-llm-node-0.2.1-gpu.tar.gz")" /remove:g '*S-1-1-0' >/dev/null
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' ncrun)"
  contains "trusted image tar next to the script is used" "$out" "docker load -i /mnt/"
  icacls "$(cygpath -w "$NC/node/nsu-llm-node-0.2.1-gpu.tar.gz")" /grant '*S-1-1-0:(M)' >/dev/null
  fsout="$(NSU_LLM_TEST_FS=exFAT FAKE_DISTROS='' FAKE_RUNNING='' ncrun)"
  contains "image tar on FAT/exFAT: says to copy it to NTFS" "$fsout" "скопируйте архив на NTFS-диск (например, C:\\)"
  contains "trusted image tar: the one next to the script" "$out" "nodecopy/node/nsu-llm-node-0.2.1-gpu.tar.gz"
  # an archive of another version than main's image (NODE_IMAGE_TAG): docker load would give the wrong name, so main's image is pulled
  icacls "$(cygpath -w "$NC/node/nsu-llm-node-0.2.1-gpu.tar.gz")" /remove:g '*S-1-1-0' >/dev/null
  out="$(FAKE_NODE_IMAGE='{"tag":"0.1.1","cpu":"nsu-llm-node:0.1.1-cpu","nvidia":"nsu-llm-node:0.1.1-gpu"}' FAKE_DISTROS='' FAKE_RUNNING='' ncrun)"; rc=$?
  [ $rc -eq 0 ] && pass "archive of another version: the dry run succeeds" || { fail "archive of another version: the dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "archive of another version: says it is not used" "$out" "архив не используется, образ nsu-llm-node:0.1.1-gpu будет скачан с главного"
  contains "archive of another version: main's image is pulled" "$out" "pull-image.sh --main 10.9.0.5 --image nsu-llm-node:0.1.1-gpu"
  lacks "archive of another version: nothing is loaded" "$out" "docker load"
  out="$(FAKE_NODE_IMAGE='{"tag":"0.1.1","cpu":"nsu-llm-node:0.1.1-cpu","nvidia":"nsu-llm-node:0.1.1-gpu"}' FAKE_DISTROS='' FAKE_RUNNING='' ncrun -ImageTar "$(cygpath -w "$NC/node/nsu-llm-node-0.2.1-gpu.tar.gz")")"; rc=$?
  [ $rc -ne 0 ] && pass "archive of another version named with -ImageTar: an error" || fail "archive of another version named with -ImageTar: an error"
  contains "archive of another version named with -ImageTar: names the file that is needed" "$out" "nsu-llm-node-0.1.1-gpu.tar.gz"
  out="$(FAKE_NODE_IMAGE='{"tag":"0.2.1","cpu":"nsu-llm-node:0.2.1-cpu","nvidia":"nsu-llm-node:0.2.1-gpu"}' FAKE_DISTROS='' FAKE_RUNNING='' ncrun)"
  contains "archive of main's version is loaded as before" "$out" "docker load -i /mnt/"
  rm -rf "$NC" "$IDIR"

  # PSModulePath inherited from pwsh 7: install-node.ps1 drops the 7.x module paths at start-up (Get-Acl and friends load again)
  P7="$USERPROFILE\\Documents\\PowerShell\\Modules;C:\\Program Files\\PowerShell\\Modules;c:\\program files\\powershell\\7\\Modules;C:\\Program Files\\WindowsPowerShell\\Modules;C:\\WINDOWS\\system32\\WindowsPowerShell\\v1.0\\Modules"
  cat > "$TMP/pm.ps1" <<'EOS'
param([string]$Inst, [switch]$Fix)
if ($Fix) {
    $src = [IO.File]::ReadAllText($Inst)
    $a = $src.IndexOf('# --- PSModulePath begin'); $b = $src.IndexOf('# --- PSModulePath end')
    Invoke-Expression $src.Substring($a, $b - $a)
}
try { $null = Get-Acl -LiteralPath $env:TEMP -ErrorAction Stop; 'acl-ok' } catch { 'acl-broken' }
EOS
  before="$(PSModulePath="$P7" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$TMP/pm.ps1")" -Inst "$INST" 2>&1 | tr -d '\r')"
  after="$(PSModulePath="$P7" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$TMP/pm.ps1")" -Inst "$INST" -Fix 2>&1 | tr -d '\r')"
  if [ -d "/c/Program Files/PowerShell/7" ]; then
    contains "pwsh 7 PSModulePath breaks Get-Acl without the fix (precondition)" "$before" "acl-broken"
  else echo "skip - pwsh 7 not installed: the precondition cannot be shown"; fi
  contains "pwsh 7 PSModulePath: Get-Acl loads after install-node's start-up fix" "$after" "acl-ok"
  out="$(PSModulePath="$P7" full)"; rc=$?
  [ $rc -eq 0 ] && pass "dry run under a pwsh 7 PSModulePath succeeds" || { fail "dry run under a pwsh 7 PSModulePath succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }

  # an install dir writable by others is not trusted: its install.json is ignored (a planted main must not get the token)
  mkdir -p "$IDIR"
  printf '{"Main":"6.6.6.6","Mode":"k3s","Name":"pc01"}' > "$IDIR/install.json"
  icacls "$(cygpath -w "$IDIR")" /grant '*S-1-1-0:(OI)(CI)M' >/dev/null
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  contains "untrusted install dir: answers ignored" "$out" "не доверенный"
  lacks "untrusted install dir: planted main not used" "$out" "MAIN_HOST=6.6.6.6"
  [ $rc -ne 0 ] && pass "untrusted install dir: no main, stops" || fail "untrusted install dir: no main, stops"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -Main 10.9.0.5 -NodeIp 10.1.2.3 -TaskPassword pw)"
  contains "untrusted install dir: moved aside and recreated, not fixed in place" "$out" "отложить его в"
  rm -rf "$IDIR"

  # ---------------------------------------------------------------- -Proxy
  out="$(full -Proxy http://proxy.nsu.ru:3128)"; rc=$?
  [ $rc -eq 0 ] && pass "proxy dry run succeeds" || { fail "proxy dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "proxy in the summary" "$out" "Прокси: http://proxy.nsu.ru:3128"
  contains "proxy NO_PROXY covers the main and the class" "$out" "10.9.0.5"
  contains "proxy NO_PROXY covers the class subnet" "$out" "NO_PROXY=localhost,127.0.0.1"
  contains "proxy reaches wsl-setup (apt, docker CE, daemon)" "$out" "PROXY=http://proxy.nsu.ru:3128"
  contains "proxy NO_PROXY reaches wsl-setup" "$out" "NO_PROXY_LIST="
  contains "proxy for the node container" "$out" "-e HTTPS_PROXY"
  contains "no_proxy for the node container" "$out" "-e NO_PROXY"
  contains "proxy goes to wsl-setup through WSLENV, not argv" "$out" "WSLENV"
  out="$(full -Proxy http://user:s3cret@proxy.nsu.ru:3128)"
  lacks "proxy password never printed" "$out" "s3cret"
  contains "proxy password masked" "$out" "http://user:***@proxy.nsu.ru:3128"
  out="$(full -Proxy 'http://u:secret9@x;calc')"
  lacks "invalid proxy error masks the password" "$out" "secret9"
  out="$(full -Proxy 'http://x;calc')"; rc=$?
  [ $rc -ne 0 ] && pass "junk proxy fails" || fail "junk proxy fails"
  contains "junk proxy explained" "$out" "Недопустимый -Proxy"
  mkdir -p "$IDIR"; printf '{"Main":"10.9.0.5","Mode":"k3s","Name":"pc01","Proxy":"http://proxy.nsu.ru:3128"}' > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"
  contains "proxy kept in the answers file" "$out" "Прокси: http://proxy.nsu.ru:3128"
  pxe="$("$PS" -NoProfile -Command "Add-Type -AssemblyName System.Security; [Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes('http://u:pw9@proxy.nsu.ru:3128'), \$null, 'LocalMachine'))" | tr -d '\r')"
  printf '{"Main":"10.9.0.5","Mode":"k3s","Name":"pc01","Proxy":"http://u:***@proxy.nsu.ru:3128","ProxyMachine":"%s"}' "$pxe" > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "proxy with a password read from ProxyMachine" || { fail "proxy with a password read from ProxyMachine (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "proxy with a password shown masked" "$out" "http://u:***@proxy.nsu.ru:3128"
  lacks "proxy password from the file not printed" "$out" "pw9"
  rm -f "$IDIR/install.json"
  out="$(FAKE_DISTROS="NSU-LLM docker-desktop" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -Proxy http://proxy.nsu.ru:3128)"
  contains "desktop: daemon proxy left to Docker Desktop settings" "$out" "Docker Desktop: прокси"
  contains "desktop: container still gets the proxy" "$out" "-e HTTPS_PROXY"

  # ---------------------------------------------------------------- ssh without credentials: install through a one-time batch-logon task
  out="$(full -ViaTask)"; rc=$?
  [ $rc -eq 0 ] && pass "-ViaTask dry run succeeds" || { fail "-ViaTask dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "-ViaTask registers the one-time task" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  contains "-ViaTask child reads the saved answers" "$out" "FromTask=True"
  lacks "-ViaTask token not in the task" "$out" "SECRETTOKEN7"
  lacks "-ViaTask does not run the steps itself" "$out" "[1/"
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 full)"
  contains "no-credential logon switches to the task" "$out" "без учётных данных"
  contains "no-credential logon task registered" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -MemoryGB 28)"; rc=$?
  [ $rc -ne 0 ] && pass "no-credential logon without a password fails" || fail "no-credential logon without a password fails"
  contains "no-credential logon names -TaskPassword" "$out" "-TaskPassword"
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 FAKE_DISTROS="NSU-LLM docker-desktop" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01)"; rc=$?
  [ $rc -eq 0 ] && pass "no-credential logon on Docker Desktop runs directly" || { fail "no-credential logon on Docker Desktop runs directly (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  lacks "no-credential logon on Docker Desktop: no install task" "$out" "NSU-LLM-Install"
  # the task gets its inputs through the DPAPI parameter file of class-run.ps1, password masked in the plan
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 full)"
  contains "task runner is class-run.ps1" "$out" "class-run.ps1"
  contains "task params carry the node address" "$out" "NodeIp=10.1.2.3"
  contains "task params password masked" "$out" "TaskPassword=***"
  lacks "task params password not printed" "$out" "TaskPassword=pw"
  # I3: in a credential-less session the parent does not judge the image, the task does
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 FAKE_IMAGES='' full)"; rc=$?
  [ $rc -eq 0 ] && pass "task mode: missing image left to the task" || { fail "task mode: missing image left to the task (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "task mode: image warning" "$out" "проверит задача"

  # ---------------------------------------------------------------- Docker Desktop already on the machine
  DD="NSU-LLM docker-desktop"
  # -NonInteractive keeps Docker Desktop (fallback path) with a warning: no WSL changes, no boot task, no password needed
  : > "$TMP/fake.log"
  out="$(FAKE_DISTROS="$DD" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token SECRETTOKEN7 -NodeIp 10.1.2.3 -Name pc01)"; rc=$?
  [ $rc -eq 0 ] && pass "desktop: non-interactive dry run succeeds" || { fail "desktop: non-interactive dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "desktop: detected" "$out" "Docker Desktop"
  contains "desktop: non-interactive keeps it with a warning" "$out" "Внимание: найден Docker Desktop"
  contains "desktop: node runs on the Windows docker" "$out" "docker run -d --name llm-node"
  lacks "desktop: node not run through WSL" "$out" "wsl -d NSU-LLM -u root -e docker run"
  contains "desktop: bridge with published inference port" "$out" "-p 18080:18080"
  lacks "desktop: no wsl --shutdown (it would stop Docker Desktop)" "$out" "wsl --shutdown"
  lacks "desktop: no distro setup" "$out" "-e env ACCEL="
  lacks "desktop: no boot task" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Node -TaskPath"
  contains "desktop: a SYSTEM task keeps the RPC rule in step with the split group" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Node-FwSync"
  contains "desktop: the sync task runs run-node -SyncFirewall" "$out" "run-node.ps1\" -SyncFirewall"
  contains "desktop: the sync task runs as SYSTEM" "$out" "-User SYSTEM"
  contains "desktop: autostart limit explained" "$out" "после входа"
  contains "desktop: firewall limited to main" "$out" "-LocalPort 18080 -RemoteAddress 10.9.0.5 -Action Allow"
  contains "desktop: the RPC port is published on the node address only" "$out" "-p 10.1.2.3:18050:18050"
  contains "desktop: power still set" "$out" "powercfg /hibernate off"
  contains "desktop: engine checked" "$(cat "$TMP/fake.log")" "docker info"
  # image archive goes to the Windows docker
  out="$(FAKE_DISTROS="$DD" FAKE_IMAGES='' inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -ImageTar 'C:\img\nsu-llm-node-0.2.1-gpu.tar.gz')"
  contains "desktop: image loaded by the Windows docker" "$out" "[dry-run] docker load -i C:\\img\\nsu-llm-node-0.2.1-gpu.tar.gz"
  # Docker Desktop: the Windows docker pulls itself, only when Windows already trusts the certificate of main; the installer never fetches or writes a CA there
  out="$(FAKE_DISTROS="$DD" FAKE_IMAGES='' inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01)"
  contains "desktop: the image is pulled from main" "$out" "[dry-run] docker pull 10.9.0.5/nsu-llm-node:0.2.1-gpu"
  contains "desktop: retagged to the local name" "$out" "[dry-run] docker tag 10.9.0.5/nsu-llm-node:0.2.1-gpu nsu-llm-node:0.2.1-gpu"
  lacks "desktop: no CA is fetched with -k and nothing is written to certs.d" "$out" "certs.d"
  contains "desktop: Windows trust is probed first" "$out" "https://10.9.0.5/v2/ (сертификат главного должен приниматься Windows"
  # engine not running: clear message with the fix
  out="$(FAKE_DD_ENGINE=0 FAKE_DISTROS="$DD" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01)"
  contains "desktop: stopped engine explained" "$out" "Docker Desktop не запущен"
  # interactive: the question carries the default; answering wsl takes the main path
  out="$(printf '10.9.0.5\nTOKEN9\n\nwsl\n\n\npw\nд\n' | FAKE_DISTROS="$DD" inst -DryRun -NodeIp 10.1.2.3 -Name pc01)"; rc=$?
  [ $rc -eq 0 ] && pass "desktop: interactive switch to wsl succeeds" || { fail "desktop: interactive switch to wsl succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "desktop: question with default" "$out" "[desktop]"
  contains "desktop: wsl answer runs the node in WSL" "$out" "wsl -d NSU-LLM -u root -e docker run"
  contains "desktop: wsl answer warns that Docker Desktop stops" "$out" "Docker Desktop"
  # -Docker wsl: no question
  out="$(FAKE_DISTROS="$DD" full -Docker wsl)"
  contains "-Docker wsl uses WSL" "$out" "wsl -d NSU-LLM -u root -e docker run"
  # status and uninstall follow the saved choice
  mkdir -p "$IDIR"
  printf '{"Main":"127.0.0.1","Name":"pc01","Mode":"k3s","Docker":"desktop"}' > "$IDIR/install.json"
  out="$(FAKE_DISTROS="$DD" inst -Status)"
  contains "desktop status: engine" "$out" "Docker Desktop работает"
  contains "desktop status: container" "$out" "Up 5 minutes"
  lacks "desktop status: no boot task required" "$out" "Задача NSU-LLM-Node не найдена"
  out="$(FAKE_DISTROS="$DD" CLUSTER_TOKEN=T inst -DryRun -NonInteractive -Uninstall)"
  contains "desktop uninstall removes the container on the Windows docker" "$out" "docker rm -f llm-node"
  lacks "desktop uninstall not through WSL" "$out" "wsl -d NSU-LLM -u root -e docker rm"
  rm -rf "$IDIR"

  # ---------------------------------------------------------------- a dropped ssh/WinRM session must not kill the install (task 2-28)
  # from an ssh or WinRM session with a password the whole install runs as the one-time task (WSL and Docker Desktop)
  out="$(NSU_LLM_ASSUME_REMOTE=1 full)"; rc=$?
  [ $rc -eq 0 ] && pass "remote session with a password: dry run succeeds" || { fail "remote session with a password: dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "remote session: the install goes through the one-time task" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  contains "remote session: said why" "$out" "чтобы обрыв связи её не прервал"
  contains "remote session: a dropped connection does not stop it, -Status and a re-run show the progress" "$out" "Обрыв ssh или WinRM её не остановит"
  lacks "remote session: the steps do not run in the session" "$out" "[1/"
  lacks "remote session: no warning about running unprotected" "$out" "Windows завершит установку на полпути"
  lacks "remote session: password not printed" "$out" "TaskPassword=pw"
  out="$(FAKE_DISTROS="$DD" NSU_LLM_ASSUME_REMOTE=1 inst -DryRun -NonInteractive -Main 10.9.0.5 -Token SECRETTOKEN7 -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "remote session on Docker Desktop: dry run succeeds" || { fail "remote session on Docker Desktop: dry run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "remote session on Docker Desktop: the one-time task too" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  contains "remote session on Docker Desktop: the child reads the saved answers" "$out" "FromTask=True"
  lacks "remote session on Docker Desktop: the node is not started by the session" "$out" "docker run -d --name llm-node"
  lacks "remote session on Docker Desktop: no boot task (it does not exist there)" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Node"
  lacks "remote session on Docker Desktop: token not in the task" "$out" "SECRETTOKEN7"
  # no password: the install runs in the session, with a warning how to make it survive
  out="$(FAKE_DISTROS="$DD" NSU_LLM_ASSUME_REMOTE=1 inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01)"; rc=$?
  [ $rc -eq 0 ] && pass "remote session without a password on Docker Desktop: still installs" || { fail "remote session without a password on Docker Desktop: still installs (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "remote session without a password: warned" "$out" "Windows завершит установку на полпути"
  contains "remote session without a password: names -TaskPassword and NSU_LLM_TASK_PASSWORD" "$out" "-TaskPassword (или NSU_LLM_TASK_PASSWORD)"
  contains "remote session without a password: runs in the session" "$out" "docker run -d --name llm-node"
  lacks "remote session without a password: no install task" "$out" "NSU-LLM-Install"
  # a local session is not touched; -ViaTask now works for Docker Desktop too
  out="$(FAKE_DISTROS="$DD" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"
  lacks "local session on Docker Desktop with a password: no install task" "$out" "NSU-LLM-Install"
  contains "local session on Docker Desktop with a password: runs directly" "$out" "docker run -d --name llm-node"
  out="$(FAKE_DISTROS="$DD" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw -ViaTask)"
  contains "-ViaTask on Docker Desktop: the one-time task" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  out="$(FAKE_DISTROS="$DD" inst -DryRun -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -ViaTask)"; rc=$?
  [ $rc -ne 0 ] && pass "-ViaTask without a password fails" || fail "-ViaTask without a password fails"
  contains "-ViaTask without a password names the password" "$out" "-TaskPassword"
  # the session is told from sshd's variables or the WinRM sender info
  out="$( unset NSU_LLM_ASSUME_REMOTE; SSH_CONNECTION='10.0.0.7 51234 10.1.2.3 22' full )"
  contains "ssh session recognised by SSH_CONNECTION" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  out="$( unset NSU_LLM_ASSUME_REMOTE SSH_CONNECTION SSH_CLIENT SSH_TTY; full )"
  lacks "no ssh variables: a local session" "$out" "NSU-LLM-Install"

  # a re-run after the connection dropped attaches to the task that keeps running instead of starting again
  mkdir -p "$IDIR/run"
  printf 'Установка идёт\r\n[3/9] запустить ноду\r\nпоследняя строка\r\n' > "$IDIR/run/install-task.log"
  printf 'install' > "$IDIR/run/action.txt"
  printf '0' > "$IDIR/run/run.done"
  out="$(NSU_LLM_TEST_INSTALL_TASK=Running inst -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "re-run while the task is running: attaches and returns the task's code" || { fail "re-run while the task is running: attaches (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "re-run: explains that it only shows the running install" "$out" "этот запуск ничего не меняет"
  contains "re-run: shows the log of the task from the start" "$out" "Установка идёт"
  contains "re-run: shows the last log lines" "$out" "последняя строка"
  lacks "re-run: starts nothing" "$out" "Register-ScheduledTask"
  lacks "re-run: runs no steps" "$out" "[1/"
  printf '5' > "$IDIR/run/run.done"
  out="$(NSU_LLM_TEST_INSTALL_TASK=Running inst -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"; rc=$?
  [ $rc -eq 5 ] && pass "re-run: the attached task's exit code is returned" || { fail "re-run: the attached task's exit code is returned (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  printf 'uninstall' > "$IDIR/run/action.txt"
  out="$(NSU_LLM_TEST_INSTALL_TASK=Running inst -NonInteractive -Main 10.9.0.5 -Token T -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"; rc=$?
  [ $rc -ne 0 ] && pass "re-run with another action than the running one is refused" || fail "re-run with another action than the running one is refused"
  contains "re-run with another action: says what is running" "$out" "сейчас идёт удаление"
  # -Status shows the running task and the result of the last one
  printf 'install' > "$IDIR/run/action.txt"
  out="$(NSU_LLM_TEST_INSTALL_TASK=Running inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "status: the running install is shown" "$out" "идёт установка в разовой задаче NSU-LLM-Install"
  contains "status: with the last log lines" "$out" "последняя строка"
  # inside the task the output of -Status IS this log: reading its tail would feed the log into itself without end
  out="$(NSU_LLM_TEST_INSTALL_TASK=Running inst -Status -FromTask -Main 127.0.0.1 -Name pc01)"
  lacks "status inside the task itself: its own log is not read back" "$out" "последняя строка"
  lacks "status inside the task itself: does not report itself as a running task" "$out" "идёт установка в разовой задаче"
  printf '1' > "$IDIR/run/run.done"
  out="$(inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "status: a failed last run is a problem" "$out" "завершился с кодом 1"
  printf '0' > "$IDIR/run/run.done"
  out="$(inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "status: a successful last run is reported" "$out" "завершился успешно (код 0)"
  rm -rf "$IDIR"

  # ---------------------------------------------------------------- Docker Desktop autostart: a logged-on user is needed (task 2-28)
  mkdir -p "$IDIR"
  printf '{"Main":"127.0.0.1","Name":"pc01","Mode":"k3s","Docker":"desktop"}' > "$IDIR/install.json"
  out="$(NSU_LLM_TEST_LOGON='PC01\student' FAKE_DISTROS="$DD" inst -Status)"
  contains "desktop status: autostart only after a logon" "$out" "стартует только после входа пользователя в Windows"
  contains "desktop status: Windows 10 class PCs need a logged-on user" "$out" "на Windows 10 ПК класса держит ноду, пока кто-то вошёл"
  contains "desktop status: auto-logon is the administrator's choice" "$out" "Winlogon AutoAdminLogon"
  contains "desktop status: the logged-on user" "$out" "вошёл в Windows: PC01\\student"
  out="$(NSU_LLM_TEST_LOGON=none FAKE_DD_ENGINE=0 FAKE_DISTROS="$DD" inst -Status)"; rc=$?
  contains "desktop status: nobody logged on is named as the reason" "$out" "в Windows никто не вошёл"
  contains "desktop status: the fix mentions auto-logon" "$out" "автовход (netplwiz, Winlogon AutoAdminLogon) решает администратор"
  [ $rc -eq 1 ] && pass "desktop status: no logon and no engine is a problem" || fail "desktop status: no logon and no engine is a problem (rc=$rc)"
  out="$(NSU_LLM_TEST_LOGON=none FAKE_DISTROS="$DD" inst -Status)"
  lacks "desktop status: a working engine with no listed user is not blamed on the logon" "$out" "в Windows никто не вошёл"
  out="$(NSU_LLM_TEST_LOGON=unknown FAKE_DD_ENGINE=0 FAKE_DISTROS="$DD" inst -Status)"
  lacks "desktop status: a failed logon query is not reported as «nobody logged on»" "$out" "в Windows никто не вошёл"
  contains "desktop status: a dead engine is still a problem when the logon is unknown" "$out" "Docker Desktop не запущен"
  rm -rf "$IDIR"

  # ---------------------------------------------------------------- old driver: interactive offer to continue as cpu
  OLD='NVIDIA GeForce RTX 4070, 552.44, 12282'
  out="$(printf '10.9.0.5\nTOKEN9\n\n\n\nн\n' | FAKE_WIN_GPU="$OLD" inst -DryRun -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"; rc=$?
  contains "old driver: offer to continue as cpu" "$out" "Продолжить как CPU-нода"
  [ $rc -ne 0 ] && pass "old driver: declining stops" || fail "old driver: declining stops"
  contains "old driver: how to update" "$out" "nvidia.com/Download"
  out="$(printf '10.9.0.5\nTOKEN9\n\n\n\nд\nд\n' | FAKE_WIN_GPU="$OLD" inst -DryRun -NodeIp 10.1.2.3 -Name pc01 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "old driver: accepting continues" || { fail "old driver: accepting continues (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "old driver: accepted -> cpu image" "$out" "nsu-llm-node:0.2.1-cpu"
  out="$(FAKE_WIN_GPU="$OLD" full)"
  lacks "old driver: non-interactive never asks" "$out" "Продолжить как CPU-нода"
  contains "old driver: non-interactive says how to update" "$out" "nvidia.com/Download"

  # ---------------------------------------------------------------- over ssh: -NonInteractive never reads stdin
  out="$(printf 'JUNK\nJUNK\nJUNK\n' | FAKE_DISTROS="$DD" inst -DryRun -NonInteractive -AnswersFile "$(cygpath -w "$TMP/class.json")" -Token T -NodeIp 10.1.2.3)"; rc=$?
  [ $rc -eq 0 ] && pass "ssh: answers file + -NonInteractive succeeds" || { fail "ssh: answers file + -NonInteractive succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  lacks "ssh: stdin not consumed" "$out" "JUNK"
  lacks "ssh: no prompt printed" "$out" "(д/н)"

  # ---------------------------------------------------------------- the saved image tar was deleted after install (class-deploy)
  mkdir -p "$IDIR"
  printf '{"Main":"10.9.0.5","Mode":"k3s","Name":"pc01","ImageTar":"C:\\\\staged\\\\gone\\\\nsu-llm-node-0.2.1-gpu.tar.gz"}' > "$IDIR/install.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"; rc=$?
  [ $rc -eq 0 ] && pass "gone tar + loaded image: re-run succeeds" || { fail "gone tar + loaded image: re-run succeeds (rc=$rc)"; echo "$out" | sed 's/^/    | /'; }
  contains "gone tar + loaded image: tar skipped" "$out" "уже загружен"
  lacks "gone tar + loaded image: no docker load" "$out" "docker load"
  # the image is missing too: non-interactive real run stops with the fix, interactive asks for the path
  out="$(CLUSTER_TOKEN=T FAKE_IMAGES='' inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw)"
  contains "gone tar + no image: explained" "$out" "не найден"
  contains "gone tar + no image: the image comes from main instead" "$out" "будет скачан с главного"
  contains "gone tar + no image: the pull is planned" "$out" "скачать образ ноды"
  out="$(CLUSTER_TOKEN=T FAKE_IMAGES='' inst -DryRun -NonInteractive -NodeIp 10.1.2.3 -TaskPassword pw -ImageSource tar)"
  contains "gone tar + no image + -ImageSource tar: names -ImageTar" "$out" "-ImageTar"
  out="$(printf '\n\nC:\\img\\new.tar.gz\nд\n' | CLUSTER_TOKEN=T FAKE_IMAGES='' inst -DryRun -NodeIp 10.1.2.3 -TaskPassword pw)"
  contains "gone tar + no image: interactive asks for the archive" "$out" "Путь к архиву образа"
  contains "gone tar + no image: the answered archive is loaded" "$out" "docker load -i /mnt/c/img/new.tar.gz"
  out="$(printf '\n\nrel\\new.tar.gz\nд\n' | CLUSTER_TOKEN=T FAKE_IMAGES='' inst -DryRun -NodeIp 10.1.2.3 -TaskPassword pw)"
  contains "gone tar + no image: a relative answer is made absolute" "$out" "ImageTar=$(cygpath -w "$PWD")\\rel\\new.tar.gz"
  rm -f "$IDIR/install.json"

  # existing .wslconfig: our keys merged, the owner's keys kept
  printf '[wsl2]\r\nprocessors=8\r\nmemory=8GB\r\n[experimental]\r\nhostAddressLoopback=true\r\n' > "$TMP/wslconfig"
  out="$(full)"
  contains "wslconfig keeps processors" "$out" "processors=8"
  contains "wslconfig keeps experimental keys" "$out" "hostAddressLoopback=true"
  lacks "wslconfig replaces memory" "$out" "memory=8GB"
  rm -f "$TMP/wslconfig"

  # -Uninstall: the distro disk is never deleted while WSL may still have the distro registered
  mkdir -p "$IDIR/wsl"; : > "$IDIR/wsl/ext4.vhdx"
  printf '{"CreatedDistro":true,"Distro":"NSU-LLM"}' > "$IDIR/state.json"
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' inst -DryRun -NonInteractive -Uninstall -Name pc01)"; rc=$?
  [ $rc -ne 0 ] && pass "uninstall: disk present, distro not listed -> stop" || fail "uninstall: disk present, distro not listed -> stop"
  contains "uninstall: stop explains the disk" "$out" "диск дистрибутива NSU-LLM не удаляется"
  lacks "uninstall: stop removes nothing" "$out" "[dry-run] Remove-Item -Recurse"
  out="$(FAKE_WSL_VERSION=none inst -DryRun -NonInteractive -Uninstall -Name pc01)"; rc=$?
  [ $rc -ne 0 ] && pass "uninstall: wsl error with the disk present -> stop" || fail "uninstall: wsl error with the disk present -> stop"
  # untrusted install dir: state.json is not read, so whether the script created the distro is unknown -> disk present = stop
  icacls "$(cygpath -w "$IDIR")" /grant '*S-1-1-0:(OI)(CI)M' >/dev/null
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' inst -DryRun -NonInteractive -Uninstall -Name pc01)"; rc=$?
  [ $rc -ne 0 ] && pass "uninstall: untrusted dir with a disk -> stop" || fail "uninstall: untrusted dir with a disk -> stop"
  contains "uninstall: untrusted dir, disk kept" "$out" "диск дистрибутива NSU-LLM не удаляется"
  icacls "$(cygpath -w "$IDIR")" /remove:g '*S-1-1-0' >/dev/null
  # task mode for -Uninstall in a credential-less session
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 inst -DryRun -NonInteractive -Uninstall -Name pc01 -TaskPassword pw)"
  contains "uninstall: task mode" "$out" "Register-ScheduledTask -TaskName NSU-LLM-Install"
  contains "uninstall: task runs -Uninstall" "$out" "Uninstall=True"
  lacks "uninstall: task without a token does not invent one" "$out" "Token=***"
  # the token given now goes into the task (it would read the one saved at install time, stale after a reinstall of main), masked in the output
  out="$(NSU_LLM_ASSUME_NO_CREDENTIALS=1 inst -DryRun -NonInteractive -Uninstall -Name pc01 -Main 10.9.0.5 -Token SeCr3tT0k -TaskPassword pw)"
  contains "uninstall: task carries the fresh token" "$out" "Token=***"
  lacks "uninstall: the token is never printed" "$out" "SeCr3tT0k"
  # installed by another account: refuse instead of touching this user's .wslconfig and distros
  printf '{"CreatedDistro":true,"Distro":"NSU-LLM","InstallUser":"PC\\\\other","InstallUserSid":"S-1-5-21-1-2-3-1001"}' > "$IDIR/state.json"
  out="$(inst -DryRun -NonInteractive -Uninstall -Name pc01)"; rc=$?
  [ $rc -ne 0 ] && pass "uninstall: other install user -> stop" || fail "uninstall: other install user -> stop"
  contains "uninstall: names the install user" "$out" "PC\\other"
  # empty saved power values are skipped
  printf '{"CreatedDistro":false,"Distro":"NSU-LLM","Power":{"StandbyAc":"","StandbyDc":900,"HibernateAc":0,"HibernateDc":0,"HibernateEnabled":0}}' > "$IDIR/state.json"
  out="$(inst -DryRun -NonInteractive -Uninstall -Name pc01)"
  lacks "uninstall: empty power value skipped" "$out" "STANDBYIDLE ''"
  contains "uninstall: other power values restored" "$out" "setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 900"
  rm -rf "$IDIR"
  # desktop uninstall also removes the node images
  mkdir -p "$IDIR"; printf '{"Main":"10.9.0.5","Name":"pc01","Mode":"k3s","Docker":"desktop"}' > "$IDIR/install.json"
  out="$(FAKE_DISTROS="NSU-LLM docker-desktop" inst -DryRun -NonInteractive -Uninstall)"
  contains "desktop uninstall removes the node images" "$out" "docker rmi nsu-llm-node:0.2.1-gpu"
  rm -rf "$IDIR"

  # -Uninstall: reverse of the install, using the saved state
  mkdir -p "$IDIR"
  printf '{"Main":"10.9.0.5","Name":"pc01","Mode":"k3s"}' > "$IDIR/install.json"
  printf '{"CreatedDistro":true,"Distro":"NSU-LLM","WslConfigExisted":false,"Power":{"StandbyAc":1800,"StandbyDc":900,"HibernateAc":0,"HibernateDc":0,"HibernateEnabled":1},"Wake":{"Nic":"Ethernet","Description":"Intel(R) Ethernet Connection I219-V","PmWake":"Disabled","MagicAdv":"0","S5":"0","Armed":false},"Hiberboot":1,"PowerButton":{"Ac":1,"Dc":2}}' > "$IDIR/state.json"
  out="$(CLUSTER_TOKEN=T inst -DryRun -NonInteractive -Uninstall)"; rc=$?
  [ $rc -eq 0 ] && pass "uninstall dry run succeeds" || { fail "uninstall dry run succeeds"; echo "$out" | sed 's/^/    | /'; }
  contains "uninstall restores magic packet wake" "$out" "Set-NetAdapterPowerManagement -Name Ethernet -WakeOnMagicPacket Disabled"
  contains "uninstall restores the driver property" "$out" "Set-NetAdapterAdvancedProperty -Name Ethernet -RegistryKeyword *WakeOnMagicPacket -RegistryValue 0"
  contains "uninstall restores wake from shutdown" "$out" "Set-NetAdapterAdvancedProperty -Name Ethernet -RegistryKeyword S5WakeOnLan -RegistryValue 0"
  contains "uninstall disarms the device it armed" "$out" "powercfg /devicedisablewake 'Intel(R) Ethernet Connection I219-V'"
  contains "uninstall restores fast startup" "$out" "HiberbootEnabled -Value 1"
  contains "uninstall restores the power button (AC)" "$out" "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 1"
  contains "uninstall restores the power button (DC)" "$out" "powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 2"
  contains "uninstall removes the node through run-node" "$out" "wsl -d NSU-LLM -u root -e docker rm -f llm-node"
  contains "uninstall removes the boot task" "$out" "Unregister-ScheduledTask -TaskName NSU-LLM-Node"
  contains "uninstall removes the hyper-v rule" "$out" "Remove-NetFirewallHyperVRule -Name NSU-LLM-Node"
  contains "uninstall removes the RPC rule" "$out" "Remove-NetFirewallHyperVRule -Name NSU-LLM-Node-RPC; Remove-NetFirewallRule -DisplayName NSU-LLM-Node-RPC"
  contains "uninstall removes the firewall sync task" "$out" "Unregister-ScheduledTask -TaskName NSU-LLM-Node-FwSync"
  contains "uninstall restores standby" "$out" "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 1800"
  contains "uninstall restores hibernation" "$out" "powercfg /hibernate on"
  contains "uninstall removes our .wslconfig" "$out" "Remove-Item"
  contains "uninstall unregisters the distro it created" "$out" "wsl --unregister NSU-LLM"
  contains "uninstall removes the install dir with the distro" "$out" "[dry-run] Remove-Item -Recurse -Force"
  printf '{"CreatedDistro":false,"Distro":"NSU-LLM"}' > "$IDIR/state.json"
  out="$(inst -DryRun -NonInteractive -Uninstall)"
  lacks "uninstall keeps a distro it did not create" "$out" "[dry-run] wsl --unregister"
  contains "uninstall says how to remove the kept distro" "$out" "Удалить: wsl --unregister NSU-LLM"
  contains "uninstall keeps the disk of a kept distro" "$out" "кроме wsl"
  lacks "uninstall does not wipe the install dir under a kept distro" "$out" "[dry-run] Remove-Item -Recurse -Force"
  contains "uninstall without a token warns about the main" "$out" "kubectl delete node pc01"
  contains "uninstall without a token names the panel route" "$out" "«Удалить из кластера…»"
  rm -rf "$IDIR"

  # -Status: short report with fixes; reads only
  : > "$TMP/fake.log"
  out="$(inst -Status -Main 127.0.0.1 -Name pc01)"; rc=$?
  contains "status WSL version" "$out" "2.6.1.0"
  contains "status distro running" "$out" "NSU-LLM"
  contains "status docker" "$out" "docker"
  contains "status container" "$out" "Up 5 minutes"
  contains "status gpu" "$out" "RTX 4070"
  contains "status boot task" "$out" "NSU-LLM-Node"
  contains "status missing task gives the fix" "$out" "install-node.ps1"
  [ $rc -ne 0 ] && pass "status with problems exits non-zero" || fail "status with problems exits non-zero"
  lacks "status changes nothing" "$(cat "$TMP/fake.log")" "/set"
  out="$(FAKE_RUNNING='' inst -Status -Main 127.0.0.1)"
  contains "status stopped distro gives the start command" "$out" "Start-ScheduledTask"
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' inst -Status -Main 127.0.0.1)"
  contains "status no distro" "$out" "не установлен"
  out="$(inst -Status -Main 127.0.0.1 -NodeIp 10.1.2.3)"
  contains "status wol adapter" "$out" "Wake-on-LAN: Ethernet"
  contains "status wol magic packet off is a problem" "$out" "Wake on Magic Packet выключен"
  contains "status fast startup on is a problem" "$out" "быстрый запуск Windows включён"


  # ---------------------------------------------------------------- B3: the system disk must not fill up because of the node
  # the check before the install
  out="$(NSU_LLM_TEST_FREE_GB=12 FAKE_IMAGES='' full)"; rc=$?
  [ $rc -ne 0 ] && pass "disk: 12 GB free and an image to fetch stops the install" || fail "disk: 12 GB free and an image to fetch stops the install"
  contains "disk: the error names the free space and the floor" "$out" "свободно 12 ГБ, а для установки ноды нужно не меньше 14 ГБ"
  contains "disk: the error tells what happens on a full disk" "$out" "ошибками ввода-вывода"
  contains "disk: the fix names the option and the cleanup" "$out" "-MinFreeGB"
  contains "disk: the fix points at Windows storage cleanup" "$out" "Временные файлы"
  lacks "disk: nothing is planned after the error" "$out" "Будет сделано"
  out="$(NSU_LLM_TEST_FREE_GB=12 FAKE_DISTROS='' FAKE_RUNNING='' full)"; rc=$?
  [ $rc -ne 0 ] && pass "disk: 12 GB free and no distro stops the install" || fail "disk: 12 GB free and no distro stops the install"
  out="$(NSU_LLM_TEST_FREE_GB=12 full)"; rc=$?
  [ $rc -eq 0 ] && pass "disk: a node that is already installed is not blocked by low space" || { fail "disk: a node that is already installed is not blocked by low space"; echo "$out" | sed 's/^/    | /'; }
  contains "disk: the installed node only gets a warning" "$out" "меньше порога 14 ГБ"
  out="$(NSU_LLM_TEST_FREE_GB=16 FAKE_IMAGES='' full)"; rc=$?
  [ $rc -eq 0 ] && pass "disk: 16 GB free is enough" || fail "disk: 16 GB free is enough"
  contains "disk: 16 GB free warns about the recommended 18" "$out" "рекомендуется не меньше 18 ГБ"
  out="$(full)"
  contains "disk: the free space is printed" "$out" "Свободно на диске"
  lacks "disk: plenty of space: no warning" "$out" "рекомендуется не меньше"
  out="$(NSU_LLM_TEST_FREE_GB=1 FAKE_IMAGES='' full -MinFreeGB 0)"; rc=$?
  [ $rc -eq 0 ] && pass "disk: -MinFreeGB 0 switches the check off" || fail "disk: -MinFreeGB 0 switches the check off"
  lacks "disk: switched off: no warning either" "$out" "рекомендуется не меньше"
  out="$(NSU_LLM_TEST_FREE_GB=12 FAKE_IMAGES='' full -MinFreeGB 10 -WarnFreeGB 15)"; rc=$?
  [ $rc -eq 0 ] && pass "disk: -MinFreeGB 10 lets 12 GB through" || fail "disk: -MinFreeGB 10 lets 12 GB through"
  contains "disk: the thresholds are saved with the answers" "$out" "MinFreeGB=10; WarnFreeGB=15"
  # -Status
  out="$(NSU_LLM_TEST_FREE_GB=12 inst -Status -Main 127.0.0.1 -Name pc01)"; rc=$?
  contains "disk status: below the floor is a problem" "$out" "[!!] на диске"
  contains "disk status: names the free space and the floor" "$out" "свободно 12 ГБ, меньше порога 14 ГБ"
  [ $rc -ne 0 ] && pass "disk status: exit code is not 0" || fail "disk status: exit code is not 0"
  out="$(NSU_LLM_TEST_FREE_GB=16 inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "disk status: between the floor and the recommendation: a note" "$out" "[--] на диске"
  contains "disk status: the note names the recommendation" "$out" "меньше рекомендованных 18 ГБ"
  out="$(NSU_LLM_TEST_FREE_GB=500 inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "disk status: plenty of space is ok" "$out" "[ok] на диске"
  contains "disk status: no sparse disk yet is said" "$out" "диск дистрибутива не разреженный"
  mkdir -p "$IDIR"; printf '{"DistroSparse":true}' > "$IDIR/state.json"
  out="$(inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "disk status: a sparse disk is said" "$out" "диск дистрибутива разреженный"
  mkdir -p "$IDIR/tmp"; head -c 2048 /dev/zero > "$IDIR/tmp/wsl-rootfs.tar"
  out="$(inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "disk status: leftovers of an interrupted install are shown" "$out" "остатки установки в"
  rm -rf "$IDIR"
  # the footprint of the node against the 16 GB limit
  mkdir -p "$IDIR"; head -c 4096 /dev/zero > "$IDIR/some.bin"
  out="$(NSU_LLM_TEST_FOOTPRINT_GB=21.4 inst -Status -Main 127.0.0.1 -Name pc01)"; rc=$?
  contains "footprint status: above 16 GB is a problem" "$out" "[!!] нода занимает на диске 21.4 ГБ"
  contains "footprint status: names the limit" "$out" "больше лимита 16 ГБ для ПК класса"
  contains "footprint status: and what to remove" "$out" "docker image prune -a"
  out="$(NSU_LLM_TEST_FOOTPRINT_GB=9.5 inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "footprint status: below the limit is ok" "$out" "[ok] нода занимает на диске 9.5 ГБ из 16 ГБ"
  out="$(inst -Status -Main 127.0.0.1 -Name pc01)"
  contains "footprint status: measured from the install dir" "$out" "[ok] нода занимает на диске 0 ГБ из 16 ГБ"
  # a sparse disk of the distro keeps its length after a model is removed: the limit counts the space on the disk
  mkdir -p "$IDIR/wsl"; sp="$(cygpath -w "$IDIR/wsl/ext4.vhdx")"
  if fsutil file createnew "$sp" 20000000000 >/dev/null 2>&1 && fsutil sparse setflag "$sp" >/dev/null 2>&1 && fsutil sparse setrange "$sp" 0 20000000000 >/dev/null 2>&1; then
    out="$(inst -Status -Main 127.0.0.1 -Name pc01)"
    contains "footprint status: a hole in a sparse disk does not count" "$out" "[ok] нода занимает на диске 0 ГБ из 16 ГБ"
  fi
  rm -f "$IDIR/wsl/ext4.vhdx"
  rm -rf "$IDIR"
  # the sparse disk of the distro
  out="$(full)"
  contains "sparse: an existing distro without the flag gets a step" "$out" "сделать диск дистрибутива NSU-LLM разреженным"
  contains "sparse: the distro is stopped first" "$out" "[dry-run] wsl --terminate NSU-LLM"
  contains "sparse: wsl --manage --set-sparse true" "$out" "[dry-run] wsl --manage NSU-LLM --set-sparse true"
  out="$(full -NoSparse)"
  lacks "sparse: -NoSparse: no set-sparse step" "$out" "--set-sparse"
  mkdir -p "$IDIR"; printf '{"DistroSparse":true}' > "$IDIR/state.json"
  out="$(full)"
  lacks "sparse: already done (state): not repeated" "$out" "--set-sparse"
  rm -rf "$IDIR"
  out="$(FAKE_DISTROS='' FAKE_RUNNING='' full)"
  contains "sparse: a new distro is made sparse right after it is installed" "$out" "сделать диск дистрибутива разреженным"
  pos_inst="$(before "$out" "[dry-run] wsl --install")"; pos_sp="$(before "$out" "[dry-run] wsl --manage")"; pos_run="$(before "$out" "[dry-run] wsl -d NSU-LLM -u root -e true")"
  [ "$pos_inst" -lt "$pos_sp" ] && [ "$pos_sp" -lt "$pos_run" ] && pass "sparse: after the install and before the distro is started" || fail "sparse: order ($pos_inst $pos_sp $pos_run)"
  out="$(FAKE_DISTROS="$DD" full -Docker desktop)"
  lacks "sparse: Docker Desktop has no distro of ours: no set-sparse" "$out" "--set-sparse"
  # the helpers run for real against stubs: the functions are taken from install-node.ps1 itself
  cat > "$TMP/b3fn.ps1" <<'EOS'
param([string]$Script, [string]$Dir, [string]$What)
$ErrorActionPreference = 'Stop'
$t = $null; $e = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$t, [ref]$e)
function Get-FnText([string]$n) {
    $f = $ast.Find({ param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true)
    if (-not $f) { throw "no function $n" }
    return $f.Extent.Text
}
$DryRun = $false
function Show-Dry($x) { Write-Host "[dry-run] $x" }
function Say($x) { Write-Host $x }
function Warn($x) { Write-Host "Внимание: $x" }
function Initialize-InstallDir { }
if ($What -eq 'temp') {
    . ([scriptblock]::Create((Get-FnText 'Remove-InstallerTemp')))
    . ([scriptblock]::Create((Get-FnText 'Invoke-WithInstallerTemp')))
    $TmpDir = Join-Path $Dir 'tmp'; $ImagesDir = Join-Path $Dir 'images'
    New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null; Set-Content (Join-Path $ImagesDir 'stale.tar') 'x'
    Set-Content (Join-Path $Dir 'other.txt') 'keep me'
    $before = $env:TEMP
    $ret = Invoke-WithInstallerTemp { Write-Host "inside: TEMP=$env:TEMP TMP=$env:TMP"; Set-Content (Join-Path $env:TEMP 'wsl-rootfs.tar') 'data'; Write-Host "inside: stale images gone=$(-not (Test-Path (Join-Path $Dir 'images')))"; return 7 }
    Write-Host "ok: returned=$ret tmp exists=$(Test-Path $TmpDir) images exists=$(Test-Path $ImagesDir) temp restored=$($env:TEMP -eq $before) other kept=$(Test-Path (Join-Path $Dir 'other.txt'))"
    try { Invoke-WithInstallerTemp { Set-Content (Join-Path $env:TEMP 'partial.tar') 'data'; New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null; throw 'boom' } } catch { Write-Host "failed: $($_.Exception.Message)" }
    Write-Host "after error: tmp exists=$(Test-Path $TmpDir) images exists=$(Test-Path $ImagesDir) temp restored=$($env:TEMP -eq $before) other kept=$(Test-Path (Join-Path $Dir 'other.txt'))"
    $DryRun = $true
    Invoke-WithInstallerTemp { Write-Host "dry body ran, TEMP unchanged=$($env:TEMP -eq $before)" } | Out-Null
}
elseif ($What -eq 'deltar-unc') {
    . ([scriptblock]::Create((Get-FnText 'Remove-LoadedImageTar')))
    $script:removed = 0
    function Remove-Item { $script:removed++ }
    $ImageTar = '\\srv\share\nsu-llm-node-0.2.1-cpu.tar.gz'
    $DeleteImageTar = [switch]$true
    Remove-LoadedImageTar
    Write-Host "removed=$script:removed"
}
elseif ($What -like 'deltar*') {
    . ([scriptblock]::Create((Get-FnText 'Remove-LoadedImageTar')))
    $ImageTar = Join-Path $Dir 'nsu-llm-node-0.2.1-cpu.tar.gz'
    Set-Content $ImageTar 'img'
    $DeleteImageTar = [switch]($What -ne 'deltar-off')
    if ($What -eq 'deltar-dry') { $DryRun = $true }
    Remove-LoadedImageTar
    Write-Host "archive exists=$(Test-Path $ImageTar)"
}
else {
    . ([scriptblock]::Create((Get-FnText 'Set-DistroSparse')))
    $Distro = 'NSU-LLM'; $MinWslSparse = [version]'2.0.0'; $NoSparse = [switch]$false
    $script:calls = @()
    function Get-WslVersion { return [version]$env:FAKE_WSLVER }
    function Set-State($n, $v) { Write-Host "state $n=$v" }
    function Save-State { }
    function Invoke-Change([string[]]$c, [string]$what, [string]$fix = '', [switch]$AllowFail) {
        Write-Host "run: $($c -join ' ')"
        if ($c -contains '--shutdown') { $script:wslDown = $true }
        if ($c -contains '--set-sparse') {
            switch ($env:FAKE_SPARSE) {
                'ok' { return [pscustomobject]@{ Code = 0; Out = '' } }
                'busy' { if ($c -notcontains '--allow-unsafe') { return [pscustomobject]@{ Code = 1; Out = 'use --allow-unsafe' } }; if ($script:wslDown) { return [pscustomobject]@{ Code = 0; Out = '' } }; return [pscustomobject]@{ Code = -1; Out = 'Wsl/Service/ERROR_SHARING_VIOLATION' } }
                'stuck' { return [pscustomobject]@{ Code = -1; Out = 'Wsl/Service/ERROR_SHARING_VIOLATION' } }
                'unsafe' { if ($c -contains '--allow-unsafe') { return [pscustomobject]@{ Code = 0; Out = '' } }; return [pscustomobject]@{ Code = 1; Out = 'Sparse VHD support is currently disabled due to potential data corruption. To force a distribution to use a sparse vhd, please run: wsl.exe --manage <distro> --set-sparse true --allow-unsafe' } }
                default { return [pscustomobject]@{ Code = 1; Out = 'Invalid command line argument: --set-sparse' } }
            }
        }
        return [pscustomobject]@{ Code = 0; Out = '' }
    }
    if ($What -eq 'dry') { $DryRun = $true }
    if ($What -eq 'nosparse') { $NoSparse = [switch]$true }
    Set-DistroSparse
}
EOS
  fnrun() { PATH="$FB:$PATH" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$TMP/b3fn.ps1")" -Script "$INST" -Dir "$(cygpath -w "$TMP/b3dir")" -What "$1" 2>&1; }
  rm -rf "$TMP/b3dir"; mkdir -p "$TMP/b3dir"
  out="$(fnrun temp)"
  contains "temp: during the steps TEMP and TMP point into the installer's own dir" "$out" "inside: TEMP=$(cygpath -w "$TMP/b3dir")\\tmp TMP=$(cygpath -w "$TMP/b3dir")\\tmp"
  contains "temp: the leftovers of an interrupted run are removed before the steps" "$out" "inside: stale images gone=True"
  contains "temp: the body's result is returned" "$out" "ok: returned=7"
  contains "temp: after a successful run the dir is gone and TEMP is back" "$out" "returned=7 tmp exists=False images exists=False temp restored=True other kept=True"
  contains "temp: after an error the dir is gone, TEMP is back, other files are untouched" "$out" "after error: tmp exists=False images exists=False temp restored=True other kept=True"
  contains "temp: the error still reaches the caller" "$out" "failed: boom"
  contains "temp: a dry run does not move TEMP" "$out" "dry body ran, TEMP unchanged=True"
  out="$(fnrun deltar)"
  contains "image tar: -DeleteImageTar removes the archive right after the load" "$out" "archive exists=False"
  contains "image tar: and says so" "$out" "удалён: образ уже загружен"
  out="$(fnrun deltar-unc)"
  contains "image tar: a network path is never deleted (only the installer's own copy)" "$out" "removed=0"
  contains "image tar: the one-time task gets -DeleteImageTar (a session without credentials runs the steps there)" "$(cat "$NODE/install-node.ps1")" 'if ($DeleteImageTar) { $ta.DeleteImageTar = $true }'
  out="$(fnrun deltar-off)"
  contains "image tar: without -DeleteImageTar a foreign archive is never touched" "$out" "archive exists=True"
  out="$(fnrun deltar-dry)"
  contains "image tar: a dry run deletes nothing" "$out" "archive exists=True"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=ok fnrun real)"
  contains "sparse run: the distro is stopped first" "$out" "run: wsl --terminate NSU-LLM"
  contains "sparse run: set-sparse true" "$out" "run: wsl --manage NSU-LLM --set-sparse true"
  lacks "sparse run: no allow-unsafe when WSL accepts it" "$out" "--allow-unsafe"
  contains "sparse run: remembered in the state" "$out" "state DistroSparse=True"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=unsafe fnrun real)"
  contains "sparse run: WSL asks for --allow-unsafe: repeated with it" "$out" "run: wsl --manage NSU-LLM --set-sparse true --allow-unsafe"
  contains "sparse run: and said so" "$out" "повторяю с ним"
  contains "sparse run: remembered after the retry" "$out" "state DistroSparse=True"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=busy fnrun real)"
  contains "sparse run: the disk is busy after terminate: wsl --shutdown" "$out" "run: wsl --shutdown"
  [ "$(grep -c 'run: wsl --manage NSU-LLM --set-sparse true --allow-unsafe' <<<"$out")" = 2 ] && pass "sparse run: repeated with --allow-unsafe after the shutdown" || fail "sparse run: repeated with --allow-unsafe after the shutdown"
  contains "sparse run: remembered after the shutdown" "$out" "state DistroSparse=True"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=ok fnrun real)"
  lacks "sparse run: no wsl --shutdown when the disk is free" "$out" "wsl --shutdown"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=stuck fnrun real)"
  contains "sparse run: still busy after the shutdown: a warning" "$out" "Вручную: wsl --manage NSU-LLM --set-sparse true"
  lacks "sparse run: still busy: not remembered" "$out" "state DistroSparse"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=fail fnrun real)"
  contains "sparse run: a refusal is a warning with the manual command" "$out" "Вручную: wsl --manage NSU-LLM --set-sparse true"
  lacks "sparse run: a refusal is not remembered" "$out" "state DistroSparse"
  out="$(FAKE_WSLVER=1.2.5.0 FAKE_SPARSE=ok fnrun real)"
  contains "sparse run: an old WSL is skipped with a note" "$out" "не умеет делать диск разреженным"
  lacks "sparse run: an old WSL: no command" "$out" "run: wsl --manage"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=ok fnrun nosparse)"
  contains "sparse run: -NoSparse says it" "$out" "отключён (-NoSparse)"
  lacks "sparse run: -NoSparse runs nothing" "$out" "run:"
  out="$(FAKE_WSLVER=2.6.1.0 FAKE_SPARSE=ok fnrun dry)"
  contains "sparse run: dry run prints both commands" "$out" "run: wsl --terminate NSU-LLM"
  rm -rf "$TMP/b3dir"
  # the copy of an image from a network path is deleted in a finally block, the steps run inside the temp wrapper
  src="$(cat "$NODE/install-node.ps1")"
  contains "temp: the copy from a network path is removed whatever happens" "$src" 'finally { if ($copied) { Remove-Item -LiteralPath $ImagesDir -Recurse -Force'
  contains "temp: the install steps run inside the temp wrapper" "$src" "Invoke-WithInstallerTemp { foreach (\$s in \$steps)"
  contains "image tar: deleted right after docker load when asked (-DeleteImageTar), in both Docker paths" "$(grep -c '^                    Remove-LoadedImageTar' "$NODE/install-node.ps1")" "2"
  out="$(full)"
  contains "temp: the plan says where the temp files go" "$out" "[dry-run] TEMP и TMP шагов установки:"
  contains "images: the old versions of the node image are removed after the node starts" "$out" "[dry-run] wsl -d NSU-LLM -u root -e docker rmi nsu-llm-node:0.2.1-cpu"
  contains "images: dangling layers are pruned" "$out" "docker image prune -f"
  lacks "images: the current image is never removed" "$out" "docker rmi nsu-llm-node:0.2.1-gpu"

  # node-boot.ps1: starts docker, keeps the distro alive, logs
  : > "$TMP/fake.log"
  BOOT="$(cygpath -w "$NODE/node-boot.ps1")"
  out="$(PATH="$FB:$PATH" FAKE_LOG="$(cygpath -w "$TMP/fake.log")" "$PS" -NoProfile -ExecutionPolicy Bypass -File "$BOOT" \
    -Distro NSU-LLM -Log "$(cygpath -w "$TMP/boot.log")" -MaxRuns 1 -PauseSec 0 2>&1)"
  log="$(cat "$TMP/fake.log")"
  contains "boot starts docker" "$log" "wsl -d NSU-LLM -u root -e systemctl start docker"
  contains "boot keeps the distro alive" "$log" "wsl -d NSU-LLM -u root -e sleep infinity"
  contains "boot log written" "$(cat "$TMP/boot.log" 2>/dev/null)" "docker запущен"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; exit 0; else echo "$FAILS FAILED"; exit 1; fi
