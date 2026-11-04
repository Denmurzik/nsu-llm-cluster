# Запускает (или удаляет) контейнер главного сервера NSU LLM Cluster на Windows (Docker Desktop, WSL2).
# Использование:
#   .\run-main.ps1 [-EnvFile FILE] [-AdvertiseIp IP] [-Airgap] [-AirgapDir DIR] [-Certs DIR] [-Image IMAGE] [-DryRun]
#   .\run-main.ps1 -Uninstall [-DryRun]
#   .\run-main.ps1 -PrintToken [-DryRun]
# -EnvFile      файл с переменными (OIDC_CLIENT_SECRET, PUBLIC_HOSTNAME, ADMIN_EMAILS, ...), по умолчанию .env
# -AdvertiseIp  LAN-адрес этого компьютера; по умолчанию MAIN_ADVERTISE_IP из env-файла, иначе определяется сам
# -Airgap       положить в каталог образов k3s системные образы k3s (архив релиза, sha256 проверяется), postgres, redis
#               и LiteLLM: главный стартует без выхода в интернет
# -Certs        каталог с tls.crt, tls.key (и необязательно ca.crt): свой сертификат вместо внутреннего CA Caddy
# -Uninstall    удалить контейнер и тома llm-main-k3s, llm-main-kubelet, llm-main-data, llm-main-hf, llm-main-registry (данные, секреты, кэш моделей и реестр образов теряются)
# -PrintToken   напечатать токен подключения нод (токен агента k3s, для run-node -Token) и выйти
[CmdletBinding()]
param(
    [string]$EnvFile = '.env',
    [string]$AdvertiseIp = '',
    [switch]$Airgap,
    [string]$AirgapDir = '',
    [string]$Certs = '',
    [string]$Image = '',
    [switch]$Uninstall,
    [switch]$PrintToken,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

$Version = '0.1.0'
$Container = 'llm-main'
if (-not $Image) { $Image = "nsu-llm-main:$Version" }
if (-not $AirgapDir) { $AirgapDir = Join-Path $env:ProgramData 'nsu-llm-main\images' }
$AirgapImages = @('postgres:17-alpine', 'redis:7-alpine', 'ghcr.io/berriai/litellm:v1.106.6', 'registry:2.8.3')
# версия k3s в образе (ARG K3S_VERSION в main/Dockerfile): системные образы (pause, coredns, local-path, metrics-server)
# берутся из архива того же релиза
$K3sVersion = 'v1.36.5+k3s1'
$K3sReleaseUrl = $env:NSU_LLM_K3S_RELEASE_URL
if (-not $K3sReleaseUrl) { $K3sReleaseUrl = 'https://github.com/k3s-io/k3s/releases/download/' + $K3sVersion.Replace('+', '%2B') }
$K3sAirgapFile = 'k3s-airgap-images-amd64.tar.zst'
$FwName = 'NSU-LLM-Main'
$FwCmd = "New-NetFirewallRule -DisplayName $FwName -Direction Inbound -Protocol TCP -LocalPort 443,80,6443 -Action Allow"

function Format-Arg([string]$a) { if ($a -match '[\s"''`;|&<>]') { return "'" + $a.Replace("'", "''") + "'" } else { return $a } }
# Выполняет docker; при -DryRun только печатает команду. Простая функция: -f/-v/-d не принимаются за параметры PowerShell.
# Ненулевой код docker прерывает скрипт: иначе после неудачного pull/save/run он сообщил бы об успехе.
function Invoke-Docker {
    if ($DryRun) { Write-Host ('docker ' + (($args | ForEach-Object { Format-Arg $_ }) -join ' ')); return }
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Команда docker $($args[0]) завершилась с кодом $LASTEXITCODE (сообщение docker выше)" }
}
function Test-Docker {
    # "no such object" и подобное docker пишет в stderr: под $ErrorActionPreference = 'Stop' это было бы фатально
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & docker @args *> $null } finally { $ErrorActionPreference = $prev }
    return ($LASTEXITCODE -eq 0)
}

# ------------------------------------------------------------------ токен подключения нод
# Токен агента k3s из контейнера (agent-token.sh; серверный токен k3s он не печатает). Пока k3s не записал токен,
# попытка повторяется, не дольше NSU_LLM_TOKEN_WAIT_S секунд (по умолчанию 120: первый запуск k3s занимает десятки секунд).
$TokenWaitS = 120
if ($env:NSU_LLM_TOKEN_WAIT_S) { $TokenWaitS = [int]$env:NSU_LLM_TOKEN_WAIT_S }
function Get-AgentToken {
    $deadline = (Get-Date).AddSeconds($TokenWaitS)
    while ($true) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $t = (& docker exec $Container /opt/llm/main/agent-token.sh 2>$null | Select-Object -First 1) } finally { $ErrorActionPreference = $prev }
        if ($LASTEXITCODE -eq 0 -and $t) { return "$t".Trim() }
        if ((Get-Date) -ge $deadline) { return $null }
        Start-Sleep -Seconds 3
    }
}
function Write-AgentToken([string]$mainIp) {
    Write-Host "Ожидание токена подключения нод (k3s запускается, до $TokenWaitS с)..."
    $token = Get-AgentToken
    if ($token) {
        Write-Host "Токен подключения нод (токен агента k3s, храните его как пароль): $token"
        Write-Host "  .\node\run-node.ps1 -Main $mainIp -Token $token"
    } else {
        Write-Host 'Токен подключения нод пока не готов: выполните позже .\run-main.ps1 -PrintToken или откройте раздел «Ноды» в интерфейсе.'
    }
}
if ($PrintToken) {
    if ($DryRun) { Invoke-Docker exec $Container /opt/llm/main/agent-token.sh; return }
    $token = Get-AgentToken
    if (-not $token) {
        throw "Токен подключения нод не получен: контейнер $Container не запущен или k3s ещё стартует (docker logs $Container), повторите через минуту"
    }
    Write-Output $token
    return
}

# ------------------------------------------------------------------ удаление
if ($Uninstall) {
    if ($DryRun -or (Test-Docker inspect $Container)) { Invoke-Docker rm -f $Container }
    try { Invoke-Docker volume rm llm-main-k3s llm-main-kubelet llm-main-data llm-main-hf llm-main-registry } catch { Write-Warning "Не все тома llm-main-* удалены: $_" }
    if (-not $DryRun) { Write-Host "Главный сервер удалён: контейнер и тома llm-main-*. Каталог образов $AirgapDir не тронут." }
    return
}

# ------------------------------------------------------------------ проверки
if (-not $DryRun) {
    if (-not (Test-Docker info)) { throw 'docker недоступен (docker info завершился ошибкой): запустите Docker Desktop' }
    $memTotal = [int64]((& docker info --format '{{.MemTotal}}') | Select-Object -First 1)
    if ($memTotal -lt 4GB) {
        throw ("Docker выделено {0:N1} ГиБ памяти, нужно не менее 4. Увеличьте memory в %UserProfile%\.wslconfig " -f ($memTotal / 1GB) +
            "([wsl2] memory=8GB), выполните wsl --shutdown и перезапустите Docker Desktop.")
    }
    if (-not (Test-Path $EnvFile)) { throw "Файл $EnvFile не найден: создайте его (обязательны OIDC_CLIENT_SECRET и PUBLIC_HOSTNAME)" }
    if (-not (Test-Docker image inspect $Image)) { throw "Образ $Image не найден (docker build -f main/Dockerfile -t $Image . или docker load)" }
}
$EnvFileFull = $EnvFile
if (Test-Path $EnvFile) { $EnvFileFull = (Resolve-Path $EnvFile).Path }

# Адрес: параметр, затем env-файл (передаётся контейнеру через --env-file), затем автоопределение.
$envHasIp = (Test-Path $EnvFile) -and [bool](Select-String -Path $EnvFile -Pattern '^MAIN_ADVERTISE_IP=\S' -Quiet)
if (-not $AdvertiseIp -and -not $envHasIp) {
    # Физический адаптер со шлюзом: маршрут по умолчанию может вести в виртуальный адаптер VPN/прокси.
    try {
        $cfg = Get-NetIPConfiguration | Where-Object {
            $_.IPv4DefaultGateway -and $_.IPv4DefaultGateway.NextHop -ne '0.0.0.0' -and $_.NetAdapter.Status -eq 'Up' -and -not $_.NetAdapter.Virtual
        } | Select-Object -First 1
        if ($cfg) { $AdvertiseIp = ($cfg.IPv4Address | Select-Object -First 1).IPAddress }
        if (-not $AdvertiseIp) {
            $AdvertiseIp = (Find-NetRoute -RemoteIPAddress 1.1.1.1 | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
        }
    } catch { }
    if (-not $AdvertiseIp) { throw 'Не удалось определить адрес этого компьютера, укажите -AdvertiseIp' }
}

# ------------------------------------------------------------------ офлайн-образы
# До пересоздания контейнера: неудачная загрузка не оставляет главный остановленным.
# SHA-256 через .NET: Get-FileHash есть не в каждом окружении Windows PowerShell 5.1
function Get-Sha256([string]$path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead((Resolve-Path -LiteralPath $path).ProviderPath)
    try { return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) }
    finally { $stream.Dispose(); $sha.Dispose() }
}
# Архив системных образов k3s: скачивается, если его нет или он не совпадает с sha256sum-amd64.txt релиза.
function Get-K3sAirgapImages {
    $dest = Join-Path $AirgapDir $K3sAirgapFile
    $sumsUrl = "$K3sReleaseUrl/sha256sum-amd64.txt"
    if ($DryRun) {
        Write-Host "curl.exe -fL --retry 3 -o $(Format-Arg $dest) $K3sReleaseUrl/$K3sAirgapFile"
        Write-Host "# sha256 сверяется с $sumsUrl"
        return
    }
    $sumsFile = Join-Path $AirgapDir 'k3s-sha256sum-amd64.download'
    & curl.exe -fsSL --retry 3 -o $sumsFile $sumsUrl
    if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать контрольные суммы k3s $K3sVersion ($sumsUrl)" }
    $want = ''
    foreach ($line in (Get-Content -LiteralPath $sumsFile)) {
        $f = $line -split '\s+'
        if ($f.Count -ge 2 -and $f[1] -eq $K3sAirgapFile) { $want = $f[0].ToLowerInvariant() }
    }
    Remove-Item -LiteralPath $sumsFile -Force
    if (-not $want) { throw "В sha256sum-amd64.txt k3s $K3sVersion нет строки для $K3sAirgapFile" }
    if ((Test-Path -LiteralPath $dest) -and ((Get-Sha256 $dest) -eq $want)) {
        Write-Host "Системные образы k3s $K3sVersion уже скачаны: $dest"
        return
    }
    $part = "$dest.part"
    & curl.exe -fL --retry 3 -o $part "$K3sReleaseUrl/$K3sAirgapFile"
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
        throw "Не удалось скачать $K3sReleaseUrl/$K3sAirgapFile"
    }
    $got = Get-Sha256 $part
    if ($got -ne $want) {
        Remove-Item -LiteralPath $part -Force
        throw "Контрольная сумма $K3sAirgapFile не совпала (ожидалась $want, получена $got): архив повреждён или подменён, повторите -Airgap"
    }
    Move-Item -LiteralPath $part -Destination $dest -Force
}
if ($Airgap) {
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $AirgapDir | Out-Null }
    Get-K3sAirgapImages
    foreach ($img in $AirgapImages) {
        $file = Join-Path $AirgapDir (($img -replace '[/:]', '_') + '.tar')
        Invoke-Docker pull --platform linux/amd64 $img
        Invoke-Docker save --platform linux/amd64 -o $file $img
    }
}

# Порты хоста должны быть свободны (контейнер llm-main, если он уже есть, будет пересоздан).
if (-not $DryRun) {
    if (Test-Docker inspect $Container) { Invoke-Docker rm -f $Container | Out-Null }
    $busy = @(Get-NetTCPConnection -State Listen -LocalPort 80, 443, 6443 -ErrorAction SilentlyContinue)
    if ($busy.Count -gt 0) {
        $ports = ($busy | ForEach-Object { $_.LocalPort } | Sort-Object -Unique) -join ', '
        throw "Порты $ports на этом компьютере уже заняты: освободите их и повторите запуск."
    }
}

# ------------------------------------------------------------------ запуск
# Логи идут в stdout контейнера: Docker хранит 5 файлов по 50 МБ, старые удаляются.
# nofile: при мягком лимите 1024 дескрипторов 1000 пользователей упираются в EMFILE (Caddy, LiteLLM)
$run = @('run', '-d', '--name', $Container, '--hostname', 'llm-main', '--privileged', '--restart', 'unless-stopped',
    '--ulimit', 'nofile=1048576:1048576',
    '--log-driver', 'json-file', '--log-opt', 'max-size=50m', '--log-opt', 'max-file=5',
    '--tmpfs', '/run', '--tmpfs', '/var/run',
    '-p', '443:443', '-p', '80:80', '-p', '6443:6443',
    '-v', 'llm-main-k3s:/var/lib/rancher/k3s', '-v', 'llm-main-kubelet:/var/lib/kubelet', '-v', 'llm-main-data:/var/lib/llm',
    '-v', 'llm-main-hf:/var/lib/llm-hf', '-v', 'llm-main-registry:/var/lib/llm-registry',
    '--env-file', $EnvFileFull)
if ($Airgap -or (Test-Path (Join-Path $AirgapDir '*.tar')) -or (Test-Path (Join-Path $AirgapDir '*.tar.zst'))) { $run += @('-v', "${AirgapDir}:/var/lib/rancher/k3s/agent/images:ro") }
if ($Certs) { $run += @('-v', ((Resolve-Path $Certs).Path + ':/certs:ro'), '-e', 'TLS_CERT=1') }
if ($AdvertiseIp) { $run += @('-e', "MAIN_ADVERTISE_IP=$AdvertiseIp") }
$run += $Image
Invoke-Docker @run

# Брандмауэр Windows: входящие 443, 80 и 6443 (ноды подключаются к 6443).
if ($DryRun) { Write-Host $FwCmd; return }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not [bool](Get-NetFirewallRule -DisplayName $FwName -ErrorAction SilentlyContinue)) {
    if ($isAdmin) { Invoke-Expression $FwCmd | Out-Null; Write-Host "Правило брандмауэра $FwName создано." }
    else { Write-Host "Откройте порты, выполнив в PowerShell от имени администратора:`n  $FwCmd" }
}
Write-Host "Главный сервер запущен: контейнер $Container, образ $Image."
Write-Host 'Первый запуск занимает несколько минут (k3s скачивает образы postgres и LiteLLM). Проверка:'
$shownIp = $AdvertiseIp
if (-not $shownIp -and (Test-Path $EnvFile)) {
    $line = Select-String -Path $EnvFile -Pattern '^MAIN_ADVERTISE_IP=(\S+)' | Select-Object -Last 1
    if ($line) { $shownIp = $line.Matches[0].Groups[1].Value }
}
Write-Host "  curl.exe -sk https://$shownIp/readyz"
Write-AgentToken $shownIp
