# Запускает (или удаляет) контейнер ноды NSU LLM на Windows (Docker Desktop или Docker внутри WSL2).
# Использование:
#   .\run-node.ps1 -Main HOST -Token TOKEN [-Mode k3s|standalone] [-Accel auto|cpu|nvidia]
#                  [-NodeIp IP] [-Name NAME] [-Network bridge|host] [-Image IMAGE] [-DataDir DIR]
#                  [-InsecureTls] [-DryRun] [-Wsl DISTRO] [-AllowFrom ADDR[,ADDR...]]
#   .\run-node.ps1 -Uninstall -Main HOST -Name NAME [-Token TOKEN] [-Network bridge|host] [-Wsl DISTRO]
# -Wsl DISTRO: все команды docker выполняются как `wsl -d DISTRO -e docker ...` (DISTRO = default для
# дистрибутива по умолчанию); сеть по умолчанию host, данные лежат внутри WSL.
# -AllowFrom: правило брандмауэра пускает 18080/18050 только с этих адресов или подсетей (IPv4, CIDR), иначе с любых.
# -WolMac MAC -WolIp IP -WolPrefix N: физическая сетевая карта хоста (install-node.ps1 её находит), нода сообщает её
# порталу для Wake-on-LAN (WOL_MAC, WOL_IP, WOL_PREFIX).
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Main,
    [string]$Token = '',
    [ValidateSet('k3s', 'standalone')][string]$Mode = 'k3s',
    [ValidateSet('auto', 'cpu', 'nvidia')][string]$Accel = 'auto',
    [string]$NodeIp = '',
    [string]$Name = '',
    [ValidateSet('bridge', 'host', '')][string]$Network = '',
    [string]$Image = '',
    [string]$DataDir = '',
    [switch]$InsecureTls,
    [switch]$DryRun,
    [switch]$Uninstall,
    [string]$Wsl = '',
    [string]$AllowFrom = '',
    [string]$WolMac = '',
    [string]$WolIp = '',
    [int]$WolPrefix = 0
)
$ErrorActionPreference = 'Stop'

$Version = '0.1.0'
$Container = 'llm-node'
$UseWsl = $PSBoundParameters.ContainsKey('Wsl') -and $Wsl -ne ''
if (-not $Network) { $Network = $(if ($UseWsl) { 'host' } else { 'bridge' }) }
if (-not $DataDir) { $DataDir = $(if ($UseWsl) { '/var/lib/nsu-llm-node' } else { $(if ($env:NSU_LLM_DATA_DIR) { $env:NSU_LLM_DATA_DIR } else { Join-Path $env:ProgramData 'nsu-llm-node' }) }) }   # NSU_LLM_DATA_DIR: только для тестов (изолированный каталог данных)
if (-not $Name) { $Name = $env:COMPUTERNAME }
$Name = $Name.ToLowerInvariant()
# То же правило, что у портала (метка DNS-1123): имя, которое он отвергает, не зарегистрировать и не удалить.
if ($Name -cnotmatch '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$') { throw "Недопустимое имя ноды: $Name (латинские буквы, цифры и дефис, до 63 символов)" }
if (-not $Uninstall -and -not $Token) { throw 'Параметр -Token обязателен' }
# Список адресов попадает в команду брандмауэра: только цифры, точки, косые черты и запятые.
$AllowFrom = $AllowFrom.Replace(' ', '')
if ($AllowFrom -and $AllowFrom -notmatch '^[0-9./]+(,[0-9./]+)*$') { throw "Недопустимый -AllowFrom: $AllowFrom (адреса IPv4 или подсети через запятую, например 192.168.3.0/24,10.0.0.5)" }
if ($WolMac -and $WolMac -notmatch '^[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}$') { throw "Недопустимый -WolMac: $WolMac (MAC сетевой карты, например 00-1A-2B-3C-4D-5E)" }
$ipOk = $null
$wolIpOk = ($WolIp -match '^\d{1,3}(\.\d{1,3}){3}$') -and [Net.IPAddress]::TryParse($WolIp, [ref]$ipOk)
if ($WolMac -and ((-not $wolIpOk) -or ($WolPrefix -lt 1) -or ($WolPrefix -gt 32))) {
    throw "Недопустимые -WolIp $WolIp или -WolPrefix $WolPrefix (адрес IPv4 сетевой карты и длина префикса её подсети, 1-32)"
}
$MainUrl = "https://$Main"
$Sep = $(if ($UseWsl) { '/' } else { '\' })
function Join-Data([string]$leaf) { return ($DataDir.TrimEnd('/', '\') + $Sep + $leaf.Replace('/', $Sep)) }

function Format-Arg([string]$a) { if ($a -match '[\s"''`;|&<>]') { return "'" + $a.Replace("'", "''") + "'" } else { return $a } }

# Префикс команд WSL. От root: пользователь дистрибутива по умолчанию обычно не в группе docker
# и не может писать в /var/lib/nsu-llm-node.
function Get-WslPrefix {
    $w = @('wsl'); if ($Wsl -ne 'default') { $w += @('-d', $Wsl) }
    return $w + @('-u', 'root')
}
# Команда docker целиком (через WSL при -Wsl).
function Get-DockerCmd([string[]]$dargs) {
    if ($UseWsl) { return (Get-WslPrefix) + @('-e', 'docker') + $dargs }
    return @('docker') + $dargs
}
# Выполняет docker; при -DryRun только печатает команду; ненулевой код завершения прерывает скрипт.
# Простая функция: -f/-v/-d не принимаются за параметры PowerShell.
function Invoke-Docker {
    $full = Get-DockerCmd $args
    if ($DryRun) { Write-Host (($full | ForEach-Object { Format-Arg $_ }) -join ' '); return }
    & $full[0] $full[1..($full.Count - 1)]
    # Только подкоманда: в аргументах docker run есть токен кластера.
    if ($LASTEXITCODE -ne 0) { throw "docker $($args[0]) завершился с кодом $LASTEXITCODE" }
}
# То же без прерывания: для шагов удаления, где объекта может уже не быть.
function Invoke-DockerTry {
    $full = Get-DockerCmd $args
    if ($DryRun) { Write-Host (($full | ForEach-Object { Format-Arg $_ }) -join ' '); return }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $full[0] $full[1..($full.Count - 1)] } finally { $ErrorActionPreference = $prev }
}
function Test-Admin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
$FwRule = 'NSU-LLM-Node'
function Test-Docker {
    $full = Get-DockerCmd $args
    # Windows PowerShell 5.1 превращает stderr docker'а в ошибку, а при Stop она прерывает скрипт.
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $full[0] $full[1..($full.Count - 1)] *> $null } finally { $ErrorActionPreference = $prev }
    return ($LASTEXITCODE -eq 0)
}
function Get-DockerOut {
    $full = Get-DockerCmd $args
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'   # stderr docker'а (нет объекта) не ошибка
    try { return (& $full[0] $full[1..($full.Count - 1)] 2>$null) } finally { $ErrorActionPreference = $prev }
}

# Файловые операции: внутри WSL через bash, иначе нативно.
# Код для bash не содержит двойных кавычек: Windows PowerShell 5.1 разрывает такой аргумент нативной команды.
function Get-ShCmd([string]$script) { return (Get-WslPrefix) + @('-e', 'bash', '-c', $script) }
function Invoke-Sh([string]$script) {
    $w = Get-ShCmd $script
    & $w[0] $w[1..($w.Count - 1)]
}
function Ensure-Dir([string]$path) {
    if ($UseWsl) {
        Invoke-Sh "mkdir -p '$path'"
        if ($LASTEXITCODE -ne 0) { throw "Не удалось создать каталог $path в WSL" }
    }
    elseif (-not (Test-Path $path)) { New-Item -ItemType Directory -Force -Path $path | Out-Null }
}
function Read-Text([string]$path) {
    if ($UseWsl) {
        if ($DryRun) { return '' }   # -DryRun не запускает WSL
        return (Invoke-Sh "cat '$path' 2>/dev/null") -join "`n"
    }
    if (Test-Path $path) { return (Get-Content -Raw -Path $path).TrimEnd() }
    return ''
}
function Write-Text([string]$path, [string]$text) {
    if ($UseWsl) {
        Invoke-Sh "printf '%s\n' '$text' > '$path'"
        if ($LASTEXITCODE -ne 0) { throw "Не удалось записать $path в WSL" }
    }
    else { Set-Content -Path $path -Value $text -NoNewline -Encoding ascii }
}

# ------------------------------------------------------------------ удаление
if ($Uninstall) {
    # Очистка сети хоста запускается из образа ноды: -Image, иначе образ существующего контейнера (читаем до удаления),
    # иначе любой локальный nsu-llm-node.
    if ($Network -eq 'host' -and -not $Image) {
        $Image = ((Get-DockerOut inspect -f '{{.Config.Image}}' $Container) -join '').Trim()
        if (-not $Image) { $Image = ((Get-DockerOut images --format '{{.Repository}}:{{.Tag}}') | Where-Object { $_ -like 'nsu-llm-node:*' } | Select-Object -First 1) }
    }
    Invoke-DockerTry rm -f $Container
    if ($Token) {
        $ca = Join-Data 'state/ca.crt'
        $url = "$MainUrl/api/nodes/$Name"
        # Токен уходит в curl файлом заголовков (как у registrar.sh), а не через argv: командную строку процесса видят все.
        $header = "Authorization: Bearer $Token"
        $hdr = ''
        if ($UseWsl) {
            # CA лежит внутри WSL: DELETE выполняет curl дистрибутива с --cacert, если CA сохранён. Заголовок приходит
            # через stdin в файл /tmp/nsu-llm-delete.<pid> (umask 077, set -C: существующий файл или ссылка не
            # перезаписываются), который bash удаляет при выходе. Ключи TLS собираются в $@ через set --, без подстановки
            # вывода команды: на ноде с WSL 2.7.14 прежний вариант с $(test ... && echo --cacert ...) отдал curl слово
            # echo как адрес. В скрипте нет двойных кавычек: Windows PowerShell 5.1 не экранирует их в аргументах внешних
            # программ (wsl.exe), поэтому $@ без кавычек (пути данных без пробелов).
            $tlsSh = $(if ($InsecureTls) { 'set -- -k' } else { "set --; if [ -s '$ca' ]; then set -- --cacert '$ca'; fi" })
            $del = Get-ShCmd ("$tlsSh; umask 077; set -C; h=/tmp/nsu-llm-delete.`$`$; trap 'rm -f `$h' EXIT; " +
                "sed -e '1s/^\xef\xbb\xbf//' -e 's/\r`$//' > `$h || exit 1; curl -X DELETE -sS -o /dev/null -w '%{http_code}' -m 15 `$@ -H @`$h '$url'")
        }
        else {
            $hdr = $(if ($DryRun) { '<файл заголовков>' } else { [IO.Path]::GetTempFileName() })
            $del = @('curl.exe', '-X', 'DELETE', '-sS', '-o', 'NUL', '-w', '%{http_code}', '-m', '15')
            if ($InsecureTls) { $del += '-k' } elseif (Test-Path $ca) {
                # curl.exe (Schannel) cannot check revocation for the portal's private CA and fails without --ssl-no-revoke.
                $del += @('--cacert', $ca, '--ssl-no-revoke')
            }
            $del += @('-H', "@$hdr", $url)
        }
        if ($DryRun) { Write-Host (($del | ForEach-Object { Format-Arg $_ }) -join ' ') }
        else {
            $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try {
                if ($UseWsl) {
                    # Кодировка stdin внешней программы: без BOM (с UTF-8-BOM заголовок начинался бы с U+FEFF и портал
                    # ответил бы 401); sed в скрипте снимает BOM на случай, если консоль добавит его сама.
                    $prevEnc = $OutputEncoding; $OutputEncoding = New-Object System.Text.UTF8Encoding $false
                    try { $out = ($header | & $del[0] $del[1..($del.Count - 1)] 2>&1) -join ' ' } finally { $OutputEncoding = $prevEnc }
                }
                else {
                    # Временный каталог пользователя: файл читает только он, удаляется сразу после запроса.
                    Set-Content -Path $hdr -Value $header -NoNewline -Encoding ascii
                    $out = (& $del[0] $del[1..($del.Count - 1)] 2>&1) -join ' '
                }
            }
            finally {
                $ErrorActionPreference = $prev
                if ($hdr) { Remove-Item -Force -Path $hdr -ErrorAction SilentlyContinue }
            }
            # 204 — удалена, 404 — её уже нет: оба исхода годятся; иначе (нет связи, 401, 5xx) объект ноды остаётся.
            if ($LASTEXITCODE -ne 0 -or ($out.Trim() -notin @('204', '404'))) {
                Write-Warning "DELETE $MainUrl/api/nodes/$Name не выполнен (curl $LASTEXITCODE, ответ: $out). Нода останется в кластере: удалите её на главном сервере (docker exec llm-main k3s kubectl delete node $Name)."
            }
        }
    }
    Invoke-DockerTry volume rm llm-node-k3s llm-node-kubelet llm-models
    if ($Network -eq 'host' -and -not $Image) {
        Write-Warning 'Образ ноды не найден локально, очистка интерфейсов (cni0, flannel.1) и цепочек iptables (KUBE-*, CNI-*, FLANNEL-*) хоста пропущена. Загрузите образ и повторите -Uninstall -Network host -Image IMAGE.'
    }
    elseif ($Network -eq 'host') {
        # Без двойных кавычек (см. Get-ShCmd): имена интерфейсов не содержат пробелов.
        $cleanup = 'ip link show | grep ''master cni0'' | while read -r _ i _; do ip link delete ${i%%@*}; done; ' +
        'for l in cni0 flannel.1 flannel-v6.1 kube-ipvs0 flannel-wg flannel-wg-v6; do ip link delete $l 2>/dev/null || true; done; ' +
        'iptables-save | grep -v KUBE- | grep -v CNI- | grep -iv flannel | iptables-restore; ' +
        'ip6tables-save | grep -v KUBE- | grep -v CNI- | grep -iv flannel | ip6tables-restore || true'
        Invoke-DockerTry run --rm --pull never --privileged --network host --entrypoint bash $Image -c $cleanup
    }
    if ($DryRun) { Write-Host "Remove-Item -Recurse -Force $DataDir" }
    elseif ($UseWsl) { Invoke-Sh "rm -rf '$DataDir'" }
    elseif (Test-Path $DataDir) { Remove-Item -Recurse -Force $DataDir }
    if ($DryRun) { Write-Host "Remove-NetFirewallRule -DisplayName $FwRule" }
    elseif (Get-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue) {
        if (Test-Admin) { Remove-NetFirewallRule -DisplayName $FwRule; Write-Host "Правило брандмауэра $FwRule удалено." }
        else { Write-Host "Удалите правило брандмауэра, выполнив в PowerShell от имени администратора:`n  Remove-NetFirewallRule -DisplayName $FwRule" }
    }
    if (-not $DryRun) { Write-Host "Нода $Name удалена." }
    return
}

# ------------------------------------------------------------------ установка
if (-not $DryRun -and -not (Test-Docker info)) { throw 'docker недоступен (docker info завершился ошибкой)' }

# GPU проверяется локальным образом ноды (без обращения к реестру): nvidia-smi в контейнер монтирует nvidia-container-toolkit.
$GpuImage = $(if ($Image) { $Image } else { "nsu-llm-node:$Version-gpu" })
function Test-Gpu { return (Test-Docker run --rm --pull never --gpus all --entrypoint nvidia-smi $GpuImage) }
if ($Accel -eq 'auto') {
    if ($DryRun) { $Accel = 'cpu' }
    elseif (-not (Test-Docker image inspect $GpuImage)) { Write-Host "Образ $GpuImage не загружен: GPU не проверяется, нода запускается как cpu."; $Accel = 'cpu' }
    elseif (Test-Gpu) { $Accel = 'nvidia' } else { $Accel = 'cpu' }
}
elseif ($Accel -eq 'nvidia' -and -not $DryRun -and -not (Test-Gpu)) {
    throw "GPU недоступен: docker run --rm --pull never --gpus all --entrypoint nvidia-smi $GpuImage завершился ошибкой (образ должен быть загружен)"
}
if (-not $Image) { $Image = "nsu-llm-node:$Version-" + $(if ($Accel -eq 'nvidia') { 'gpu' } else { 'cpu' }) }

if (-not $NodeIp) {
    try {
        $addr = [System.Net.Dns]::GetHostAddresses($Main) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
        $NodeIp = (Find-NetRoute -RemoteIPAddress $addr.IPAddressToString | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
    } catch { }
    if (-not $NodeIp) { throw 'Не удалось определить адрес этого хоста, укажите -NodeIp' }
}

# Метки k3s применяются только при первой регистрации ноды.
$Labels = "llm.nsu/accel=$Accel"
if ($Accel -eq 'nvidia' -and -not $DryRun) {
    $vram = (Get-DockerOut run --rm --pull never --gpus all --entrypoint nvidia-smi $GpuImage --query-gpu=memory.total '--format=csv,noheader,nounits' -i 0 | Select-Object -First 1)
    if ($vram) { $Labels += "`nllm.nsu/vram-mib=" + $vram.Trim() }
}
$StateDir = Join-Data 'state'
$AirgapDir = Join-Data 'airgap'
if ($Mode -eq 'k3s') {
    $old = Read-Text (Join-Data 'state/labels')
    if ($old -and ($old.Replace("`r", '') -ne $Labels)) { throw 'Метки ноды изменились — выполните -Uninstall и запустите снова' }
}

if (-not $DryRun) {
    if (-not (Test-Docker image inspect $Image)) { throw "Образ $Image не найден (docker load или docker build)" }
    Ensure-Dir $StateDir; Ensure-Dir $AirgapDir
    # CA портала один раз (trust on first use), как run-node.sh: по нему -Uninstall проверяет сервер при DELETE.
    $ca = Join-Data 'state/ca.crt'
    if ($UseWsl -and -not $InsecureTls) {
        # Внутри WSL: там же его читает DELETE при -Uninstall. Нет curl в дистрибутиве — CA не сохранится (системное хранилище).
        Invoke-Sh "test -s '$ca' || { curl -sSf -k --connect-timeout 5 -m 15 -o '$ca.tmp' '$MainUrl/ca.crt' 2>/dev/null && mv '$ca.tmp' '$ca' || rm -f '$ca.tmp'; }" | Out-Null
    }
    elseif (-not $InsecureTls -and -not (Test-Path $ca)) {
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & curl.exe -sSf -k --connect-timeout 5 -m 15 -o "$ca.tmp" "$MainUrl/ca.crt" 2>$null } finally { $ErrorActionPreference = $prev }
        if ($LASTEXITCODE -eq 0 -and (Test-Path "$ca.tmp")) { Move-Item -Force "$ca.tmp" $ca } else { Remove-Item -Force "$ca.tmp" -ErrorAction SilentlyContinue }
    }
    if ($Mode -eq 'k3s') { Write-Text (Join-Data 'state/labels') $Labels }
}

if ($Mode -eq 'k3s') {
    $tar = Join-Data 'airgap/node.tar'
    $idFile = Join-Data 'airgap/node.id'
    $id = ''
    if (-not $DryRun) { $id = ((Get-DockerOut image inspect -f '{{.Id}}' $Image) -join '').Trim() }
    if ($DryRun -or ((Read-Text $idFile) -ne $id)) {
        Invoke-Docker save --platform linux/amd64 $Image -o $tar
        if (-not $DryRun) { Write-Text $idFile $id }
    }
}

if (-not $DryRun -and (Test-Docker inspect $Container)) { Invoke-Docker rm -f $Container | Out-Null }

$run = @('run', '-d', '--name', $Container, '--hostname', $Name, '--restart', 'unless-stopped', '--stop-timeout', '45',
    '-v', 'llm-models:/var/lib/llm-models', '-v', "${StateDir}:/var/lib/llm-node")
if ($Mode -eq 'k3s') {
    $run += @('--privileged', '--tmpfs', '/run', '--tmpfs', '/var/run', '--ulimit', 'nproc=65535', '--ulimit', 'nofile=65535:65535',
        '-v', 'llm-node-k3s:/var/lib/rancher/k3s', '-v', 'llm-node-kubelet:/var/lib/kubelet',
        '-v', "${AirgapDir}:/var/lib/rancher/k3s/agent/images")
}
if ($Network -eq 'host') { $run += @('--network', 'host') }
else {
    # 18050 (RPC режима split) без аутентификации: он нужен только ноде k3s, standalone его не публикует
    $run += @('-p', '18080:18080')
    if ($Mode -eq 'k3s') { $run += @('-p', '18050:18050') }
}
if ($Accel -eq 'nvidia') {
    $run += @('--gpus', 'all')
    # Docker Desktop gives the container libcuda and /dev/dxg but not /usr/lib/wsl/lib (libdxcore, the WSL NVML): without
    # them the k3s pods inside (nvidia-device-plugin, llama) get NVML "Not Supported". Docker CE in WSL mounts it itself.
    if (-not $UseWsl) { $run += @('-v', '/usr/lib/wsl:/usr/lib/wsl:ro') }
}
$run += @('-e', "MODE=$Mode", '-e', "MAIN_HOST=$Main", '-e', 'CLUSTER_TOKEN', '-e', "NODE_NAME=$Name",
    '-e', "NODE_IP=$NodeIp", '-e', "ACCEL=$Accel")
if ($InsecureTls) { $run += @('-e', 'MAIN_INSECURE_TLS=1') }
# Wake-on-LAN: сетевая карта хоста (не секрет), нода сообщает её порталу.
if ($WolMac) { $run += @('-e', "WOL_MAC=$WolMac", '-e', "WOL_IP=$WolIp", '-e', "WOL_PREFIX=$WolPrefix") }
# Память, которую Docker даёт ноде (в WSL — память WSL): метка llm.nsu/ram-mib для размещения split.
if (-not $DryRun) { $mem = ((Get-DockerOut info --format '{{.MemTotal}}') -join '').Trim(); if ($mem -match '^\d+$') { $run += @('-e', "RAM_MIB=$([math]::Floor([int64]$mem / 1MB))") } }
# Как run-node.sh: HF_ENDPOINT, HF_TOKEN, THREADS из окружения скрипта передаются в контейнер; прокси тоже
# (обе формы имени: curl читает только http_proxy в нижнем регистре, Go — любую).
$passEnv = @('CLUSTER_TOKEN') + @('HF_ENDPOINT', 'HF_TOKEN', 'THREADS', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy' |
    Where-Object { [Environment]::GetEnvironmentVariable($_) })
foreach ($v in $passEnv) { if ($v -ne 'CLUSTER_TOKEN') { $run += @('-e', $v) } }
$run += $Image
# Токен попадает в docker через окружение (-e ИМЯ), а не через командную строку; в WSL его переносит WSLENV.
$prevToken = $env:CLUSTER_TOKEN; $prevWslEnv = $env:WSLENV
try {
    $env:CLUSTER_TOKEN = $Token
    if ($UseWsl) { $env:WSLENV = ((@($env:WSLENV -split ':' | Where-Object { $_ }) + ($passEnv | ForEach-Object { "$_/u" })) -join ':') }
    Invoke-Docker @run
}
finally { $env:CLUSTER_TOKEN = $prevToken; $env:WSLENV = $prevWslEnv }

# Брандмауэр Windows: входящие 18080 (инференс) и, только в режиме k3s, 18050 (RPC без аутентификации).
$fwPorts = $(if ($Mode -eq 'k3s') { '18080,18050' } else { '18080' })
# Windows хранит подсеть как 10.1.2.0/255.255.255.0: для сравнения приводим к виду 10.1.2.0/24.
function ConvertTo-Cidr([string]$a) {
    if ($a -match '^(.+)/(\d+\.\d+\.\d+\.\d+)$') {
        $bits = (([Net.IPAddress]$matches[2]).GetAddressBytes() | ForEach-Object { [Convert]::ToString($_, 2) }) -join ''
        return $matches[1] + '/' + ($bits -replace '0', '').Length
    }
    return $a
}
$fwRemote = $(if ($AllowFrom) { " -RemoteAddress $AllowFrom" } else { '' })
$fwCmd = "New-NetFirewallRule -DisplayName $FwRule -Direction Inbound -Protocol TCP -LocalPort $fwPorts$fwRemote -Action Allow"
$fwDel = "Remove-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue"
if ($DryRun) { Write-Host "$fwDel; $fwCmd" }
else {
    # Порты и адреса правила сверяются при каждом запуске: смена режима (k3s/standalone) меняет набор портов.
    $rule = Get-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue
    $have = ''
    if ($rule) {
        $have = ((@($rule | Get-NetFirewallPortFilter | ForEach-Object { $_.LocalPort }) | Sort-Object -Unique) -join ',') + '|' +
        ((@($rule | Get-NetFirewallAddressFilter | ForEach-Object { $_.RemoteAddress } | ForEach-Object { ConvertTo-Cidr $_ }) | Sort-Object -Unique) -join ',')
    }
    $want = ((($fwPorts -split ',') | Sort-Object -Unique) -join ',') + '|' + $(if ($AllowFrom) { (($AllowFrom -split ',') | Sort-Object -Unique) -join ',' } else { 'Any' })
    if ($have -ne $want) {
        if (Test-Admin) { Invoke-Expression $fwDel; Invoke-Expression $fwCmd | Out-Null; Write-Host "Правило брандмауэра $FwRule открывает TCP $fwPorts." }
        elseif ($rule) { Write-Host "Обновите порты, выполнив в PowerShell от имени администратора:`n  $fwDel; $fwCmd" }
        else { Write-Host "Откройте порты, выполнив в PowerShell от имени администратора:`n  $fwCmd" }
    }
    Write-Host "Нода $Name запущена: контейнер $Container, образ $Image, режим $Mode, адрес $NodeIp."
}
