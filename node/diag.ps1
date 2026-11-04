# Диагностика сети ПК класса (Windows) для NSU LLM Cluster. Ничего не меняет.
# Использование (PowerShell, лучше от администратора: так видны все правила брандмауэра):
#   .\diag.ps1 [-Main ИМЯ_ИЛИ_АДРЕС_ГЛАВНОГО] [-NonInteractive] [-Config FILE] [-Stub FILE]
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\diag.ps1 -NonInteractive -Main llm.nsu.ru
#
# Что проверяется (у каждого провала печатаются причина и готовая команда исправления):
#   1. DNS: имя главного разрешается в адрес;
#   2. 443/tcp и 6443/tcp главного доступны с этого ПК;
#   3. ca.crt скачивается, сертификат главного принят системой этого ПК;
#   4. эндпоинт регистрации нод (/api/nodes/register) отвечает;
#   5. локальный брандмауэр Windows (действующие правила, вместе с групповой политикой): входящие 18080/tcp (инференс)
#      и 18050/tcp (RPC split) разрешены, запрещающих правил нет;
#   6. по тому, где работает контейнер llm-node: для ноды в WSL версия WSL, networkingMode=mirrored в .wslconfig и в
#      работающем WSL, правило Hyper-V firewall, имя главного и 6443/tcp изнутри WSL; для Docker Desktop эти проверки не нужны.
# Проверка 6 запускает команды в WSL (wslinfo, getent, bash): остановленный WSL при этом стартует.
#
# Откуда берётся -Main (первое найденное): параметр, файл конфигурации, контейнер llm-node на этом ПК. Недостающее
# спрашивается и записывается в файл конфигурации (по умолчанию %ProgramData%\nsu-llm-node\diag.json), повторный запуск
# идёт без вопросов. -NonInteractive (-Yes): без вопросов; если значения нет, код выхода 2.
# -Stub FILE: ответы системы берутся из JSON-файла вместо настоящих проверок (для тестов, см. node/tests/test_diag.sh).
# Коды выхода: 0 провалов нет (предупреждения допустимы), 1 есть провалы, 2 ошибка параметров.
[CmdletBinding()]
param(
    [string]$Main = '',
    [Alias('Yes')][switch]$NonInteractive,
    [string]$Config = '',
    [string]$Stub = '',
    [int]$TimeoutSec = 4
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$S = $null
if ($Stub) { $S = Get-Content -Raw -Encoding UTF8 -LiteralPath $Stub | ConvertFrom-Json }
if (-not $Config) { $Config = Join-Path $(if ($env:ProgramData) { $env:ProgramData } else { [IO.Path]::GetTempPath() }) 'nsu-llm-node\diag.json' }
$HyperVCreator = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'

# ------------------------------------------------------------------ ответы заглушки
function Get-Stub([string]$path) { # путь через точки: 'wsl.version'
    $o = $S
    foreach ($p in $path.Split('.')) {
        if ($null -eq $o) { return $null }
        $m = $o.PSObject.Properties[$p]
        if ($null -eq $m) { return $null }
        $o = $m.Value
    }
    return $o
}
function Get-StubKey([string]$section, [string]$key) { # раздел с произвольными ключами ("10.0.0.5:443")
    $o = Get-Stub $section
    if ($null -eq $o) { return $null }
    $m = $o.PSObject.Properties[$key]
    if ($null -eq $m) { return $null }
    return $m.Value
}

# ------------------------------------------------------------------ вывод
$script:nOk = 0; $script:nFail = 0; $script:nWarn = 0
function Write-Detail([string[]]$lines) { foreach ($l in $lines) { Write-Host "       $l" } }
function Write-Ok([string]$msg) { $script:nOk++; Write-Host "[ OK ] $msg" }
function Write-Info([string]$msg, [string[]]$lines = @()) { Write-Host "[ -- ] $msg"; Write-Detail $lines }
function Write-Bad([string]$msg, [string[]]$lines = @()) { $script:nFail++; Write-Host "[СБОЙ] $msg"; Write-Detail $lines }
function Write-Warn([string]$msg, [string[]]$lines = @()) { $script:nWarn++; Write-Host "[ВНИМ] $msg"; Write-Detail $lines }

# ------------------------------------------------------------------ примитивы (настоящие или из заглушки)
function Resolve-Ipv4([string]$name) {
    if ($S) { $v = Get-StubKey 'dns' $name; if ($null -eq $v) { return @() }; return @($v) }
    try {
        return @([System.Net.Dns]::GetHostAddresses($name) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | ForEach-Object { $_.ToString() })
    } catch { return @() }
}

# open | refused (сеть доходит, порт никто не слушает) | blocked (таймаут или нет маршрута)
function Test-Port([string]$ip, [int]$port) {
    if ($S) { $v = Get-StubKey 'tcp' "${ip}:${port}"; if ($null -eq $v) { return 'blocked' }; return [string]$v }
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect($ip, $port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne($TimeoutSec * 1000)) { return 'blocked' }
        try { $c.EndConnect($ar); return 'open' }
        catch [System.Net.Sockets.SocketException] {
            if ($_.Exception.SocketErrorCode -eq 'ConnectionRefused') { return 'refused' }
            return 'blocked'
        }
    } catch { return 'blocked' }
    finally { $c.Close() }
}

function Get-LocalIp([string]$toIp) {
    if ($S) { return [string](Get-Stub 'local_ip') }
    try {
        $sock = New-Object System.Net.Sockets.Socket('InterNetwork', 'Dgram', 'Udp')
        try { $sock.Connect($toIp, 6443); return $sock.LocalEndPoint.Address.ToString() } finally { $sock.Close() }
    } catch { return '' }
}

# HTTP через curl.exe (есть в Windows 10 1803+; тот же инструмент, что в run-node.ps1). Insecure: без проверки сертификата.
function Invoke-Http([string]$method, [string]$url, [bool]$insecure, [string]$bodyJson = '', [string]$bearer = '') {
    if ($S) {
        $key = $(if ($insecure) { "$method $url" } else { "TRUSTED $method $url" })
        $v = Get-StubKey 'http' $key
        if ($null -eq $v) { return [pscustomobject]@{ Code = 0; Body = ''; Exit = 7 } }
        $exit = 0; if ($v.PSObject.Properties['exit']) { $exit = [int]$v.exit }
        $body = ''; if ($v.PSObject.Properties['body']) { $body = [string]$v.body }
        return [pscustomobject]@{ Code = [int]$v.code; Body = $body; Exit = $exit }
    }
    $out = [IO.Path]::GetTempFileName(); $inp = ''
    try {
        $a = @('-s', '-o', $out, '-w', '%{http_code}', '--connect-timeout', '5', '-m', '15', '--ssl-no-revoke', '-X', $method)
        if ($insecure) { $a += '-k' }
        if ($bearer) { $a += @('-H', "Authorization: Bearer $bearer") }
        if ($bodyJson) {
            $inp = [IO.Path]::GetTempFileName(); [IO.File]::WriteAllText($inp, $bodyJson)
            $a += @('-H', 'Content-Type: application/json', '-d', "@$inp")
        }
        $a += $url
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $code = (& curl.exe @a 2>$null | Out-String).Trim() } finally { $ErrorActionPreference = $prev }
        $exit = $LASTEXITCODE
        $n = 0; [void][int]::TryParse($code, [ref]$n)
        $body = ''; if (Test-Path -LiteralPath $out) { $body = [IO.File]::ReadAllText($out) }
        return [pscustomobject]@{ Code = $n; Body = $body; Exit = $exit }
    } finally {
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
        if ($inp) { Remove-Item -LiteralPath $inp -Force -ErrorAction SilentlyContinue }
    }
}

function Test-IsAdmin {
    if ($S) { return [bool](Get-Stub 'admin') }
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-OsBuild {
    if ($S) { return [int](Get-Stub 'os_build') }
    return [Environment]::OSVersion.Version.Build
}

# ------------------------------------------------------------------ разбор портов и адресов в правилах
function Test-PortInSpec([string]$spec, [int]$port, [bool]$anyMatches) {
    if (-not $spec -or $spec -eq 'Any') { return $anyMatches }
    foreach ($part in $spec.Split(',')) {
        $p = $part.Trim()
        if ($p -match '^(\d+)-(\d+)$') { if ($port -ge [int]$Matches[1] -and $port -le [int]$Matches[2]) { return $true } }
        elseif ($p -eq "$port") { return $true }
    }
    return $false
}

function ConvertTo-IpNumber([string]$ip) {
    $b = [System.Net.IPAddress]::Parse($ip).GetAddressBytes()
    return [int64]$b[0] * 16777216 + [int64]$b[1] * 65536 + [int64]$b[2] * 256 + [int64]$b[3]
}

# yes | no | unknown (LocalSubnet и другие ключевые слова: подсеть ПК здесь неизвестна)
function Test-IpInSpec([string]$spec, [string]$ip) {
    if (-not $spec -or $spec -eq 'Any') { return 'yes' }
    $n = ConvertTo-IpNumber $ip
    $unknown = $false
    foreach ($part in $spec.Split(',')) {
        $p = $part.Trim()
        try {
            if ($p -match '^(\d+\.\d+\.\d+\.\d+)/(\d+)$') {
                $size = [math]::Pow(2, 32 - [int]$Matches[2])
                if ([math]::Floor($n / $size) -eq [math]::Floor((ConvertTo-IpNumber $Matches[1]) / $size)) { return 'yes' }
            }
            elseif ($p -match '^(\d+\.\d+\.\d+\.\d+)/(\d+\.\d+\.\d+\.\d+)$') {
                $size = 4294967296 - (ConvertTo-IpNumber $Matches[2])
                if ([math]::Floor($n / $size) -eq [math]::Floor((ConvertTo-IpNumber $Matches[1]) / $size)) { return 'yes' }
            }
            elseif ($p -match '^(\d+\.\d+\.\d+\.\d+)-(\d+\.\d+\.\d+\.\d+)$') {
                if ($n -ge (ConvertTo-IpNumber $Matches[1]) -and $n -le (ConvertTo-IpNumber $Matches[2])) { return 'yes' }
            }
            elseif ($p -match '^\d+\.\d+\.\d+\.\d+$') { if ($n -eq (ConvertTo-IpNumber $p)) { return 'yes' } }
            else { $unknown = $true }
        } catch { $unknown = $true }
    }
    if ($unknown) { return 'unknown' }
    return 'no'
}

# ------------------------------------------------------------------ брандмауэр и WSL
# Входящие включённые правила: Name, Action, Protocol, Ports, Remote, Program, Profile. $null, если прочитать не удалось.
# ActiveStore: действующий набор, вместе с правилами из групповой политики (их нет в PersistentStore по умолчанию).
function Get-InboundRules {
    if ($S) {
        $f = Get-Stub 'firewall'
        if ($null -eq $f -or -not $f.PSObject.Properties['rules']) { return $null }
        return ,@($f.rules | ForEach-Object {
            $r = $_; $g = { param($n, $d) if ($r.PSObject.Properties[$n]) { [string]$r.$n } else { $d } }
            [pscustomobject]@{
                Name = [string]$r.name; Action = [string]$r.action; Protocol = (& $g 'protocol' 'TCP'); Ports = [string]$r.ports
                Remote = (& $g 'remote' 'Any'); Program = (& $g 'program' 'Any'); Profile = (& $g 'profile' 'Any')
            }
        })
    }
    try {
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Enabled True -ErrorAction Stop)
        $ports = @{}; Get-NetFirewallPortFilter -All -PolicyStore ActiveStore -ErrorAction Stop | ForEach-Object { $ports[$_.InstanceID] = $_ }
        $addrs = @{}; Get-NetFirewallAddressFilter -All -PolicyStore ActiveStore -ErrorAction Stop | ForEach-Object { $addrs[$_.InstanceID] = $_ }
        $apps = @{}; Get-NetFirewallApplicationFilter -All -PolicyStore ActiveStore -ErrorAction Stop | ForEach-Object { $apps[$_.InstanceID] = $_ }
        return ,@($rules | ForEach-Object {
            $pf = $ports[$_.InstanceID]; $af = $addrs[$_.InstanceID]; $ap = $apps[$_.InstanceID]
            [pscustomobject]@{
                Name     = [string]$_.DisplayName
                Action   = [string]$_.Action
                Protocol = $(if ($pf) { [string]$pf.Protocol } else { 'Any' })
                Ports    = $(if ($pf) { (@($pf.LocalPort) -join ',') } else { 'Any' })
                Remote   = $(if ($af) { (@($af.RemoteAddress) -join ',') } else { 'Any' })
                Program  = $(if ($ap -and $ap.Program) { [string]$ap.Program } else { 'Any' })
                Profile  = [string]$_.Profile
            }
        })
    } catch { return $null }
}

# Профили брандмауэра, в которых сейчас сети этого ПК (Domain, Private, Public); пусто, если не удалось узнать.
function Get-ActiveProfiles {
    if ($S) { $p = Get-Stub 'firewall.active_profiles'; if ($null -eq $p) { return @() }; return @($p | ForEach-Object { [string]$_ }) }
    try {
        return @(Get-NetConnectionProfile -ErrorAction Stop | ForEach-Object {
            $c = [string]$_.NetworkCategory; if ($c -eq 'DomainAuthenticated') { 'Domain' } else { $c } } | Sort-Object -Unique)
    } catch { return @() }
}

# Правило действует в одном из профилей $active (неизвестные профили: считается, что действует)
function Test-RuleInProfiles($rule, [string[]]$active) {
    if ($active.Count -eq 0 -or -not $rule.Profile -or $rule.Profile -eq 'Any') { return $true }
    foreach ($p in $active) { if ($rule.Profile -match "\b$p\b") { return $true } }
    return $false
}

function Get-HyperVFirewall { # Supported, DefaultInbound, Rules (Name, Ports)
    if ($S) {
        $h = Get-Stub 'hyperv'
        if ($null -eq $h -or -not $h.supported) { return [pscustomobject]@{ Supported = $false; DefaultInbound = ''; Rules = @() } }
        $rules = @(); if ($h.PSObject.Properties['rules']) { $rules = @($h.rules | ForEach-Object { [pscustomobject]@{ Name = [string]$_.name; Ports = [string]$_.ports } }) }
        return [pscustomobject]@{ Supported = $true; DefaultInbound = [string]$h.default_inbound; Rules = $rules }
    }
    try {
        $vm = Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name $HyperVCreator -ErrorAction Stop
        $rules = @(Get-NetFirewallHyperVRule -VMCreatorId $HyperVCreator -ErrorAction Stop |
            Where-Object { [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Allow' -and [string]$_.Enabled -eq 'True' } |
            ForEach-Object { [pscustomobject]@{ Name = [string]$_.DisplayName; Ports = (@($_.LocalPorts) -join ',') } })
        return [pscustomobject]@{ Supported = $true; DefaultInbound = [string]$vm.DefaultInboundAction; Rules = $rules }
    } catch { return [pscustomobject]@{ Supported = $false; DefaultInbound = ''; Rules = @() } }
}

function ConvertFrom-WslOutput($raw) { # wsl.exe печатает UTF-16: PowerShell видит символы с нулевыми байтами
    return (((@($raw) | ForEach-Object { [string]$_ }) -join "`n") -replace "`0", '').Trim()
}
function Get-WslInfo { # Installed, Version, Mode (работающий), Config (текст .wslconfig)
    if ($S) {
        $w = Get-Stub 'wsl'
        if ($null -eq $w -or -not $w.installed) { return [pscustomobject]@{ Installed = $false; Version = ''; Mode = ''; Config = '' } }
        $g = { param($n) if ($w.PSObject.Properties[$n]) { [string]$w.$n } else { '' } }
        return [pscustomobject]@{ Installed = $true; Version = (& $g 'version'); Mode = (& $g 'networking_mode'); Config = (& $g 'wslconfig') }
    }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return [pscustomobject]@{ Installed = $false; Version = ''; Mode = ''; Config = '' } }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $ver = ConvertFrom-WslOutput (& wsl.exe --version 2>$null)
        $distros = ConvertFrom-WslOutput (& wsl.exe -l -q 2>$null)
        $version = ''; if ($ver -match 'WSL[^:\r\n]*:\s*([0-9]+(\.[0-9]+)+)') { $version = $Matches[1] }
        if (-not $version -and -not $distros) { return [pscustomobject]@{ Installed = $false; Version = ''; Mode = ''; Config = '' } }
        $mode = ConvertFrom-WslOutput (& wsl.exe -e wslinfo --networking-mode 2>$null)
        if ($LASTEXITCODE -ne 0 -or $mode -notmatch '^[a-z]+$') { $mode = '' }
    } finally { $ErrorActionPreference = $prev }
    $cfg = ''; $cfgPath = Join-Path $env:USERPROFILE '.wslconfig'
    if (Test-Path -LiteralPath $cfgPath) { $cfg = Get-Content -Raw -LiteralPath $cfgPath }
    return [pscustomobject]@{ Installed = $true; Version = $version; Mode = $mode; Config = $cfg }
}

# Где работает контейнер llm-node на этом ПК и его MAIN_HOST (остальные переменные не читаются).
# Runtime: wsl (Docker CE внутри WSL, основной путь) | desktop (Docker Desktop, запасной путь) | '' (контейнер не найден).
# docker.exe Windows проверяется первым: при интеграции Docker Desktop с WSL docker внутри WSL видит тот же движок.
function Get-NodeContainer {
    if ($S) {
        $h = Get-StubKey 'node_env' 'MAIN_HOST'
        return [pscustomobject]@{ Runtime = [string](Get-Stub 'node_runtime'); MainHost = $(if ($h) { [string]$h } else { '' }) }
    }
    $fmt = '{{range .Config.Env}}{{println .}}{{end}}'
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        foreach ($cmd in @(@('docker'), @('wsl.exe', '-u', 'root', '-e', 'docker'))) {
            if (-not (Get-Command $cmd[0] -ErrorAction SilentlyContinue)) { continue }
            $rest = @(); if ($cmd.Count -gt 1) { $rest = $cmd[1..($cmd.Count - 1)] }
            $lines = ConvertFrom-WslOutput (& $cmd[0] @rest inspect -f $fmt llm-node 2>$null)
            if ($LASTEXITCODE -ne 0) { continue }
            $runtime = 'wsl'
            if ($cmd[0] -eq 'docker') { # docker.exe может смотреть и на dockerd в WSL (DOCKER_HOST): решает ОС движка
                $os = ConvertFrom-WslOutput (& docker info --format '{{.OperatingSystem}}' 2>$null)
                if ($os -match 'Docker Desktop') { $runtime = 'desktop' }
            }
            $mainHost = ''
            foreach ($l in ($lines -split "`n")) { if ($l -match '^MAIN_HOST=(.+)$') { $mainHost = $Matches[1].Trim() } }
            return [pscustomobject]@{ Runtime = $runtime; MainHost = $mainHost }
        }
    } finally { $ErrorActionPreference = $prev }
    return [pscustomobject]@{ Runtime = ''; MainHost = '' }
}

# Связность изнутри WSL (там работает k3s-агент ноды): имя главного через резолвер WSL и 6443/tcp через /dev/tcp bash.
# Resolved: адрес или ''; Port: open | refused | blocked.
function Get-WslIpv4([string]$name) {
    if ($S) { $v = Get-StubKey 'wsl.dns' $name; if ($null -eq $v) { return '' }; return [string]$v }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = ConvertFrom-WslOutput (& wsl.exe -e getent ahostsv4 $name 2>$null) } finally { $ErrorActionPreference = $prev }
    if ($out -match '^(\d+\.\d+\.\d+\.\d+)') { return $Matches[1] }
    return ''
}
function Test-WslPort([string]$ip, [int]$port) {
    if ($S) { $v = Get-StubKey 'wsl.tcp' "${ip}:${port}"; if ($null -eq $v) { return 'blocked' }; return [string]$v }
    # без двойных кавычек внутри: Windows PowerShell 5.1 искажает их в аргументах внешних программ
    $script = 'r=$(timeout ' + $TimeoutSec + ' bash -c '': </dev/tcp/$0/$1'' $1 $2 2>&1); c=$?; ' +
        'case $c$r in 0*) echo open ;; *[Rr]efused*) echo refused ;; *) echo blocked ;; esac'
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $st = ConvertFrom-WslOutput (& wsl.exe -e bash -c $script _ $ip $port 2>$null) } finally { $ErrorActionPreference = $prev }
    if ($st -in 'open', 'refused') { return $st }
    return 'blocked'
}

# ------------------------------------------------------------------ значение -Main: параметр, конфигурация, контейнер, вопрос
function Read-Answer([string]$question, [string]$default) {
    if ($default) { Write-Host -NoNewline "$question [$default]: " } else { Write-Host -NoNewline "${question}: " }
    $a = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }
    return ([string]$a).Trim()
}

$asked = $false
if (-not $Main -and (Test-Path -LiteralPath $Config)) {
    try { $c = Get-Content -Raw -Encoding UTF8 -LiteralPath $Config | ConvertFrom-Json; if ($c.Main) { $Main = [string]$c.Main } } catch { }
}
$node = Get-NodeContainer
$fromNode = ''
if (-not $Main) { $fromNode = $node.MainHost; $Main = $fromNode }
if (-not $Main) {
    if ($NonInteractive) {
        [Console]::Error.WriteLine('Ошибка: не задан адрес главного сервера. Укажите -Main (имя PUBLIC_HOSTNAME или IP главного) или запустите без -NonInteractive.')
        exit 2
    }
    $Main = Read-Answer 'Имя или адрес главного сервера (PUBLIC_HOSTNAME)' ''
    $asked = $true
} elseif ($fromNode -and -not $NonInteractive) {
    $Main = Read-Answer 'Имя или адрес главного сервера (найден в контейнере llm-node)' $fromNode
    $asked = $true
}
if ($Main -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$') {
    [Console]::Error.WriteLine("Ошибка: недопустимое имя или адрес главного сервера: '$Main'")
    exit 2
}
if ($asked) {
    try {
        $dir = Split-Path -Parent $Config
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [IO.File]::WriteAllText($Config, ((@{ Main = $Main } | ConvertTo-Json) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
        [Console]::Error.WriteLine("Ответ записан в ${Config}: повторный запуск пройдёт без вопросов.")
    } catch { [Console]::Error.WriteLine("Предупреждение: не удалось записать $Config ($($_.Exception.Message)).") }
}

Write-Host 'NSU LLM Cluster: диагностика сети ПК класса'
Write-Host "Главный: $Main"
Write-Host ''

# ------------------------------------------------------------------ 1. DNS
$mainIp = ''
if ($Main -match '^\d+\.\d+\.\d+\.\d+$') {
    $mainIp = $Main
    Write-Info "Главный задан адресом ($Main): проверка DNS пропущена. Для сертификата НГУ и смены адреса удобнее имя"
} else {
    $ips = @(Resolve-Ipv4 $Main)
    if ($ips.Count -eq 0) {
        Write-Bad "DNS: $Main не разрешается на этом ПК" @(
            'Причина: в DNS кампуса нет записи для этого имени или этот ПК использует другой DNS-сервер',
            "Исправление: попросите администратора DNS создать A-запись $Main -> <IP главного>",
            "Проверка: Resolve-DnsName $Main; nslookup $Main",
            "Временно (PowerShell от администратора): Add-Content C:\Windows\System32\drivers\etc\hosts '<IP главного> $Main'",
            'Если в WSL имя не разрешается, а в Windows разрешается: в .wslconfig задайте dnsTunneling=false (раздел [wsl2]) и выполните wsl --shutdown')
    } else {
        $mainIp = $ips[0]
        Write-Ok "DNS: $Main -> $mainIp"
    }
}
$localIp = ''
if ($mainIp) {
    $localIp = Get-LocalIp $mainIp
    if ($localIp) { Write-Info "Адрес этого ПК при обращении к главному: $localIp" }
}

# ------------------------------------------------------------------ 2. 443 и 6443
$portOk = @{}
foreach ($port in 443, 6443) {
    if (-not $mainIp) { Write-Info "${port}/tcp главного не проверен: нет адреса главного (см. проверку DNS выше)"; continue }
    $state = Test-Port $mainIp $port
    $portOk[$port] = ($state -eq 'open')
    if ($state -eq 'open') { Write-Ok "${port}/tcp главного ($mainIp) доступен"; continue }
    $note = $(if ($port -eq 6443) { 'Порт 6443 нужен ноде в режиме k3s; для режима standalone достаточно 443.' } else { 'Порт 443 нужен для регистрации, ca.crt и удаления ноды.' })
    if ($state -eq 'refused') {
        Write-Bad "${port}/tcp главного ($mainIp) недоступен: соединение отклонено" @(
            "Причина: сеть до главного доходит, но порт $port на нём никто не слушает (контейнер llm-main остановлен или порт не опубликован)",
            'Исправление (на главном сервере): bash main/diag.sh, затем при необходимости bash main/run-main.sh --env-file .env',
            $note)
    } else {
        Write-Bad "${port}/tcp главного ($mainIp) недоступен: нет ответа" @(
            'Причина: брандмауэр на главном или в сети кампуса (маршрутизатор между подсетями) режет порт, либо главный выключен',
            "Исправление (на главном сервере, Ubuntu): sudo ufw allow proto tcp from $(if ($localIp) { $localIp } else { '<адрес этого ПК>' }) to any port $port",
            "Проверка с этого ПК: Test-NetConnection $mainIp -Port $port -InformationLevel Detailed; tracert -d $mainIp",
            "Если сегмент закрыт на маршрутизаторе, попросите сетевого администратора открыть TCP $port от подсети класса до главного",
            $note)
    }
}

# ------------------------------------------------------------------ 3. ca.crt и доверие к сертификату
$caFile = ''
if ($portOk[443]) {
    $r = Invoke-Http 'GET' "https://$Main/ca.crt" $true
    if ($r.Code -eq 200 -and $r.Body -match '-----BEGIN CERTIFICATE-----') {
        $fp = ''
        try {
            $b64 = ($r.Body -replace '-----(BEGIN|END) CERTIFICATE-----', '' -replace '\s', '')
            $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(, [Convert]::FromBase64String($b64))
            $sha = [System.Security.Cryptography.SHA256]::Create()
            $fp = ' (SHA256 ' + (($sha.ComputeHash($cert.RawData) | ForEach-Object { $_.ToString('X2') }) -join ':') + ')'
        } catch { }
        Write-Ok "ca.crt скачан с https://$Main/ca.crt$fp"
        if ($fp) { Write-Detail @('Сверьте отпечаток с тем, что показывает администратор главного: нода доверяет первому скачанному ca.crt.') }
        if (-not $S) {
            $caFile = Join-Path ([IO.Path]::GetTempPath()) 'nsu-llm-ca.crt'
            try { [IO.File]::WriteAllText($caFile, $r.Body) } catch { $caFile = '' }
        } else { $caFile = 'C:\Temp\nsu-llm-ca.crt' }
    } elseif ($r.Code -eq 404) {
        Write-Info 'ca.crt на главном не публикуется (используется сертификат НГУ, --certs): скачивать нечего'
    } else {
        Write-Bad "ca.crt: https://$Main/ca.crt ответил $(if ($r.Code) { $r.Code } else { 'без ответа' })" @(
            'Причина: порт 443 открыт, но портал не отвечает (главный ещё поднимается) или Caddy не отдаёт ca.crt',
            'Исправление (на главном): docker logs --tail 100 llm-main; bash main/diag.sh')
    }

    $t = Invoke-Http 'GET' "https://$Main/readyz" $false
    if ($t.Exit -eq 0 -and $t.Code -eq 200) {
        Write-Ok 'сертификат главного принят системой этого ПК'
    } elseif ($t.Exit -eq 0) {
        Write-Info "сертификат принят, но /readyz ответил $($t.Code): портал ещё не готов"
    } elseif ($t.Exit -in 60, 35, 51, 58, 59, 77, 83) {
        $import = $(if ($caFile) { "Import-Certificate -FilePath '$caFile' -CertStoreLocation Cert:\LocalMachine\Root" } else { 'Import-Certificate -FilePath <ca.crt> -CertStoreLocation Cert:\LocalMachine\Root' })
        Write-Warn 'сертификат главного не доверенный на этом ПК' @(
            'Причина: используется внутренний CA, его ca.crt нет в хранилище Windows. Нужно для браузера и PowerShell на этом ПК; сама нода хранит собственную копию ca.crt и работает без этого',
            "Исправление (PowerShell от администратора): $import",
            'Для всех ПК класса сразу: раздать ca.crt групповой политикой (Конфигурация компьютера, Политики Windows, Параметры безопасности, Политики открытого ключа, Доверенные корневые центры сертификации). Подробно: docs/deploy-class.md, раздел «Сеть и DNS»')
    } else {
        Write-Info "сертификат не проверен: curl завершился с кодом $($t.Exit)"
    }

    # ------------------------------------------------------------ 4. эндпоинт регистрации
    $reg = Invoke-Http 'POST' "https://$Main/api/nodes/register" $true '{"name":"diag-probe"}' 'diag-probe'
    if ($reg.Code -eq 401) {
        Write-Ok 'эндпоинт регистрации нод отвечает: POST /api/nodes/register -> 401 (токен не передан, это ожидаемо)'
    } else {
        Write-Bad "эндпоинт регистрации нод: POST /api/nodes/register ответил $(if ($reg.Code) { $reg.Code } else { 'без ответа' }), ожидался 401" @(
            'Причина: на порту 443 отвечает не портал NSU LLM (прокси, заглушка) или портал ещё не поднялся (502, 503)',
            'Исправление (на главном): bash main/diag.sh; docker logs --tail 100 llm-main',
            "Проверка с этого ПК: curl.exe -sk -o NUL -w '%{http_code}' https://$Main/readyz")
    }
} else {
    Write-Info 'ca.crt, сертификат и эндпоинт регистрации не проверены: порт 443 главного недоступен'
}

# ------------------------------------------------------------------ 5. локальный брандмауэр Windows
$subnetExample = '<подсеть класса, например 10.0.1.0/24>'
if ($localIp -match '^(\d+\.\d+\.\d+)\.\d+$') { $subnetExample = "$($Matches[1]).0/24" }
$remoteHint = $(if ($mainIp) { "$subnetExample,$mainIp" } else { $subnetExample })
$rules = Get-InboundRules
if ($null -eq $rules) {
    Write-Info 'правила брандмауэра Windows не прочитаны (нужны права администратора или модуль NetSecurity)' @('Запустите PowerShell от имени администратора и повторите.')
} else {
    $activeProfiles = @(Get-ActiveProfiles)
    foreach ($port in 18080, 18050) {
        $tcpAllow = @($rules | Where-Object { $_.Action -eq 'Allow' -and $_.Protocol -in 'TCP', 'Any' })
        $portRules = @($tcpAllow | Where-Object { Test-PortInSpec $_.Ports $port $false })
        $allow = @($portRules | Where-Object { Test-RuleInProfiles $_ $activeProfiles })
        $reach = $allow
        if ($mainIp -and $port -eq 18080) { $reach = @($allow | Where-Object { (Test-IpInSpec $_.Remote $mainIp) -ne 'no' }) }
        $purpose = $(if ($port -eq 18080) { 'инференс, запросы шлюза с главного' } else { 'RPC режима split между нодами класса' })
        # Без правила для порта его всё же могут пропускать правила на любые порты (обычно по программе: Docker Desktop,
        # WSL) или правило для порта в другом профиле: точно покажет только проверка снаружи, поэтому это предупреждение.
        $maybe = @($portRules | Where-Object { -not (Test-RuleInProfiles $_ $activeProfiles) }) +
            @($tcpAllow | Where-Object { -not $_.Ports -or $_.Ports -eq 'Any' } |
                Where-Object { (Test-RuleInProfiles $_ $activeProfiles) -and (-not $mainIp -or $port -ne 18080 -or (Test-IpInSpec $_.Remote $mainIp) -ne 'no') })
        $cmd = "New-NetFirewallRule -DisplayName NSU-LLM-Node -Direction Inbound -Protocol TCP -LocalPort 18080,18050 -RemoteAddress $remoteHint -Action Allow"
        if ($allow.Count -eq 0 -and $maybe.Count -gt 0) {
            $about = ($maybe | Select-Object -First 3 | ForEach-Object {
                if (-not (Test-RuleInProfiles $_ $activeProfiles)) { "$($_.Name) (порт $port, но только профиль $($_.Profile), а сеть сейчас: $($activeProfiles -join ', '))" }
                elseif ($_.Program -and $_.Program -ne 'Any') { "$($_.Name) (любые порты для программы $($_.Program))" }
                else { "$($_.Name) (любые порты)" } }) -join '; '
            Write-Warn "брандмауэр: отдельного правила для входящего ${port}/tcp ($purpose) нет, но его могут пропускать другие правила" @(
                "Правила: $about",
                'Причина: правило для программы пускает порт, только если его слушает эта программа; правило другого профиля не действует в текущей сети. Надёжнее правило для порта',
                "Исправление (PowerShell от администратора): $cmd",
                $(if ($port -eq 18080) { 'Проверка снаружи: bash main/diag.sh на главном сервере (18080/tcp доступен или нет)' } else { 'Нужно только для развёртываний в режиме split' }))
        } elseif ($allow.Count -eq 0) {
            if ($port -eq 18080) {
                Write-Bad "брандмауэр: нет разрешающего правила для входящего 18080/tcp ($purpose)" @(
                    'Причина: без правила Windows отбрасывает запросы шлюза, нода будет в кластере, но модель не ответит',
                    "Исправление (PowerShell от администратора): $cmd",
                    'Или запустите run-node.ps1 от администратора: он создаёт и обновляет правило NSU-LLM-Node сам')
            } else {
                Write-Warn "брандмауэр: нет разрешающего правила для входящего 18050/tcp ($purpose)" @(
                    'Нужно только для развёртываний в режиме split; для replicas можно не открывать. Порт без аутентификации: открывайте только для адресов класса, не для всей сети',
                    "Исправление (PowerShell от администратора): $cmd")
            }
        } elseif ($reach.Count -eq 0) {
            Write-Bad "брандмауэр: правило для входящего 18080/tcp не пускает главный ($mainIp)" @(
                "Причина: в правиле $($allow[0].Name) RemoteAddress = $($allow[0].Remote), адреса главного там нет",
                "Исправление (PowerShell от администратора): Set-NetFirewallRule -DisplayName '$($allow[0].Name)' -RemoteAddress $remoteHint")
        } else {
            $unknown = $mainIp -and $port -eq 18080 -and (@($reach | Where-Object { (Test-IpInSpec $_.Remote $mainIp) -eq 'yes' }).Count -eq 0)
            if ($unknown) {
                Write-Warn "брандмауэр: входящий 18080/tcp разрешён правилом $($reach[0].Name), но только для '$($reach[0].Remote)'" @(
                    'Причина: LocalSubnet и подобные значения пускают только свою подсеть; главный в другой подсети будет отброшен',
                    "Исправление (PowerShell от администратора): Set-NetFirewallRule -DisplayName '$($reach[0].Name)' -RemoteAddress $remoteHint")
            } else {
                Write-Ok "брандмауэр: входящий ${port}/tcp разрешён правилом $($reach[0].Name)"
            }
        }
    }
    $blocks = @($rules | Where-Object { $_.Action -eq 'Block' -and $_.Protocol -in 'TCP', 'Any' -and ((Test-PortInSpec $_.Ports 18080 $false) -or (Test-PortInSpec $_.Ports 18050 $false)) })
    if ($blocks.Count -gt 0) {
        $names = ($blocks | ForEach-Object { $_.Name }) -join ', '
        Write-Bad "брандмауэр: есть запрещающие входящие правила для 18080 или 18050: $names" @(
            'Причина: запрет сильнее разрешения, порт закрыт, как бы ни было настроено разрешающее правило',
            "Исправление (PowerShell от администратора): Disable-NetFirewallRule -DisplayName '$($blocks[0].Name)'   (или удалите правило, если оно не нужно)")
    } else {
        Write-Ok 'брандмауэр: запрещающих входящих правил для 18080 и 18050 нет'
    }
}

# ------------------------------------------------------------------ 6. WSL и mirrored-сеть
# Проверки выбираются по тому, где работает llm-node: в Docker Desktop (запасной путь) порты публикует сам Docker
# Desktop, NAT-режим WSL и Hyper-V firewall ему не мешают. Контейнер не найден: проверяется основной путь (Docker CE
# в WSL), но его провалы только предупреждения, ведь нода может работать и через Docker Desktop.
$wsl = Get-WslInfo
$mirroredConfigured = $wsl.Config -match '(?im)^\s*networkingMode\s*=\s*mirrored\s*$'
$mirroredActive = $false
$onWsl = $node.Runtime -eq 'wsl'
$fallback = 'Если нода запускается через Docker Desktop (запасной путь), это не мешает: mirrored-сеть нужна только ноде в WSL'
function Write-WslProblem([string]$msg, [string[]]$lines) {
    if ($onWsl) { Write-Bad $msg $lines } else { Write-Warn $msg (@($lines) + $fallback) }
}
if ($node.Runtime -eq 'desktop') {
    Write-Info 'нода работает в Docker Desktop (запасной путь): порты 18080 и 18050 публикует Docker Desktop, mirrored-сеть WSL и правило Hyper-V firewall не нужны'
    if ($wsl.Installed -and $wsl.Mode -and $wsl.Mode -ne 'mirrored') {
        Write-Info "WSL работает в режиме $($wsl.Mode): для ноды в Docker Desktop это нормально"
    }
} elseif (-not $wsl.Installed) {
    Write-Info 'WSL не найден: нода работает через Docker Desktop (публикация портов), mirrored-сеть не нужна'
} else {
    if ($onWsl) { Write-Info 'нода работает в WSL (Docker CE): проверяются mirrored-сеть, Hyper-V firewall и связь с главным изнутри WSL' }
    else { Write-Info 'контейнер llm-node на этом ПК не найден: проверяется основной путь (нода в WSL)' }
    $build = Get-OsBuild
    if ($build -lt 22621) {
        Write-WslProblem "WSL: Windows (сборка $build) не поддерживает mirrored-сеть" @(
            'Причина: networkingMode=mirrored требует Windows 11 22H2 (сборка 22621) и новее; без неё главный не достучится до порта 18080 ноды в WSL',
            'Исправление: обновите Windows до 11 22H2 или новее, либо запустите ноду на Docker Desktop (docs/deploy-class.md, «Нода на Windows: запасной путь»)')
    } elseif (-not $wsl.Version) {
        Write-Warn 'WSL: версию определить не удалось (wsl --version не отвечает)' @('Исправление (PowerShell от администратора): wsl --update; затем wsl --version (нужна версия 2.0.5 и новее)')
    } elseif ([version]$wsl.Version -lt [version]'2.0.5') {
        Write-WslProblem "WSL: версия $($wsl.Version) слишком старая для mirrored-сети" @(
            'Причина: networkingMode=mirrored появился в WSL 2.0.5',
            'Исправление (PowerShell от администратора): wsl --update; wsl --shutdown')
    } else {
        Write-Ok "WSL: версия $($wsl.Version)"
    }

    $fixConfig = @("Исправление: откройте %UserProfile%\.wslconfig (notepad `$env:UserProfile\.wslconfig), в разделе [wsl2] задайте networkingMode=mirrored, затем выполните wsl --shutdown и запустите ноду снова")
    if ($wsl.Mode -eq 'mirrored') {
        $mirroredActive = $true
        Write-Ok 'WSL: mirrored-сеть работает (wslinfo --networking-mode: mirrored)'
        if (-not $mirroredConfigured) {
            Write-Warn 'WSL: mirrored включена, но .wslconfig об этом не знает' @('Причина: после wsl --shutdown WSL вернётся к NAT. Добавьте networkingMode=mirrored в раздел [wsl2] файла %UserProfile%\.wslconfig')
        }
    } elseif ($wsl.Mode) {
        if ($mirroredConfigured) {
            Write-WslProblem "WSL: в .wslconfig задан mirrored, но WSL работает в режиме $($wsl.Mode)" (@('Причина: настройка не применена: WSL читает .wslconfig только при запуске виртуальной машины', 'Исправление (PowerShell): wsl --shutdown   (затем подождите 8 секунд и запустите ноду)'))
        } else {
            Write-WslProblem "WSL: сеть работает в режиме $($wsl.Mode), mirrored не включена" (@('Причина: в режиме NAT порт ноды виден только самому ПК, главный сервер до 18080 не достучится') + $fixConfig)
        }
    } else {
        if ($mirroredConfigured) {
            Write-Info 'WSL: networkingMode=mirrored есть в .wslconfig, но работающий режим определить не удалось (wslinfo недоступен или дистрибутив не запускается)' @('Проверьте внутри WSL: wslinfo --networking-mode (должно быть mirrored). Если нет: wsl --shutdown и запуск заново.')
        } else {
            Write-Warn 'WSL: в .wslconfig нет networkingMode=mirrored' (@('Причина: для ноды внутри WSL нужна mirrored-сеть (иначе порт 18080 не виден из LAN). Для Docker Desktop это не требуется') + $fixConfig)
        }
    }

    if ($mirroredActive -or $mirroredConfigured) {
        $hv = Get-HyperVFirewall
        if (-not $hv.Supported) {
            Write-Info 'Hyper-V firewall: состояние прочитать не удалось (старая сборка Windows 11 или нет прав администратора)' @('Если порт 18080 ноды в WSL недоступен снаружи, проверьте: Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name ''' + $HyperVCreator + '''')
        } else {
            $hvRule = @($hv.Rules | Where-Object { (Test-PortInSpec $_.Ports 18080 $false) })
            if ($hv.DefaultInbound -eq 'Allow') {
                Write-Ok 'Hyper-V firewall: входящие в WSL разрешены по умолчанию'
            } elseif ($hvRule.Count -gt 0) {
                Write-Ok "Hyper-V firewall: входящий 18080/tcp разрешён правилом $($hvRule[0].Name)"
                # 18050 нужен только для split: без правила обычные развёртывания работают, поэтому предупреждение
                $hvRpc = @($hv.Rules | Where-Object { (Test-PortInSpec $_.Ports 18050 $false) })
                if ($hvRpc.Count -gt 0) {
                    Write-Ok "Hyper-V firewall: входящий 18050/tcp разрешён правилом $($hvRpc[0].Name)"
                } else {
                    Write-Warn 'Hyper-V firewall: нет правила для входящего 18050/tcp (RPC режима split)' @(
                        'Причина: без этого правила нода в WSL не примет часть модели от других нод класса в split; обычные развёртывания работают',
                        "Исправление (PowerShell от администратора): New-NetFirewallHyperVRule -Name llm-inference -DisplayName 'LLM inference' -Direction Inbound -VMCreatorId '$HyperVCreator' -Protocol TCP -LocalPorts 18080,18050 (или добавьте 18050 в существующее правило)")
                }
            } else {
                Write-WslProblem 'Hyper-V firewall: входящие в WSL запрещены, правила для 18080/tcp нет' @(
                    'Причина: Hyper-V firewall по умолчанию режет входящие в WSL, даже если правило Windows Firewall есть. run-node.ps1 это правило не создаёт',
                    "Исправление (PowerShell от администратора): New-NetFirewallHyperVRule -Name llm-inference -DisplayName 'LLM inference' -Direction Inbound -VMCreatorId '$HyperVCreator' -Protocol TCP -LocalPorts 18080,18050")
            }
        }
    }

    # k3s-агент ноды работает внутри WSL: имя главного он разрешает резолвером WSL, к 6443 подключается из WSL
    $wslIp = ''
    if ($Main -match '^\d+\.\d+\.\d+\.\d+$') { $wslIp = $Main }
    else {
        $wslIp = Get-WslIpv4 $Main
        if (-not $wslIp) {
            Write-WslProblem "WSL: $Main не разрешается внутри WSL" @(
                $(if ($mainIp) { "Причина: Windows разрешает имя ($mainIp), а резолвер WSL нет: k3s-агент ноды не найдёт главный" } else { 'Причина: имя не разрешается ни в Windows, ни в WSL (см. проверку DNS выше)' }),
                "Проверка: wsl -e getent hosts $Main",
                'Исправление: в .wslconfig (раздел [wsl2]) задайте dnsTunneling=true или false (что работает в сети кампуса), затем wsl --shutdown',
                "Временно (внутри WSL): echo '$(if ($mainIp) { $mainIp } else { '<IP главного>' }) $Main' | sudo tee -a /etc/hosts")
        } elseif ($mainIp -and $wslIp -ne $mainIp) {
            Write-Warn "WSL: $Main внутри WSL -> $wslIp, а в Windows -> $mainIp" @('Причина: WSL использует другой DNS-сервер или свой /etc/hosts. Нода подключится к адресу из WSL; если это не главный, исправьте /etc/hosts в WSL')
        } else {
            Write-Ok "WSL: $Main -> $wslIp (резолвер WSL)"
        }
    }
    if ($wslIp) {
        $st = Test-WslPort $wslIp 6443
        if ($st -eq 'open') {
            Write-Ok "WSL: 6443/tcp главного ($wslIp) доступен изнутри WSL"
        } else {
            $why = $(if ($st -eq 'refused') { 'соединение отклонено' } else { 'нет ответа' })
            Write-WslProblem "WSL: 6443/tcp главного ($wslIp) недоступен изнутри WSL: $why" @(
                $(if ($portOk[6443]) { 'Причина: из Windows порт доступен, а из WSL нет: исходящие WSL режет Hyper-V firewall, VPN или прокси' } else { 'Причина: порт недоступен и из Windows (см. проверку 6443 выше)' }),
                "Проверка (внутри WSL): timeout 4 bash -c ': </dev/tcp/$wslIp/6443' && echo open",
                "Проверка (PowerShell от администратора): Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name '$HyperVCreator'   (DefaultOutboundAction должен быть Allow)")
        }
    }
}

Write-Host ''
Write-Host "Итог: проверок пройдено: $($script:nOk), провалов: $($script:nFail), предупреждений: $($script:nWarn)"
if ($script:nFail -gt 0) {
    Write-Host 'Исправьте провалы выше и запустите diag.ps1 ещё раз. Связность главного с нодой (обратное направление) проверяет main/diag.sh на главном сервере.'
    exit 1
}
Write-Host 'Связность главного с нодой (обратное направление) проверяет main/diag.sh на главном сервере.'
exit 0
