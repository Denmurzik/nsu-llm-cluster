# Массовое развёртывание нод NSU LLM на ПК класса с ПК администратора: install, update, uninstall, status через WinRM.
# Сама установка на каждом ПК — node\install-node.ps1 (WSL2 + Docker CE, автозапуск, брандмауэр); здесь только раздача файлов и запуск.
# PowerShell 5.1. По умолчанию интерактивный: определяет что может, спрашивает недостающее, показывает план и просит подтверждение.
#
#   .\class-deploy.ps1                                   # интерактивно: меню действий, мастер инвентаря и ответов
#   .\class-deploy.ps1 -Action status                    # состояние всех нод таблицей
#   .\class-deploy.ps1 -Action install -Only pc01        # сначала один ПК, потом остальные
#   .\class-deploy.ps1 -Action install -NonInteractive -Credential $cred   # без вопросов (ответы из файла)
#   .\class-deploy.ps1 -Action check                     # только проверить WinRM и напечатать, что включить
#   .\class-deploy.ps1 -Action install -DryRun           # план и команды, ничего не копируется и не запускается
#
# На ПК установщик по умолчанию запускается разовой задачей планировщика от учётки администратора (-RunMode task: вход как пакетное
# задание, с профилем и DPAPI, без «двойного прыжка»); вывод идёт в C:\ProgramData\NSU-LLM-stage\run.log и показывается по ходу.
# -RunMode session — прежний запуск прямо в сессии WinRM.
#
# Пароль администратора и токен агента уходят на ПК как SecureString (PSRP шифрует их ключом сеанса независимо от транспорта), а не
# строкой в аргументах; на ПК они только в файле параметров под DPAPI LocalMachine (доступ SYSTEM и Administrators, файл удаляется при
# чтении). Один пароль на весь класс означает, что администратор одного ПК получает его из LSA и входит на все остальные: поэтому
# -Laps берёт у каждого ПК свой пароль из Windows LAPS (Get-LapsADPassword), а без LAPS заведите на ПК отдельную учётную запись с
# уникальным паролем. Сервер WinRM по HTTP с TrustedHosts не проверяется (его мог занять другой ПК): для проверки сервера -UseSSL
# (HTTPS с сертификатом) или Kerberos (домен, ПК по имени). LocalAccountTokenFilterPolicy=1 не нужен и не рекомендуется.
#
# Перед любой записью каталог раздачи на ПК закрывается (node\secure-dir.ps1: владелец Administrators, Users только чтение):
# права C:\ProgramData по умолчанию позволяют любому пользователю заранее создать его и подменить скрипты.
#
# Место на системном диске ПК: install и update пропускают ПК, где свободно меньше -MinFreeGB (по умолчанию 14 ГБ; для установленной
# ноды при update порог не применяется, считается только место под образ), таблица итогов показывает свободное место на C: каждого ПК и
# отмечает ПК с местом меньше -WarnFreeGB (по умолчанию 18 ГБ); -MinFreeGB и -WarnFreeGB уходят на ПК в install-node.ps1.
# Колонка «NSU-LLM» показывает, сколько места занимает сама нода (C:\ProgramData\NSU-LLM и раздача); больше 16 ГБ отмечается и перечисляется.
#
# Файлы рядом с инвентарём (в git не попадают): class-nodes.csv (name,address,mode,accel,docker), class-answers.json (главный, токен
# агента под DPAPI, AllowFrom, образы), class-deploy-logs\<время>\<имя>.log. Подробности: docs\deploy-class.md, «Массовая установка».
[CmdletBinding()]
param(
    [ValidateSet('', 'install', 'update', 'uninstall', 'status', 'check')][string]$Action = '',
    [string]$Inventory = '',
    [string]$Answers = '',
    [pscredential]$Credential,
    [string]$Main = '',
    [string]$Token = '',
    [string]$AllowFrom = '',
    [string]$CaSha256 = '',
    [string]$ImageTarCpu = '',
    [string]$ImageTarGpu = '',
    [ValidateSet('', 'main', 'tar')][string]$ImageSource = '',
    [ValidateSet('auto', 'smb', 'winrm', 'none')][string]$CopyMode = 'auto',
    [int]$Throttle = 0,
    [double]$TimeoutMin = 90,
    [int]$MinFreeGB = 14,
    [int]$WarnFreeGB = 18,
    [string[]]$Only = @(),
    [string]$LogDir = '',
    [string]$ScriptsFrom = '',
    [string]$StageDir = 'C:\ProgramData\NSU-LLM-stage',
    [ValidateSet('task', 'session')][string]$RunMode = 'task',
    [string]$Proxy = '',
    [switch]$KeepStage,
    [switch]$UseSSL,
    [switch]$Laps,
    [switch]$AcceptCa,
    [switch]$InsecureTls,
    [switch]$NonInteractive,
    [switch]$Yes,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$FreeGBGiven = @{ Min = $PSBoundParameters.ContainsKey('MinFreeGB'); Warn = $PSBoundParameters.ContainsKey('WarnFreeGB') }   # $PSBoundParameters внутри функций — свои
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
# Windows PowerShell 5.1, запущенный из pwsh 7, наследует его PSModulePath, и модули 7.x (Microsoft.PowerShell.Security, WSMan,
# CimCmdlets) в нём не грузятся. Пути pwsh 7 убираются, свой каталог модулей — на месте.
if ($PSVersionTable.PSEdition -ne 'Core') {
    $mp = @("$env:PSModulePath" -split ';' | Where-Object { $_ -and $_ -notmatch '\\PowerShell\\(\d+\\)?Modules\\?$' })
    if ($mp -notcontains "$PSHOME\Modules") { $mp += "$PSHOME\Modules" }
    $env:PSModulePath = $mp -join ';'
}

$NodeDir = $(if ($ScriptsFrom) { $ScriptsFrom } else { $PSScriptRoot })   # откуда раздаются install-node.ps1 и соседи
$NodeFiles = @('install-node.ps1', 'run-node.ps1', 'node-boot.ps1', 'wsl-setup.sh', 'pull-image.sh', 'IMAGE_VERSION')   # то, что install-node.ps1 берёт рядом с собой
$HelperFiles = @('secure-dir.ps1', 'class-run.ps1')   # общие помощники: из -ScriptsFrom, иначе рядом с этим скриптом
$FootprintLimitGB = 16                             # вся нода на ПК класса укладывается в 16 ГБ диска
$Modes = @('k3s', 'standalone')
$Accels = @('auto', 'cpu', 'nvidia')
$Dockers = @('wsl', 'desktop')                     # wsl: Docker CE в WSL с автозапуском без входа (и при уже стоящем Docker Desktop)
$NameRe = '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'
$MinDriver = 570                                   # как в install-node.ps1: образ собран на CUDA 12.8
$DeployTask = 'NSU-LLM-Deploy'; $DeployTaskPath = '\NSU-LLM\'   # разовая задача запуска установщика (-RunMode task)
$script:Secrets = New-Object System.Collections.ArrayList   # токены и пароли: вырезаются из журналов и вывода

# ------------------------------------------------------------------ вспомогательное
# Прокси с паролем в выводе: http://user:***@host (как в install-node.ps1).
function Get-MaskedUrl([string]$u) { return ($u -replace '^(https?://[^:@/]+):[^@/]+@', '$1:***@') }
function Fail([string]$msg, [string]$fix = '') { $e = New-Object System.Exception($msg); $e.Data['fix'] = $fix; throw $e }
function Say([string]$t) { Write-Host $t }
function Warn([string]$t) { Write-Host "Внимание: $t" -ForegroundColor Yellow }
function Show-Dry([string]$t) { Write-Host "[dry-run] $t" }
# Без командлетов *-SecureString (Microsoft.PowerShell.Security может не грузиться): DPAPI напрямую, формат как у ConvertFrom-SecureString.
function Protect-UserText([string]$plain) {
    Add-Type -AssemblyName System.Security
    $b = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::Unicode.GetBytes($plain), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}
function Unprotect-UserText([string]$hex) {
    Add-Type -AssemblyName System.Security
    $bytes = New-Object byte[] ([int]($hex.Length / 2))
    for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($hex.Substring(2 * $i, 2), 16) }
    return [Text.Encoding]::Unicode.GetString([Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
}
function New-SecureText([string]$plain) {
    $s = New-Object Security.SecureString
    foreach ($ch in $plain.ToCharArray()) { $s.AppendChar($ch) }
    $s.MakeReadOnly()
    return $s
}
# Помощник (secure-dir.ps1, class-run.ps1): из -ScriptsFrom, иначе рядом с class-deploy.ps1.
function Get-HelperPath([string]$name) {
    foreach ($d in @($NodeDir, $PSScriptRoot)) { $f = Join-Path $d $name; if (Test-Path -LiteralPath $f) { return $f } }
    Fail "Нет файла $name" 'Он лежит в папке node репозитория рядом с class-deploy.ps1; запускайте скрипт оттуда.'
}
function ConvertTo-Plain([Security.SecureString]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Add-Secret([string]$s) { if ($s -and $s.Length -ge 4 -and -not $script:Secrets.Contains($s)) { [void]$script:Secrets.Add($s) } }
function Hide-Secrets([string]$t) {
    if (-not $t) { return '' }
    foreach ($s in $script:Secrets) { $t = $t.Replace($s, '***') }
    return $t
}
function Get-Prop($obj, [string]$n) { if ($obj -and $obj.PSObject.Properties[$n]) { return $obj.$n }; return $null }
# Вопрос с подставленным значением по умолчанию. Ввод из конвейера (тесты, автоматизация) читается построчно.
function Read-Answer([string]$q, [string]$def = '', [switch]$Secret) {
    $prompt = $(if ($def -and -not $Secret) { "$q [$def]" } else { $q })
    if ([Console]::IsInputRedirected) {
        Write-Host -NoNewline "${prompt}: "
        $a = [Console]::In.ReadLine(); if ($null -eq $a) { $a = '' }
        Write-Host ''
    }
    elseif ($Secret) { $a = ConvertTo-Plain (Read-Host -AsSecureString $prompt) }
    else { $a = Read-Host $prompt }
    $a = $a.Trim()
    if (-not $a) { return $def }
    return $a
}
function Confirm-Yes([string]$q, [string]$def = 'д') { return ((Read-Answer $q $def) -match '^(д|да|y|yes)$') }
function Test-Ipv4([string]$s) { $ip = $null; return ([Net.IPAddress]::TryParse($s, [ref]$ip) -and $ip.AddressFamily -eq 'InterNetwork') }
function Format-Size([long]$b) { return ('{0:N1} ГБ' -f ($b / 1GB)) }

# Эти четыре функции читают окружение администратора; тесты определяют свои до запуска скрипта.
if (-not (Test-Path Function:\Test-Ports)) {
    # Параллельная проверка TCP-портов: адрес -> достижим ли.
    function Test-Ports([string[]]$addrs, [int]$port) {
        $res = @{}; $pend = @{}
        foreach ($a in $addrs) {
            $c = New-Object Net.Sockets.TcpClient
            try { $pend[$a] = @{ C = $c; T = $c.BeginConnect($a, $port, $null, $null) } } catch { $res[$a] = $false; $c.Close() }
        }
        $until = (Get-Date).AddSeconds(4)
        foreach ($a in @($pend.Keys)) {
            $left = [int][math]::Max(100, ($until - (Get-Date)).TotalMilliseconds)
            try { $res[$a] = ($pend[$a].T.AsyncWaitHandle.WaitOne($left) -and $pend[$a].C.Connected) } catch { $res[$a] = $false }
            $pend[$a].C.Close()
        }
        return $res
    }
}
if (-not (Test-Path Function:\Get-TrustedHosts)) {
    function Get-TrustedHosts { try { return [string](Get-Item WSMan:\localhost\Client\TrustedHosts -ErrorAction Stop).Value } catch { return '' } }
}
if (-not (Test-Path Function:\Set-TrustedHosts)) {
    function Set-TrustedHosts([string]$value) { Set-Item WSMan:\localhost\Client\TrustedHosts -Value $value -Force }
}
if (-not (Test-Path Function:\Test-DomainJoined)) {
    function Test-DomainJoined { try { return [bool](Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).PartOfDomain } catch { return $false } }
}

# ------------------------------------------------------------------ инвентарь
# Адреса через запятую; диапазон последнего октета: 192.168.3.101-115.
function Expand-Addresses([string]$text) {
    $out = New-Object System.Collections.ArrayList
    foreach ($item in ($text -split '[,;\s]+' | Where-Object { $_ })) {
        if ($item -match '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.)(\d{1,3})-(\d{1,3})$') {
            $lo = [int]$matches[2]; $hi = [int]$matches[3]
            if ($hi -lt $lo -or $hi -gt 254) { Fail "Недопустимый диапазон: $item" 'Пример: 192.168.3.101-115' }
            for ($i = $lo; $i -le $hi; $i++) { [void]$out.Add($matches[1] + $i) }
        }
        else { [void]$out.Add($item) }
    }
    return @($out)
}
function New-InventoryWizard([string]$path) {
    Say "Файл инвентаря $path не найден. Создаю его по ответам (потом его можно править вручную: name,address,mode,accel,docker)."
    $addrs = @()
    while ($addrs.Count -eq 0) {
        $addrs = Expand-Addresses (Read-Answer 'Адреса ПК класса через запятую; диапазон — 192.168.3.101-115')
        if ($addrs.Count -eq 0) { Warn 'нужен хотя бы один адрес' }
    }
    $prefix = (Read-Answer 'Префикс имён нод (pc -> pc01, pc02, ...)' 'pc').ToLowerInvariant()
    $mode = Read-Answer 'Режим нод: k3s или standalone' 'k3s'
    $acc = Read-Answer 'Ускоритель: auto (по наличию GPU), cpu или nvidia' 'auto'
    if ($mode -notin $Modes) { Fail "Неизвестный режим $mode" 'Допустимо: k3s, standalone.' }
    if ($acc -notin $Accels) { Fail "Неизвестный ускоритель $acc" 'Допустимо: auto, cpu, nvidia.' }
    $rows = New-Object System.Collections.ArrayList; $i = 0
    foreach ($a in $addrs) {
        $i++
        $name = $(if (Test-Ipv4 $a) { '{0}{1:D2}' -f $prefix, $i } else { ($a -split '\.')[0].ToLowerInvariant() })
        [void]$rows.Add([pscustomobject]@{ name = $name; address = $a; mode = $mode; accel = $acc; docker = 'wsl' })
    }
    if ($DryRun) { Show-Dry "создать $path ($($rows.Count) строк)" }
    else {
        $dir = Split-Path -Parent $path; if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $rows | Export-Csv -Path $path -NoTypeInformation -Encoding UTF8
        Say "Записано: $path ($($rows.Count) ПК)"
    }
    return @($rows)
}
function Read-Inventory([string]$path) {
    $rows = @(Import-Csv -Path $path -Encoding UTF8 | Where-Object { $_.name -and -not "$($_.name)".StartsWith('#') })
    if ($rows.Count -eq 0) { Fail "В $path нет ни одного ПК" 'Формат: заголовок name,address,mode,accel,docker и по строке на ПК (docker можно не указывать: wsl).' }
    $seenN = @{}; $seenA = @{}; $out = New-Object System.Collections.ArrayList; $n = 0
    foreach ($r in $rows) {
        $n++
        $name = "$($r.name)".Trim().ToLowerInvariant()
        $addr = "$(Get-Prop $r 'address')".Trim()
        $mode = "$(Get-Prop $r 'mode')".Trim().ToLowerInvariant(); if (-not $mode) { $mode = 'k3s' }
        $acc = "$(Get-Prop $r 'accel')".Trim().ToLowerInvariant(); if (-not $acc) { $acc = 'auto' }
        $dk = "$(Get-Prop $r 'docker')".Trim().ToLowerInvariant(); if (-not $dk) { $dk = 'wsl' }
        if ($name -cnotmatch $NameRe) { Fail "$path`: строка ${n}: недопустимое имя '$name'" 'Латинские буквы, цифры и дефис, до 63 символов.' }
        if (-not $addr) { Fail "$path`: строка ${n} ($name): нет адреса" 'Заполните колонку address (IP или имя ПК).' }
        if ($mode -notin $Modes) { Fail "$path`: строка ${n} ($name): режим '$mode'" 'Допустимо: k3s, standalone.' }
        if ($acc -notin $Accels) { Fail "$path`: строка ${n} ($name): ускоритель '$acc'" 'Допустимо: auto, cpu, nvidia.' }
        if ($dk -notin $Dockers) { Fail "$path`: строка ${n} ($name): docker '$dk'" 'Допустимо: wsl (Docker CE в WSL, автозапуск без входа; по умолчанию), desktop (оставить уже установленный Docker Desktop).' }
        if ($seenN[$name]) { Fail "$path`: имя $name повторяется" 'Имена нод должны быть уникальны.' }
        if ($seenA[$addr]) { Fail "$path`: адрес $addr повторяется" 'Адреса должны быть уникальны.' }
        $seenN[$name] = $true; $seenA[$addr] = $true
        [void]$out.Add([pscustomobject]@{ Name = $name; Address = $addr; Mode = $mode; Accel = $acc; Docker = $dk; ImageLoaded = $false; Session = $null; Info = $null; Cred = $null; Status = ''; Detail = ''; Code = $null; Seconds = 0; Index = $n; Log = ''; Output = ''; Streamed = 0 })
    }
    return @($out)
}

# ------------------------------------------------------------------ ответы (один файл на весь класс)
# Версия образа: её называет главный (GET /api/node-image: NODE_IMAGE_TAG), установщик на ПК берёт образ оттуда; архив другой версии он
# не примет и скачает образ с главного. Главный не отвечает (или не задан): запасная версия скриптов, node\IMAGE_VERSION
# (раздаётся вместе с install-node.ps1). Ответ запоминается на время запуска.
$CurlExe = $(if ($env:NSU_LLM_CURL) { $env:NSU_LLM_CURL } else { 'curl.exe' })   # NSU_LLM_CURL: только для тестов
$script:MainForVersion = ''
$script:MainTagCache = @{}
function Get-MainTag([string]$m) {
    if (-not $m) { return '' }
    if ($script:MainTagCache.ContainsKey($m)) { return $script:MainTagCache[$m] }
    $v = ''
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        # -k: ответ публичный и проверяется регулярным выражением; CA частный и ещё не закреплён
        $body = (& $CurlExe -sk --noproxy '*' --connect-timeout 3 -m 6 "https://$m/api/node-image" 2>$null) -join ''
        if ($LASTEXITCODE -eq 0 -and $body) { $t = "$(($body | ConvertFrom-Json).tag)"; if ($t -match '^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$') { $v = $t } }
    }
    catch { }
    finally { $ErrorActionPreference = $prev }
    $script:MainTagCache[$m] = $v
    return $v
}
function Get-NodeVersion {
    $v = Get-MainTag $script:MainForVersion
    if ($v) { return $v }
    $f = Join-Path $NodeDir 'IMAGE_VERSION'
    if (-not (Test-Path -LiteralPath $f)) { return '' }
    $v = "$(Get-Content -LiteralPath $f -TotalCount 1 -ErrorAction SilentlyContinue)".Trim()
    return $(if ($v -match '^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$') { $v } else { '' })
}
function Get-TarPattern([string]$tag) { $v = Get-NodeVersion; return $(if ($v) { "nsu-llm-node-$v-$tag.tar*" } else { "nsu-llm-node-*-$tag.tar*" }) }
function Find-ImageTar([string]$tag, [string[]]$dirs) {
    $pat = Get-TarPattern $tag
    foreach ($d in ($dirs | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)) {
        $f = Get-ChildItem -Path $d -Filter $pat -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($f) { return $f.FullName }
    }
    return ''
}
function Resolve-MainIp([string]$m) {
    if (Test-Ipv4 $m) { return $m }
    try { return ([Net.Dns]::GetHostAddresses($m) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1).IPAddressToString } catch { return '' }
}
# 18080 (инференс) открыт только главному; на 18050 (RPC split) пускают главного и ноды группы split, их называет главный. Подсеть
# класса не открывается: ученик на соседнем ПК иначе подключился бы к RPC без аутентификации.
function Get-DefaultAllow($nodes, [string]$main) { return (Resolve-MainIp $main) }
# Сохранённый список прежней версии: подсеть /24, в которую входит ПК класса
function Test-LegacyAllow([string]$list, $nodes) {
    foreach ($e in ($list -split ',')) {
        if ($e -notmatch '^(\d+\.\d+\.\d+)\.\d+/(\d+)$' -or [int]$matches[2] -ge 32) { continue }
        $net = $matches[1]
        if (@($nodes | Where-Object { (Test-Ipv4 $_.Address) -and $_.Address.StartsWith("$net.") }).Count -gt 0) { return $true }
    }
    return $false
}
function Get-SavedToken($saved) {
    $enc = Get-Prop $saved 'AgentTokenProtected'
    if (-not $enc) { return '' }
    try { return Unprotect-UserText $enc } catch { Warn 'сохранённый токен не расшифровывается (другой пользователь или машина): введите заново'; return '' }
}
# Ответы: параметры > переменные окружения > файл > вопрос. Нужное для действия, но не заданное, при -NonInteractive — ошибка.
function Resolve-Answers([string]$path, $nodes, [string]$act) {
    $saved = $null
    if (Test-Path -LiteralPath $path) {
        try { $saved = Get-Content -Raw -Encoding UTF8 -Path $path | ConvertFrom-Json } catch { Warn "файл $path не читается как JSON и пропущен" }
    }
    $ask = -not $NonInteractive
    $needFull = $act -in @('install', 'update')
    $r = [ordered]@{}
    $r.Main = $(if ($Main) { $Main } elseif (Get-Prop $saved 'Main') { [string]$saved.Main } else { '' })
    $script:MainForVersion = $r.Main
    $r.Token = $(if ($Token) { $Token } elseif ($env:NSU_LLM_AGENT_TOKEN) { $env:NSU_LLM_AGENT_TOKEN } else { Get-SavedToken $saved })
    $r.AllowFrom = $(if ($AllowFrom) { $AllowFrom } elseif (Get-Prop $saved 'AllowFrom') { [string]$saved.AllowFrom } else { '' })
    if ($r.AllowFrom -and -not $AllowFrom -and (Test-LegacyAllow $r.AllowFrom $nodes)) {
        Warn "сохранённый AllowFrom ($($r.AllowFrom)) открывал порты всей подсети класса: теперь 18080 открыт только главному, 18050 главному и нодам группы split. Свой список: -AllowFrom."
        $r.AllowFrom = ''
    }
    $r.CaSha256 = $(if ($CaSha256) { $CaSha256 } elseif ($env:NSU_LLM_CA_SHA256) { $env:NSU_LLM_CA_SHA256 } elseif (Get-Prop $saved 'CaSha256') { [string]$saved.CaSha256 } else { '' })
    $r.CaSha256 = ($r.CaSha256 -replace '[:\s]', '').ToLowerInvariant()
    if ($r.CaSha256 -and $r.CaSha256 -notmatch '^[0-9a-f]{64}$') { Fail 'Недопустимый -CaSha256: нужны 64 шестнадцатеричные цифры' 'Отпечаток SHA-256 корня CA главного: страница «Подключение ноды» портала.' }
    $r.AcceptCa = [bool]($AcceptCa -or (Get-Prop $saved 'AcceptCa'))
    $dirs = @($NodeDir, (Split-Path -Parent $NodeDir), (Get-Location).Path, (Split-Path -Parent $path))
    # Источник образа: архив (-ImageTarCpu/-ImageTarGpu или nsu-llm-node-*.tar.gz рядом) или главный сервер (реестр https://<главный>/v2/):
    # без архива ПК класса скачивают образ с главного сами (умолчание); -ImageSource main игнорирует архивы, -ImageSource tar требует их.
    $r.ImageSource = $(if ($ImageSource) { $ImageSource } elseif (Get-Prop $saved 'ImageSource') { [string]$saved.ImageSource } else { '' })
    if ($r.ImageSource -notin @('', 'main', 'tar')) { Fail "Неизвестный источник образа $($r.ImageSource) в $path" 'Укажите -ImageSource main (с главного) или -ImageSource tar (из архива).' }
    $fromMain = ($r.ImageSource -eq 'main')
    $r.ImageTarCpu = $(if ($fromMain) { '' } elseif ($ImageTarCpu) { $ImageTarCpu } elseif (Get-Prop $saved 'ImageTarCpu') { [string]$saved.ImageTarCpu } else { Find-ImageTar 'cpu' $dirs })
    $r.ImageTarGpu = $(if ($fromMain) { '' } elseif ($ImageTarGpu) { $ImageTarGpu } elseif (Get-Prop $saved 'ImageTarGpu') { [string]$saved.ImageTarGpu } else { Find-ImageTar 'gpu' $dirs })
    $r.InsecureTls = [bool]($InsecureTls -or (Get-Prop $saved 'InsecureTls'))

    $needMain = $needFull -or ($act -eq 'uninstall')
    if ($needMain -and -not $r.Main -and $ask) { $r.Main = Read-Answer 'Адрес главного сервера (IP или имя)' }
    $script:MainForVersion = $r.Main
    if ($needMain -and -not $r.Token -and $ask) { $r.Token = Read-Answer 'Токен агента (портал, «Подключение ноды»; ввод скрыт)' -Secret }
    if ($needFull) {
        if (-not $r.Main) { Fail 'Не задан адрес главного сервера' 'Параметр -Main или запуск без -NonInteractive; пример: -Main 10.0.0.5' }
        if (-not $r.Token) { Fail 'Не задан токен агента' 'Параметр -Token, переменная NSU_LLM_AGENT_TOKEN или запуск без -NonInteractive (токен: портал, «Подключение ноды»).' }
        $defAllow = Get-DefaultAllow $nodes $r.Main
        if (-not $r.AllowFrom -and -not $defAllow) { Fail "Адрес главного $($r.Main) не разрешается в IPv4: брандмауэру нечего разрешить" 'Укажите -AllowFrom <адрес главного> (через запятую можно несколько адресов).' }
        if (-not $r.AllowFrom) { $r.AllowFrom = $(if ($ask) { Read-Answer 'С каких адресов пускать на 18080 (инференс; через запятую; на 18050 пускают главного и ноды группы split)' $defAllow } else { $defAllow }) }
        $r.AllowFrom = $r.AllowFrom.Replace(' ', '')
        if ($r.AllowFrom -notmatch '^[0-9./]+(,[0-9./]+)*$') { Fail "Недопустимый список адресов: $($r.AllowFrom)" 'Пример: 10.0.0.5 или 10.0.0.5,192.168.3.7' }
        $needCpu = @($nodes | Where-Object { $_.Accel -ne 'nvidia' }).Count -gt 0
        $needGpu = @($nodes | Where-Object { $_.Accel -ne 'cpu' }).Count -gt 0
        foreach ($t in @(@('ImageTarCpu', $needCpu, 'cpu'), @('ImageTarGpu', $needGpu, 'gpu'))) {
            if (-not $t[1] -or $fromMain) { continue }
            $pat = (Get-TarPattern $t[2]) -replace '\.tar\*$', '.tar.gz'
            if (-not $r[$t[0]] -and $ask -and $r.ImageSource -eq 'tar') { $r[$t[0]] = Read-Answer "Файл образа ноды ($($t[2]), $pat)" }
            if (-not $r[$t[0]] -and $r.ImageSource -ne 'tar') { Say "Образ $($t[2]): архива $pat нет, ПК класса скачают его с главного ($($r.Main), https://<главный>/v2/)."; continue }
            if (-not $r[$t[0]]) { Fail "Не задан файл образа $($t[2])" "Положите $pat рядом со скриптом или укажите -$($t[0]) <путь> (docker save: docs\deploy-class.md, «Распространение образов»)." }
            if (-not (Test-Path -LiteralPath $r[$t[0]])) { Fail "Файл образа $($r[$t[0]]) не найден" "Укажите -$($t[0]) <путь к $pat>." }
        }
    }
    if ($r.Token) { Add-Secret $r.Token }
    # Сохраняем, чтобы повторный запуск шёл без вопросов (токен — под DPAPI текущего пользователя).
    if ($needFull -and -not $DryRun) {
        $save = [ordered]@{ Main = $r.Main; AllowFrom = $r.AllowFrom; CaSha256 = $r.CaSha256; AcceptCa = $r.AcceptCa; ImageTarCpu = $r.ImageTarCpu; ImageTarGpu = $r.ImageTarGpu; ImageSource = $r.ImageSource; InsecureTls = $r.InsecureTls
            AgentTokenProtected = $(if ($r.Token) { Protect-UserText $r.Token } else { '' }) }
        $dir = Split-Path -Parent $path; if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        [pscustomobject]$save | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $path
    }
    elseif ($needFull) { Show-Dry "сохранить ответы в $path" }
    return [pscustomobject]$r
}
function Resolve-Credential {
    if ($Credential) { return $Credential }
    $u = $env:NSU_LLM_ADMIN_USER; $p = $env:NSU_LLM_ADMIN_PASSWORD
    if (-not $u -and -not $NonInteractive) { $u = Read-Answer 'Учётная запись администратора ПК класса (локальная: .\admin или ИМЯПК\admin, доменная: ДОМЕН\логин)' "$env:USERDOMAIN\$env:USERNAME" }
    if (-not $u) { Fail 'Не задана учётная запись администратора ПК' 'Параметр -Credential или переменные NSU_LLM_ADMIN_USER и NSU_LLM_ADMIN_PASSWORD.' }
    if (-not $p -and -not $NonInteractive) { $p = Read-Answer "Пароль $u (ввод скрыт; он же станет паролем задачи автозапуска на нодах)" -Secret }
    if (-not $p) { Fail 'Не задан пароль администратора ПК' 'Параметр -Credential или переменная NSU_LLM_ADMIN_PASSWORD.' }
    return New-Object pscredential($u, (New-SecureText $p))
}

# ------------------------------------------------------------------ WinRM: достижимость и подсказки
function Show-WinRmAdvice($failed, [bool]$trustNeeded, [string]$trustValue) {
    if ($failed.Count -eq 0) { return }
    Say ''
    Say 'Нет связи по WinRM:'
    foreach ($f in $failed) { Say ("  {0,-10} {1,-18} {2}" -f $f.Name, $f.Address, $f.Detail) }
    Say ''
    Say 'Как включить WinRM. На каждом таком ПК, PowerShell от администратора (один раз; можно по GPO или через psexec):'
    Say '  Enable-PSRemoting -Force -SkipNetworkProfileCheck'
    Say 'Если ПК в рабочей группе: входите встроенной учётной записью Administrator (включить: net user Administrator /active:yes и задать пароль) или доменной.'
    Say '  Локальную учётную запись, которая не встроенный Administrator, WinRM по сети не пускает (фильтр токена UAC). Рекомендуемая в интернете'
    Say '  LocalAccountTokenFilterPolicy=1 снимает фильтр для всех локальных администраторов и открывает вход по хешу пароля на всех ПК, где задан'
    Say '  одинаковый пароль, поэтому здесь она не рекомендуется.'
    Say 'Канал: WinRM по HTTP не проверяет сервер. Лучше WinRM по HTTPS на ПК (New-SelfSignedCertificate или сертификат вашего CA, затем'
    Say '  winrm quickconfig -transport:https) и -UseSSL здесь, либо Kerberos (ПК и администратор в домене, ПК по имени, не по IP).'
    Say 'Пароль: один пароль администратора на весь класс опасен (достали из LSA одного ПК, вошли на все); -Laps берёт у каждого ПК свой из Windows LAPS.'
    Say 'Если порт 5985 закрыт и после этого, проверьте брандмауэр: Get-NetFirewallRule -Name WINRM-HTTP-In-TCP* | Select Name,Profile,Enabled,RemoteAddress'
    if ($trustNeeded) {
        Say 'На этом ПК (администратора), PowerShell от администратора, чтобы разрешить вход по IP и в рабочей группе:'
        Say "  Set-Item WSMan:\localhost\Client\TrustedHosts -Value '$trustValue' -Force"
    }
}
# Открывает сессии ко всем ПК. Недоступным ставит Status='НЕДОСТУПЕН' и причину.
# Учётная запись для ПК: своя (LAPS, -Laps) или общая.
function Get-NodeCred($node, [pscredential]$cred) { if ($node.Cred) { return $node.Cred }; return $cred }
# -Laps: у каждого ПК свой пароль локального администратора в Windows LAPS; один пароль на весь класс означает, что
# взлом одного ПК открывает все остальные.
function Resolve-LapsCredentials($nodes) {
    if (-not (Get-Command Get-LapsADPassword -ErrorAction SilentlyContinue)) {
        Fail '-Laps: нет командлета Get-LapsADPassword (модуль LAPS: Windows 11 22H2 и новее или RSAT; ПК администратора в домене)' 'Установите RSAT или уберите -Laps и заведите на ПК класса отдельную учётную запись с уникальным паролем.'
    }
    foreach ($n in $nodes) {
        try {
            $r = Get-LapsADPassword -Identity $n.Name -AsPlainText -ErrorAction Stop
            if (-not $r -or -not $r.Password) { throw 'пароль не выдан' }
            Add-Secret ([string]$r.Password)
            $n.Cred = New-Object pscredential("$($n.Name)\$(if ($r.Account) { $r.Account } else { 'Administrator' })", (New-SecureText ([string]$r.Password)))
        }
        catch { $n.Status = 'НЕДОСТУПЕН'; $n.Detail = "пароль LAPS не получен: $(("$($_.Exception.Message)" -split "`r?`n")[0])" }
    }
}
function Connect-Nodes($nodes, [pscredential]$cred) {
    $port = $(if ($UseSSL) { 5986 } else { 5985 })
    $open = Test-Ports @($nodes | ForEach-Object { $_.Address }) $port
    # Профиль пользователя загружается: install-node.ps1 пишет %USERPROFILE%\.wslconfig, WSL и DPAPI живут в профиле.
    $sessOpt = New-PSSessionOption -OpenTimeout 20000 -OperationTimeout 120000
    foreach ($n in $nodes) {
        if ($n.Status -eq 'НЕДОСТУПЕН') { continue }
        $nc = Get-NodeCred $n $cred
        if (-not $open[$n.Address]) { $n.Status = 'НЕДОСТУПЕН'; $n.Detail = "порт $port закрыт или ПК выключен"; continue }
        try { Test-WSMan -ComputerName $n.Address -Credential $nc -Authentication Default -UseSSL:$UseSSL -ErrorAction Stop | Out-Null }
        catch { $n.Status = 'НЕДОСТУПЕН'; $n.Detail = "WinRM не отвечает: $(("$($_.Exception.Message)" -split "`r?`n")[0])"; continue }
        try {
            $n.Session = New-PSSession -ComputerName $n.Address -Credential $nc -SessionOption $sessOpt -UseSSL:$UseSSL -ErrorAction Stop
        }
        catch { $n.Status = 'НЕДОСТУПЕН'; $n.Detail = "вход не удался: $(("$($_.Exception.Message)" -split "`r?`n")[0]) (учётная запись, LocalAccountTokenFilterPolicy)" }
    }
}

# ------------------------------------------------------------------ удалённые блоки (выполняются на ПК класса)
$BlockProbe = {
    param($p)
    $files = @{}
    if (Test-Path -LiteralPath $p.StageDir) { Get-ChildItem -LiteralPath $p.StageDir -File -ErrorAction SilentlyContinue | ForEach-Object { $files[$_.Name] = [long]$_.Length } }
    $mk = Join-Path $p.StageDir 'image-loaded.json'; $mark = ''
    try { if (Test-Path -LiteralPath $mk) { $mark = [IO.File]::ReadAllText($mk) } } catch { }
    $gpu = $false; $drv = ''; $major = 0
    try {
        $o = @(& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1)
        if ($LASTEXITCODE -eq 0 -and $o.Count) { $drv = "$($o[0])".Trim(); $gpu = [bool]$drv; [void][int]::TryParse(($drv -split '\.')[0], [ref]$major) }
    }
    catch { }
    $free = 0.0
    try { $sd = $(if ($env:SystemDrive) { $env:SystemDrive.TrimEnd(':') } else { 'C' }); $free = [math]::Round((Get-PSDrive -Name $sd -PSProvider FileSystem).Free / 1GB, 1) } catch { }
    $adm = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    # Сколько места занимает сама нода: каталог установки (диск дистрибутива ext4.vhdx, скрипты) и раздача (архив образа).
    $fp = 0.0
    try {
        foreach ($d in @((Join-Path $env:ProgramData 'NSU-LLM'), $p.StageDir)) {
            if (Test-Path -LiteralPath $d) { foreach ($f in @(Get-ChildItem -LiteralPath $d -Recurse -File -Force -ErrorAction SilentlyContinue)) {
                # место на диске, а не длина: длина разреженного ext4.vhdx не уменьшается после удаления модели
                $sz = [double]$f.Length
                try {
                    if (-not ('NsuLlm.DiskSize' -as [type])) { Add-Type -Namespace NsuLlm -Name DiskSize -MemberDefinition '[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] public static extern uint GetCompressedFileSizeW(string name, out uint high);' }
                    $hi = [uint32]0; $lo = [NsuLlm.DiskSize]::GetCompressedFileSizeW($f.FullName, [ref]$hi)
                    if ($lo -ne [uint32]::MaxValue) { $sz = [double]$hi * 4294967296 + $lo }
                } catch { }
                $fp += $sz
            } }
        }
        $fp = [math]::Round($fp / 1GB, 1)
    } catch { }
    [pscustomobject]@{ Kind = 'probe'; ImageMark = $mark; Computer = $env:COMPUTERNAME; IsAdmin = $adm; HasGpu = $gpu; Driver = $drv; DriverMajor = $major; FreeGB = $free; FootprintGB = $fp; StageFiles = $files
        Installed = (Test-Path -LiteralPath (Join-Path $env:ProgramData 'NSU-LLM\install.json')); Build = [Environment]::OSVersion.Version.Build }
}
# Запуск install-node.ps1 на ПК. Вывод идёт в run.log раздачи и по строкам ({Kind=line}) обратно, чтобы ход был виден сразу.
# task: разовая задача планировщика от учётки администратора (вход как пакетное задание: профиль, DPAPI, без двойного прыжка);
# аргументы с токеном и паролем — в файле под DPAPI LocalMachine с доступом только Администраторам и SYSTEM, файл удаляется при чтении.
# session: прямо в сессии WinRM (как раньше).
$BlockRun = {
    param($p)
    try { Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force -ErrorAction Stop } catch { }   # GPO может переопределить: тогда сообщит сам установщик
    # Раздача закрывается до того, как из неё что-то запускается или в неё пишется (Secure-Dir из node\secure-dir.ps1).
    . ([scriptblock]::Create($p.SecureDir))
    try { Secure-Dir -Path $p.StageDir }
    catch { return [pscustomobject]@{ Kind = 'run'; Code = 1; Output = "ОШИБКА: каталог раздачи не защищён: $($_.Exception.Message)" } }
    # Свежие файлы раздачи важнее; если их нет, берём уже установленную копию.
    $script = Join-Path $p.StageDir 'install-node.ps1'
    if (-not (Test-Path -LiteralPath $script)) {
        # Установленная копия выполняется от администратора: только из доверенного каталога (wsl\ — диск дистрибутива, не проверяется).
        $inst = Join-Path $env:ProgramData 'NSU-LLM'
        $script = Join-Path $inst 'scripts\install-node.ps1'
        if ((Test-Path -LiteralPath $script) -and -not (Test-TrustedPath $inst -Exclude 'wsl')) {
            return [pscustomobject]@{ Kind = 'run'; Code = 1; Output = "ОШИБКА: $inst изменён не администратором (чужой владелец или права на запись): установленную копию install-node.ps1 запускать нельзя`nКак исправить: раздайте скрипты заново (-CopyMode auto) — install-node.ps1 из раздачи отложит недоверенное и восстановит каталог." }
        }
    }
    if (-not (Test-Path -LiteralPath $script)) {
        if ($p.Action -eq 'status') { return [pscustomobject]@{ Kind = 'run'; Code = 1; Output = '[!!] нода не установлена (нет install-node.ps1)' } }
        if ($p.Action -eq 'uninstall') {
            if ($p.Cleanup -eq 'all') { Remove-Item -LiteralPath $p.StageDir -Recurse -Force -ErrorAction SilentlyContinue }
            return [pscustomobject]@{ Kind = 'run'; Code = 0; Output = 'нода не установлена, удалять нечего' }
        }
        return [pscustomobject]@{ Kind = 'run'; Code = 127; Output = "ОШИБКА: нет $($p.StageDir)\install-node.ps1: файлы не раздавались (-CopyMode none?)" }
    }
    $a = @{}
    # Токен и пароль пришли как SecureString (зашифрованы ключом сеанса PSRP): открытым текстом они только здесь, в памяти ПК, и дальше
    # в файле параметров под DPAPI LocalMachine.
    foreach ($k in $p.Args.Keys) {
        $v = $p.Args[$k]
        if ($v -is [Security.SecureString]) { $v = (New-Object Net.NetworkCredential('', $v)).Password }
        $a[$k] = $v
    }
    $taskPw = $p.TaskPassword
    if ($taskPw -is [Security.SecureString]) { $taskPw = (New-Object Net.NetworkCredential('', $taskPw)).Password }
    # Прокси: установщик с -Proxy сам настраивает wsl, apt, Docker и NO_PROXY; старому — хотя бы переменные окружения.
    $envProxy = ''
    if ($p.Proxy) {
        $decl = $false; try { $decl = (Get-Command -Name $script -ErrorAction Stop).Parameters.ContainsKey('Proxy') } catch { }
        if ($decl) { $a['Proxy'] = $p.Proxy } else { $envProxy = $p.Proxy; $env:HTTP_PROXY = $p.Proxy; $env:HTTPS_PROXY = $p.Proxy }
    }
    $log = Join-Path $p.StageDir 'run.log'; $done = Join-Path $p.StageDir 'run.done'; $mark = Join-Path $p.StageDir 'image-loaded.json'
    $actFile = Join-Path $p.StageDir 'run.action'
    $runner = Join-Path $p.StageDir 'class-run.ps1'; $pf = Join-Path $p.StageDir 'run-params.bin'
    $want = $(if ($p.Action -eq 'uninstall') { 'uninstall' } else { 'install' })   # install и update — один и тот же установщик
    # Прошлый запуск ещё идёт (связь с ПК администратора оборвалась, а задача на ПК продолжается): тот же вид запуска не начинается
    # заново, а подключается к идущему и показывает его журнал с начала. Другое действие на ПК с идущим запуском — ошибка.
    $attach = $false
    $old = $null
    if ($p.RunMode -eq 'task') {
        $tn = $p.TaskName; $tp = $p.TaskPath
        try { $old = Get-ScheduledTask -TaskPath $tp -TaskName $tn -ErrorAction Stop } catch { }
        if ($old -and "$($old.State)" -eq 'Running') {
            $has = ''; try { $has = ([IO.File]::ReadAllText($actFile)).Trim() } catch { }
            if ($p.Action -ne 'status' -and $has -eq $want) { $attach = $true }
            else {
                return [pscustomobject]@{ Kind = 'run'; Code = 1; Output = "ОШИБКА: на ПК ещё идёт прошлый запуск (задача $tp$tn$(if ($has) { ", $has" }))`nКак исправить: дождитесь его конца (журнал $log) или остановите: Stop-ScheduledTask -TaskPath '$tp' -TaskName $tn" }
            }
        }
    }
    if (-not $attach) { Remove-Item -LiteralPath $log, $done -Force -ErrorAction SilentlyContinue }
    $enc = New-Object Text.UTF8Encoding $false
    $code = 0; $extra = ''
    if ($p.RunMode -eq 'task') {
        if ($attach) { [pscustomobject]@{ Kind = 'line'; Text = "Прошлый запуск на ПК ещё идёт (задача $tp$tn): показываю его журнал, новые параметры не применялись" } }
        else {
            if ($old) { Unregister-ScheduledTask -TaskPath $tp -TaskName $tn -Confirm:$false }
            Write-SecureFile -Path $runner -Bytes ((New-Object Text.UTF8Encoding $true).GetPreamble() + (New-Object Text.UTF8Encoding $false).GetBytes($p.Runner))
            Add-Type -AssemblyName System.Security
            $json = [pscustomobject]@{ Script = $script; Args = $a; EnvProxy = $envProxy } | ConvertTo-Json -Depth 4
            # Файл с первого байта доступен только SYSTEM и Administrators (Write-SecureFile -AdminOnly), содержимое под DPAPI LocalMachine.
            try { Write-SecureFile -Path $pf -AdminOnly -Bytes ([Security.Cryptography.ProtectedData]::Protect($enc.GetBytes($json), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)) }
            catch { return [pscustomobject]@{ Kind = 'run'; Code = 1; Output = "ОШИБКА: не удалось записать параметры запуска: $($_.Exception.Message)" } }
            $json = $null
            [IO.File]::WriteAllText($actFile, $want)
            $user = [string]$p.TaskUser
            if ($user -like '.\*') { $user = "$env:COMPUTERNAME\" + $user.Substring(2) }
            $arg = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Params `"$pf`" -Log `"$log`" -Done `"$done`""
            $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
            $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::FromSeconds($p.TimeoutSec + 1800)) -MultipleInstances IgnoreNew
            try {
                Register-ScheduledTask -TaskName $tn -TaskPath $tp -Action $action -Settings $set -User $user -Password $taskPw -RunLevel Highest `
                    -Description 'NSU LLM: разовый запуск install-node.ps1 из class-deploy.ps1' -ErrorAction Stop | Out-Null
                Start-ScheduledTask -TaskPath $tp -TaskName $tn -ErrorAction Stop
            }
            catch {
                Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue
                return [pscustomobject]@{ Kind = 'run'; Code = 1; Output = "ОШИБКА: не удалось создать или запустить задачу $tp$tn от $user`: $($_.Exception.Message)`nКак исправить: проверьте пароль учётки и право «Вход в качестве пакетного задания» (у Администраторов оно есть, если GPO его не забирает) или запустите с -RunMode session" }
            }
        }
        # Новые строки run.log -> {Kind=line}; неполная последняя строка ждёт следующего чтения.
        $dec = $enc.GetDecoder(); $st = @{ Pos = [long]0; Buf = '' }
        $pull = {
            param([switch]$Flush)
            if (Test-Path -LiteralPath $log) {
                $fs = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
                try {
                    [void]$fs.Seek($st.Pos, 'Begin'); $bytes = New-Object byte[] ([int][math]::Max(0, $fs.Length - $st.Pos)); $n = $fs.Read($bytes, 0, $bytes.Length); $st.Pos += $n
                }
                finally { $fs.Close() }
                if ($n -gt 0) { $chars = New-Object char[] ($n + 4); $c = $dec.GetChars($bytes, 0, $n, $chars, 0); $st.Buf += [string]::new($chars, 0, $c) }
            }
            $parts = $st.Buf -split "`n"
            $st.Buf = $parts[-1]
            for ($i = 0; $i -lt $parts.Count - 1; $i++) { [pscustomobject]@{ Kind = 'line'; Text = $parts[$i].TrimEnd("`r") } }
            if ($Flush -and $st.Buf) { [pscustomobject]@{ Kind = 'line'; Text = $st.Buf.TrimEnd("`r") }; $st.Buf = '' }
        }
        $t0 = Get-Date; $deadline = $t0.AddSeconds($p.TimeoutSec); $why = ''
        while ($true) {
            & $pull
            if (Test-Path -LiteralPath $done) { break }
            if ((Get-Date) -gt $deadline) { $why = 'timeout'; break }
            if (((Get-Date) - $t0).TotalSeconds -gt 20) {
                $ts = ''; try { $ts = "$((Get-ScheduledTask -TaskPath $tp -TaskName $tn -ErrorAction Stop).State)" } catch { }
                if ($ts -notin @('Running', 'Queued') -and -not (Test-Path -LiteralPath $done)) { $why = 'dead'; break }
            }
            Start-Sleep -Seconds 2
        }
        & $pull -Flush
        if ($why -eq 'timeout') {
            $code = 124
            $extra = "ОШИБКА: не завершилось за $([math]::Round($p.TimeoutSec / 60, 1)) мин; установка продолжается в задаче $tp$tn`nКак исправить: дождитесь и проверьте: .\class-deploy.ps1 -Action status -Only $($p.Args['Name']) (журнал на ПК: $log)"
        }
        else {
            if ($why -eq 'dead') {
                $res = ''; try { $res = '0x{0:X}' -f [int](Get-ScheduledTaskInfo -TaskPath $tp -TaskName $tn -ErrorAction Stop).LastTaskResult } catch { }
                $code = 1; $extra = "ОШИБКА: задача $tp$tn завершилась без результата (LastTaskResult $res)`nКак исправить: проверьте журнал планировщика на ПК или запустите с -RunMode session"
            }
            else { $code = [int]("$(Get-Content -LiteralPath $done -Raw)".Trim()) }
            Unregister-ScheduledTask -TaskPath $tp -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $pf, $runner, $done -Force -ErrorAction SilentlyContinue
        }
    }
    else {
        $global:LASTEXITCODE = $null
        try {
            & $script @a *>&1 | ForEach-Object { $s = "$_"; [IO.File]::AppendAllText($log, $s + "`r`n", $enc); [pscustomobject]@{ Kind = 'line'; Text = $s } }
            if ($null -ne $LASTEXITCODE) { $code = [int]$LASTEXITCODE }
        }
        catch { $extra = "ОШИБКА: $($_.Exception.Message)"; $code = 1 }
    }
    if ($extra) { [IO.File]::AppendAllText($log, $extra + "`r`n", $enc); foreach ($l in ($extra -split "`n")) { [pscustomobject]@{ Kind = 'line'; Text = $l } } }
    $out = $(if (Test-Path -LiteralPath $log) { [IO.File]::ReadAllText($log, $enc) } else { '' })
    # Метка загруженного образа: после успеха с архивом — какой архив загружен (следующий запуск его не копирует и не передаёт);
    # если без архива установщик образа не нашёл (просит -ImageTar) — метка снимается, и следующий запуск скопирует архив.
    if ($attach) { }   # шёл прошлый запуск, с какими образами — неизвестно: метка не меняется (нет образа — установщик попросит архив, и метка снимется)
    elseif ($code -eq 0 -and $p.ImageMark) { [IO.File]::WriteAllText($mark, [string]$p.ImageMark, $enc) }
    elseif ($code -ne 0 -and -not $a.ContainsKey('ImageTar') -and $out -match 'укажите -ImageTar') { Remove-Item -LiteralPath $mark -Force -ErrorAction SilentlyContinue }
    # Раздача после ошибки: архив образа остаётся, чтобы повтор не копировал его заново, но на почти полном диске (меньше -MinFreeGB) он
    # удаляется: копия в несколько ГБ в каталоге раздачи не должна добить системный диск.
    if ($code -ne 0 -and $p.Cleanup -eq 'stage' -and $p.MinFreeGB -gt 0) {
        $left = $null
        try { $sd = $(if ($env:SystemDrive) { $env:SystemDrive.TrimEnd(':') } else { 'C' }); $left = (Get-PSDrive -Name $sd -PSProvider FileSystem).Free / 1GB } catch { }
        if ($null -ne $left -and $left -lt $p.MinFreeGB) {
            Get-ChildItem -LiteralPath $p.StageDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'nsu-llm-node-*.tar*' } | Remove-Item -Force -ErrorAction SilentlyContinue
            $extra2 = "На диске осталось $([math]::Round($left, 1)) ГБ: архив образа удалён из $($p.StageDir), чтобы не заполнять диск; повторный запуск скопирует его заново, когда место появится."
            [IO.File]::AppendAllText($log, $extra2 + "`r`n", $enc); $out = $out + "`r`n" + $extra2
        }
    }
    # Раздача после успеха: uninstall — всё; install/update — всё, кроме run.log и метки образа (архив остаётся только с -KeepStage).
    if ($code -eq 0 -and $p.Cleanup -eq 'all') { Remove-Item -LiteralPath $p.StageDir -Recurse -Force -ErrorAction SilentlyContinue }
    elseif ($code -eq 0 -and $p.Cleanup -in @('stage', 'keep-image')) {
        Get-ChildItem -LiteralPath $p.StageDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notin @('run.log', 'image-loaded.json') -and -not ($p.Cleanup -eq 'keep-image' -and $_.Name -like 'nsu-llm-node-*.tar*') } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
    [pscustomobject]@{ Kind = 'run'; Code = $code; Output = $out }
}
$BlockTail = {
    param($p)
    $t = ''
    if (Test-Path -LiteralPath $p.Path) { $t = (Get-Content -LiteralPath $p.Path -Tail $p.Lines -Encoding UTF8) -join "`r`n" }
    [pscustomobject]@{ Kind = 'tail'; Text = $t }
}
# Каталог раздачи закрывается на ПК до копирования (текст node\secure-dir.ps1 уходит в блок). Обёртка задачи (-RunMode task) —
# node\class-run.ps1, её текст пишется в раздачу каждого ПК.
# Op=place: скопированные во временные имена файлы ставятся на место (новый объект вместо старого, как Write-SecureFile).
$BlockSecure = {
    param($p)
    try {
        . ([scriptblock]::Create($p.Code))
        if ($p.Op -eq 'place') { foreach ($i in $p.Items) { Move-SecureFileInto $i.Tmp $i.Path } }
        else { Secure-Dir -Path $p.Path }
        [pscustomobject]@{ Kind = 'secure'; Ok = $true; Error = '' }
    }
    catch { [pscustomobject]@{ Kind = 'secure'; Ok = $false; Error = "$($_.Exception.Message)" } }
}

# ------------------------------------------------------------------ параллельный запуск с ограничением
# $argsFor вызывается для узла и возвращает хэш-таблицу аргумента блока. $onDone получает узел, результат (или $null) и текст ошибки,
# $onLine — строки {Kind=line}, пока задание ещё идёт.
function Invoke-OnNodes($nodes, [scriptblock]$block, [scriptblock]$argsFor, [int]$limit, [scriptblock]$onDone, [scriptblock]$onLine = $null, [double]$timeout = $TimeoutMin) {
    $queue = New-Object System.Collections.Queue
    foreach ($n in $nodes) { $queue.Enqueue($n) }
    $run = @{}
    while ($queue.Count -gt 0 -or $run.Count -gt 0) {
        while ($queue.Count -gt 0 -and $run.Count -lt $limit) {
            $n = $queue.Dequeue()
            try {
                $job = Invoke-Command -Session $n.Session -ScriptBlock $block -ArgumentList @(, (& $argsFor $n)) -AsJob
                $run[$job.Id] = @{ Node = $n; Job = $job; Start = Get-Date; Res = $null; Errs = New-Object System.Collections.ArrayList }
            }
            catch { & $onDone $n $null "не запустилось: $($_.Exception.Message)" }
        }
        if ($run.Count -eq 0) { continue }
        $null = Wait-Job -Job @($run.Values | ForEach-Object { $_.Job }) -Any -Timeout 2
        foreach ($id in @($run.Keys)) {
            $e = $run[$id]; $state = [string]$e.Job.State
            $ev = $null; $got = @()
            try { $got = @(Receive-Job -Job $e.Job -ErrorAction SilentlyContinue -ErrorVariable ev) } catch { [void]$e.Errs.Add($_.Exception.Message) }
            foreach ($x in @($ev)) { if ($x) { [void]$e.Errs.Add("$x") } }
            foreach ($o in $got) {
                if (-not $o -or -not $o.PSObject.Properties['Kind']) { continue }
                if ($o.Kind -eq 'line') { if ($onLine) { & $onLine $e.Node ([string]$o.Text) } }
                else { $e.Res = $o }
            }
            if ($state -in @('Completed', 'Failed', 'Stopped', 'Blocked', 'Disconnected')) {
                $err = ''
                if ($state -ne 'Completed' -or -not $e.Res) {
                    $reason = $null; try { $reason = $e.Job.ChildJobs[0].JobStateInfo.Reason } catch { }
                    $err = $(if ($reason) { "$($reason.Message)" } elseif ($e.Errs.Count) { "$($e.Errs[0])" } else { "задание завершилось в состоянии $state" })
                }
                Remove-Job -Job $e.Job -Force -ErrorAction SilentlyContinue
                $run.Remove($id)
                & $onDone $e.Node $e.Res $err
            }
            elseif (((Get-Date) - $e.Start).TotalMinutes -gt $timeout) {
                Stop-Job -Job $e.Job -ErrorAction SilentlyContinue; Remove-Job -Job $e.Job -Force -ErrorAction SilentlyContinue
                $run.Remove($id)
                & $onDone $e.Node $null "не завершилось за $timeout мин"
            }
        }
    }
}
# Последние строки run.log с ПК (после обрыва задания или тайм-аута), не дольше минуты.
function Get-RemoteTail($node) {
    try {
        $j = Invoke-Command -Session $node.Session -ScriptBlock $BlockTail -ArgumentList @(, @{ Op = 'tail'; Path = (Join-Path $StageDir 'run.log'); Lines = 60 }) -AsJob
        $null = Wait-Job -Job $j -Timeout 60
        $t = @(Receive-Job -Job $j -ErrorAction SilentlyContinue) | Where-Object { $_ -and $_.PSObject.Properties['Kind'] } | Select-Object -Last 1
        Remove-Job -Job $j -Force -ErrorAction SilentlyContinue
        if ($t) { return [string]$t.Text }
    }
    catch { }
    return ''
}

# ------------------------------------------------------------------ раздача файлов
function Get-LocalFiles($answers, $node) {
    $files = New-Object System.Collections.ArrayList
    foreach ($f in $NodeFiles) {
        $p = Join-Path $NodeDir $f
        if (-not (Test-Path -LiteralPath $p)) { Fail "Нет файла $p" 'Запускайте class-deploy.ps1 из папки node репозитория: рядом должны лежать install-node.ps1, run-node.ps1, node-boot.ps1, wsl-setup.sh, pull-image.sh, IMAGE_VERSION.' }
        [void]$files.Add([pscustomobject]@{ Path = $p; Name = $f; Image = $false })
    }
    foreach ($f in $HelperFiles) { [void]$files.Add([pscustomobject]@{ Path = (Get-HelperPath $f); Name = $f; Image = $false }) }
    $tar = Get-ImageFor $answers $node
    if ($tar -and -not $node.ImageLoaded) { [void]$files.Add([pscustomobject]@{ Path = $tar; Name = (Split-Path -Leaf $tar); Image = $true }) }
    return @($files)
}
# Сколько байт образа ещё нужно доставить на ПК (0, если файл такой же длины уже лежит).
function Get-MissingImageBytes($answers, $node) {
    $t = Get-ImageFor $answers $node
    if (-not $t -or $node.ImageLoaded) { return 0 }
    $len = (Get-Item -LiteralPath $t).Length
    $have = Get-Prop $node.Info 'StageFiles'
    if ($have -and $have[(Split-Path -Leaf $t)] -eq $len) { return 0 }
    return $len
}
# GPU-образ нужен, если ускоритель nvidia или auto и на ПК нашёлся GPU с драйвером $MinDriver+; иначе cpu-образ
# (install-node.ps1 со старым драйвером ставит ноду как cpu).
function Test-OldDriver($node) { return ($node.Accel -eq 'auto' -and [bool](Get-Prop $node.Info 'HasGpu') -and [int](Get-Prop $node.Info 'DriverMajor') -lt $MinDriver) }
function Get-ImageFor($answers, $node) {
    $gpu = $(if ($node.Accel -eq 'nvidia') { $true } elseif ($node.Accel -eq 'cpu') { $false } else { [bool](Get-Prop $node.Info 'HasGpu') -and -not (Test-OldDriver $node) })
    return $(if ($gpu) { $answers.ImageTarGpu } else { $answers.ImageTarCpu })
}
# Метка архива образа: имя, длина и время изменения файла на ПК администратора и куда он загружен (docker: wsl или desktop).
function Get-ImageMark($answers, $node) {
    $i = Get-Item -LiteralPath (Get-ImageFor $answers $node)
    return ([pscustomobject][ordered]@{ Name = $i.Name; Length = [long]$i.Length; Ticks = [long]$i.LastWriteTimeUtc.Ticks; Docker = $node.Docker } | ConvertTo-Json -Compress)
}
# Образ уже загружен на ПК: нода установлена и метка последнего успешного запуска совпадает с нужным архивом.
function Test-ImageLoaded($answers, $node) {
    if (-not (Get-Prop $node.Info 'Installed')) { return $false }
    $m = [string](Get-Prop $node.Info 'ImageMark')
    if (-not $m -or -not (Get-ImageFor $answers $node)) { return $false }
    try { $o = $m | ConvertFrom-Json; $want = Get-ImageMark $answers $node | ConvertFrom-Json } catch { return $false }
    return ("$($o.Name)" -eq $want.Name -and "$($o.Length)" -eq "$($want.Length)" -and "$($o.Ticks)" -eq "$($want.Ticks)" -and "$($o.Docker)" -eq $want.Docker)
}
# Каталог раздачи на ПК: владелец Administrators, Users только чтение; чужой и несбрасываемый — ошибка, ПК пропускается.
function Protect-NodeStage($node) {
    $r = @(Invoke-Command -Session $node.Session -ScriptBlock $BlockSecure -ArgumentList @(, @{ Op = 'secure'; Path = $StageDir; Code = $script:SecureDirText })) |
        Where-Object { $_ -and $_.PSObject.Properties['Kind'] } | Select-Object -Last 1
    if (-not $r) { Fail "каталог раздачи $StageDir не защищён: ПК не ответил" }
    if (-not $r.Ok) { Fail "каталог раздачи не защищён: $($r.Error)" }
}
function Open-NodeShare($node, [pscredential]$cred) {
    if ($StageDir -notmatch '^[Cc]:\\') { return $null }
    $drive = 'NSUND' + $node.Index
    try { New-PSDrive -Name $drive -PSProvider FileSystem -Root "\\$($node.Address)\C$" -Credential $cred -Scope Global -ErrorAction Stop | Out-Null; return $drive } catch { return $null }
}
function Copy-ToNode($node, $files, [pscredential]$cred) {
    $stageRel = $StageDir.Substring(3)
    $mode = $CopyMode
    $drive = $null
    Protect-NodeStage $node
    if ($mode -in @('auto', 'smb')) {
        $drive = Open-NodeShare $node (Get-NodeCred $node $cred)
        if ($drive) { $mode = 'smb' }
        elseif ($mode -eq 'smb') { Fail "Админ-ресурс \\$($node.Address)\C$ недоступен" 'Включите «Общий доступ к файлам и принтерам» в брандмауэре ПК и проверьте учётку, либо используйте -CopyMode winrm.' }
        else { $mode = 'winrm' }
    }
    $have = Get-Prop $node.Info 'StageFiles'
    # Файлы ложатся во временные имена и затем ставятся на место на ПК: дескриптор, открытый к старому файлу раньше, нового не видит.
    # (Список файлов раздачи, прочитанный проверкой до Secure-Dir, влияет только на «уже на месте»: подложенный файл не запускается,
    # а в худшем случае лишь не дал бы скопировать образ — отказ в обслуживании, не выполнение кода.)
    $placed = New-Object System.Collections.ArrayList
    try {
        foreach ($f in $files) {
            $len = (Get-Item -LiteralPath $f.Path).Length
            if ($f.Image -and $have -and $have[$f.Name] -eq $len) { Say "  $($node.Name): $($f.Name) уже на месте"; continue }
            if ($f.Image) { Say "  $($node.Name): копирую $($f.Name) ($(Format-Size $len)) по $($mode.ToUpper())" }
            $tmp = ".$($f.Name).$([guid]::NewGuid().ToString('N')).tmp"
            if ($mode -eq 'smb') { Copy-Item -LiteralPath $f.Path -Destination "${drive}:\$stageRel\$tmp" -Force }
            else { Copy-Item -LiteralPath $f.Path -Destination (Join-Path $StageDir $tmp) -ToSession $node.Session -Force }
            [void]$placed.Add(@{ Tmp = (Join-Path $StageDir $tmp); Path = (Join-Path $StageDir $f.Name) })
        }
        if ($placed.Count) {
            $r = @(Invoke-Command -Session $node.Session -ScriptBlock $BlockSecure -ArgumentList @(, @{ Op = 'place'; Items = @($placed); Code = $script:SecureDirText })) |
                Where-Object { $_ -and $_.PSObject.Properties['Kind'] } | Select-Object -Last 1
            if (-not $r -or -not $r.Ok) { Fail "файлы раздачи не поставлены на место: $(if ($r) { $r.Error } else { 'ПК не ответил' })" }
        }
    }
    finally { if ($drive) { Remove-PSDrive -Name $drive -Scope Global -Force -ErrorAction SilentlyContinue } }
    return $mode
}

# ------------------------------------------------------------------ аргументы install-node.ps1 для действия
function Get-RunArgs([string]$act, $answers, $node, [pscredential]$cred) {
    $a = [ordered]@{}
    switch ($act) {
        { $_ -in @('install', 'update') } {
            $a.Main = $answers.Main; $a.Token = (New-SecureText $answers.Token); $a.Mode = $node.Mode; $a.Accel = $node.Accel; $a.Docker = $node.Docker; $a.Name = $node.Name
            $a.AllowFrom = $answers.AllowFrom; $a.NonInteractive = $true; $a.Yes = $true
            $a.TaskPassword = $cred.Password   # SecureString: на ПК уходит зашифрованным ключом сеанса, не строкой
            if ($answers.CaSha256) { $a.CaSha256 = $answers.CaSha256 }
            if ($answers.AcceptCa) { $a.AcceptCa = $true }
            $tar = Get-ImageFor $answers $node
            if ($tar -and -not $node.ImageLoaded) { $a.ImageTar = (Join-Path $StageDir (Split-Path -Leaf $tar)) }
            if ($answers.ImageSource -eq 'main') { $a.ImageSource = 'main' }
            elseif (-not $tar -and $answers.ImageSource -ne 'tar') { $a.ImageSource = 'main' }   # без архива: образ с главного, не архив, найденный рядом с установщиком на ПК
            if ($answers.InsecureTls) { $a.InsecureTls = $true }
            if ($a.Contains('ImageTar') -and -not $KeepStage) { $a.DeleteImageTar = $true }   # архив раздачи удаляется сразу после docker load, не в конце запуска
            if ($FreeGBGiven.Min) { $a.MinFreeGB = $MinFreeGB }
            if ($FreeGBGiven.Warn) { $a.WarnFreeGB = $WarnFreeGB }
        }
        'uninstall' {
            $a.Uninstall = $true; $a.NonInteractive = $true; $a.Yes = $true; $a.Name = $node.Name
            if ($answers.Main) { $a.Main = $answers.Main }
            if ($answers.Token) { $a.Token = (New-SecureText $answers.Token) }
            if ($answers.InsecureTls) { $a.InsecureTls = $true }
        }
        'status' { $a.Status = $true; $a.Name = $node.Name }
    }
    return $a
}
function Format-RunArgs($a) {
    return (($a.Keys | ForEach-Object {
                $v = $a[$_]
                if ($v -is [bool]) { "-$_" }
                elseif ($_ -in @('Token', 'TaskPassword')) { "-$_ ***" }
                else { "-$_ $v" }
            }) -join ' ')
}

# ------------------------------------------------------------------ результаты
function Write-NodeLog($node, [string]$text, [switch]$Raw) {
    if ($DryRun) { return }
    $node.Log = Join-Path $LogDir "$($node.Name).log"
    $h = "===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $($node.Name) $($node.Address) ====="
    Add-Content -Encoding UTF8 -Path $node.Log -Value $(if ($Raw) { Hide-Secrets $text } else { $h + "`r`n" + (Hide-Secrets $text) })
}
function Set-RunResult($node, [string]$act, $res, [string]$err) {
    if (-not $res) { $node.Status = 'ОШИБКА'; $node.Detail = $err; $node.Output = $err; return }
    $node.Code = [int]$res.Code; $node.Output = [string]$res.Output
    $lines = @(($node.Output -split "`r?`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($act -eq 'status') {
        $bad = @($lines | Where-Object { $_ -match '^\[!!\]' })
        if ($node.Code -eq 0) { $node.Status = 'ГОТОВО'; $node.Detail = 'всё в порядке' }
        else { $node.Status = 'ПРОБЛЕМЫ'; $node.Detail = "$($bad.Count): " + (($bad | Select-Object -First 2 | ForEach-Object { $_ -replace '^\[!!\]\s*', '' }) -join '; ') }
        return
    }
    if ($node.Code -eq 0) { $node.Status = 'ГОТОВО'; $node.Detail = $(if ($act -eq 'uninstall') { 'удалено' } else { 'нода установлена' }); return }
    if ($node.Code -eq 3010) { $node.Status = 'ПЕРЕЗАГРУЗКА'; $node.Detail = "перезагрузите ПК, затем повторите: .\class-deploy.ps1 -Action $act -Only $($node.Name)"; return }
    $e = @($lines | Where-Object { $_ -match '^(ОШИБКА|Как исправить)' })
    $node.Status = 'ОШИБКА'
    $node.Detail = $(if ($e.Count) { ($e | Select-Object -First 2) -join ' ' } elseif ($lines.Count) { "код $($node.Code): $($lines[-1])" } else { "код $($node.Code)" })
    if ($node.ImageLoaded -and $node.Output -match 'укажите -ImageTar') {
        $node.Detail += " Образа на ПК нет, хотя он был загружен: повторите .\class-deploy.ps1 -Action $act -Only $($node.Name), архив будет скопирован заново."
    }
    if ($node.Output -match 'GPU не виден в WSL' -and $node.Accel -ne 'cpu') {
        $node.Detail += " GPU не виден в WSL: поставьте accel=cpu для $($node.Name) в $Inventory и повторите: .\class-deploy.ps1 -Action $act -Only $($node.Name)"
    }
}
# Свободное место на C: в таблице итогов: '!! 12.3 ГБ' меньше -MinFreeGB, '! 40.1 ГБ' меньше -WarnFreeGB, пусто — ПК не опрашивался.
function Format-FreeGB($node) {
    $f = Get-Prop $node.Info 'FreeGB'
    if ($null -eq $f) { return '' }
    $mark = $(if ($MinFreeGB -gt 0 -and $f -lt $MinFreeGB) { '!! ' } elseif ($MinFreeGB -gt 0 -and $WarnFreeGB -gt 0 -and $f -lt $WarnFreeGB) { '! ' } else { '' })
    return "$mark$f ГБ"
}
# Свободное место после установки меньше, чем до неё: итоговая таблица должна показывать то, что осталось.
# Размер самой ноды на диске ПК: '!! 21.4 ГБ' больше лимита 16 ГБ, пусто — ноды ещё нет или ПК не опрашивался.
function Format-FootprintGB($node) {
    $f = Get-Prop $node.Info 'FootprintGB'
    if ($null -eq $f -or $f -le 0) { return '' }
    return "$(if ($f -gt $FootprintLimitGB) { '!! ' } else { '' })$f ГБ"
}
function Update-FreeGB($nodes) {
    $live = @($nodes | Where-Object { $_.Session })
    if ($live.Count -eq 0) { return }
    Invoke-OnNodes $live $BlockProbe { param($n) @{ Op = 'probe'; StageDir = $StageDir } } $Throttle {
        param($n, $res, $err)
        if ($res -and $n.Info) {
            $n.Info | Add-Member -NotePropertyName FreeGB -NotePropertyValue $res.FreeGB -Force
            $n.Info | Add-Member -NotePropertyName FootprintGB -NotePropertyValue (Get-Prop $res 'FootprintGB') -Force
        }
    }
}
function Show-Table($nodes) {
    Say ''
    $rows = $nodes | ForEach-Object {
        [pscustomobject]@{ 'Имя' = $_.Name; 'Адрес' = $_.Address; 'Режим' = $_.Mode; 'Ускоритель' = $_.Accel; 'Результат' = $_.Status; 'Диск C:' = (Format-FreeGB $_); 'NSU-LLM' = (Format-FootprintGB $_)
            'Время' = $(if ($_.Seconds) { '{0}:{1:D2}' -f [int][math]::Floor($_.Seconds / 60), [int]($_.Seconds % 60) } else { '' }); 'Подробности' = (Hide-Secrets $_.Detail) }
    }
    $text = ($rows | Format-Table -AutoSize -Wrap | Out-String -Width 200).TrimEnd()
    Say $text
    $groups = $nodes | Group-Object Status | ForEach-Object { "$($_.Name): $($_.Count)" }
    $lowDisk = @($nodes | Where-Object { $f = Get-Prop $_.Info 'FreeGB'; $null -ne $f -and $MinFreeGB -gt 0 -and $WarnFreeGB -gt 0 -and $f -lt $WarnFreeGB } |
            ForEach-Object { "$($_.Name) ($(Get-Prop $_.Info 'FreeGB') ГБ)" })
    $bigFp = @($nodes | Where-Object { $f = Get-Prop $_.Info 'FootprintGB'; $null -ne $f -and $f -gt $FootprintLimitGB } | ForEach-Object { "$($_.Name) ($(Get-Prop $_.Info 'FootprintGB') ГБ)" })
    $lowLine = $(if ($lowDisk.Count) { "Мало места на C: ($($lowDisk.Count) ПК, порог ошибки $MinFreeGB ГБ, рекомендуется $WarnFreeGB ГБ): $($lowDisk -join ', '). С заполненным диском WSL отвечает ошибками ввода-вывода и нода останавливается: освободите место (Параметры, Система, Память, «Временные файлы»)." } else { '' })
    Say ''
    if ($bigFp.Count) { $lowLine = $(if ($lowLine) { "$lowLine`r`n" } else { '' }) + "Нода занимает больше $FootprintLimitGB ГБ диска ($($bigFp.Count) ПК): $($bigFp -join ', '). Лимит для ПК класса: освободите место (удалите ненужные модели кнопкой очистки на странице «Ноды», образы прежних версий, остатки установки)." }
    if ($lowLine) { Warn $lowLine }
    Say ('Итого: ' + ($groups -join ', '))
    if (-not $DryRun -and $LogDir -and (Test-Path -LiteralPath $LogDir)) {
        Say "Журналы по ПК: $LogDir"
        Set-Content -Encoding UTF8 -Path (Join-Path $LogDir 'summary.txt') -Value ($text + $(if ($lowLine) { "`r`n`r`n$lowLine" } else { '' }) + "`r`n`r`nИтого: " + ($groups -join ', '))
    }
}

# ================================================================== основной ход
$script:sessions = @()
function Invoke-Main {
    $act = $Action
    if (-not $act) {
        if ($NonInteractive) { Fail 'Не задано действие (-Action)' 'Допустимо: install, update, uninstall, status, check.' }
        Say 'Что сделать на ПК класса?'
        Say '  1. status    — состояние нод'
        Say '  2. install   — установить'
        Say '  3. update    — обновить скрипты и образ'
        Say '  4. uninstall — удалить ноды'
        Say '  5. check     — только проверить WinRM'
        $c = Read-Answer 'Номер или название' '1'
        $map = @{ '1' = 'status'; '2' = 'install'; '3' = 'update'; '4' = 'uninstall'; '5' = 'check' }
        $act = $(if ($map.ContainsKey($c)) { $map[$c] } else { $c.ToLowerInvariant() })
        if ($act -notin @('install', 'update', 'uninstall', 'status', 'check')) { Fail "Неизвестное действие: $c" 'Допустимо: status, install, update, uninstall, check.' }
    }
    $cwd = (Get-Location).Path
    if (-not $Inventory) { $script:Inventory = Join-Path $cwd 'class-nodes.csv' }
    $script:Inventory = [IO.Path]::GetFullPath([IO.Path]::Combine($cwd, $Inventory))
    $invDir = Split-Path -Parent $Inventory
    if (-not $Answers) { $script:Answers = Join-Path $invDir 'class-answers.json' }
    if (-not $LogDir) { $script:LogDir = Join-Path $invDir ('class-deploy-logs\' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
    if ($Throttle -le 0) { $script:Throttle = 5 }

    # --- инвентарь
    if (-not (Test-Path -LiteralPath $Inventory)) {
        if ($NonInteractive) { Fail "Нет файла инвентаря $Inventory" 'Создайте CSV с заголовком name,address,mode,accel (пример: pc01,192.168.3.101,k3s,auto) или запустите без -NonInteractive: мастер создаст файл.' }
        [void](New-InventoryWizard $Inventory)
        if ($DryRun -and -not (Test-Path -LiteralPath $Inventory)) { Say 'Пробный прогон: инвентарь не записан, дальше нечего показывать.'; return 0 }
    }
    $all = @(Read-Inventory $Inventory)
    $nodes = $all
    if ($Only.Count -gt 0) {
        $want = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
        $nodes = @($all | Where-Object { $want -contains $_.Name -or $want -contains $_.Address })
        $miss = @($want | Where-Object { $w = $_; -not ($all | Where-Object { $_.Name -eq $w -or $_.Address -eq $w }) })
        if ($miss.Count) { Fail "В инвентаре нет: $($miss -join ', ')" "Имена и адреса из $Inventory." }
    }
    Say "Инвентарь $Inventory`: $($nodes.Count) ПК$(if ($nodes.Count -ne $all.Count) { " из $($all.Count)" }), действие $act, параллельно $Throttle"

    # --- общие помощники, которые уходят на ПК текстом
    $script:SecureDirText = [IO.File]::ReadAllText((Get-HelperPath 'secure-dir.ps1'))
    $script:RunnerText = [IO.File]::ReadAllText((Get-HelperPath 'class-run.ps1'))

    # --- ответы и учётка
    $answers = Resolve-Answers $Answers $nodes $act
    $cred = $(if ($Laps -and -not $Credential -and -not $env:NSU_LLM_ADMIN_PASSWORD) { $null } else { Resolve-Credential })
    if ($cred) { Add-Secret ($cred.GetNetworkCredential().Password) }
    if ($Laps) { Resolve-LapsCredentials $nodes }
    elseif (-not $UseSSL -and $act -in @('install', 'update', 'uninstall')) {
        Warn 'WinRM по HTTP: сервер на ПК не проверяется (при TrustedHosts и по IP это так), поэтому ПК, занявший чужой адрес, получит пароль и токен. Они защищены от подслушивания, но не от подмены сервера: -UseSSL (HTTPS с сертификатом) или Kerberos (домен, ПК по имени).'
    }
    # Исходная атака остаётся при общем пароле: он нужен задаче автозапуска (вход без пользователя) и лежит в LSA каждого ПК.
    if (-not $Laps -and $act -in @('install', 'update') -and @($nodes).Count -gt 1) {
        Warn "один и тот же пароль администратора будет сохранён в LSA каждого из $(@($nodes).Count) ПК (задача автозапуска NSU-LLM-Node входит по паролю): администратор одного ПК достанет его оттуда и войдёт на все остальные. Снимает это только свой пароль на каждом ПК: -Laps (Windows LAPS, нужен домен) или отдельная локальная учётная запись с уникальным паролем; gMSA задаче с WSL не подходит (дистрибутив принадлежит конкретному пользователю). docs\deploy-class.md, «Один пароль на весь класс опасен»."
    }

    # --- TrustedHosts: без домена и по IP Kerberos недоступен, WinRM по HTTP пускает только доверенные адреса
    $trustNeeded = (-not $UseSSL) -and (-not (Test-DomainJoined) -or @($nodes | Where-Object { Test-Ipv4 $_.Address }).Count -gt 0)
    $trustValue = ''
    if ($trustNeeded) {
        $cur = Get-TrustedHosts
        $have = @($cur -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        if ($have -notcontains '*') {
            $missing = @($nodes | ForEach-Object { $_.Address } | Where-Object { $have -notcontains $_ })
            if ($missing.Count -gt 0) {
                $trustValue = (($have + $missing) | Select-Object -Unique) -join ','
                Say "TrustedHosts на этом ПК не содержит: $($missing -join ', ')"
                if (-not $DryRun -and -not $NonInteractive -and (Confirm-Yes 'Добавить их сейчас? (нужна консоль администратора) (д/н)')) {
                    try { Set-TrustedHosts $trustValue; Say 'TrustedHosts обновлён.'; $trustNeeded = $false } catch { Warn "не удалось записать TrustedHosts: $($_.Exception.Message)" }
                }
                else { Say "  команда: Set-Item WSMan:\localhost\Client\TrustedHosts -Value '$trustValue' -Force" }
            }
            else { $trustNeeded = $false }
        }
        else { $trustNeeded = $false }
    }

    # --- доступность
    Say 'Проверяю WinRM...'
    Connect-Nodes $nodes $cred
    # ПК администратора в домене, ПК класса по имени, но в рабочей группе: Kerberos не проходит, нужен TrustedHosts.
    if (-not $UseSSL -and (Test-DomainJoined)) {
        $have = @((Get-TrustedHosts) -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $retry = @($nodes | Where-Object { $_.Status -eq 'НЕДОСТУПЕН' -and $_.Detail -notmatch '^порт' -and $have -notcontains $_.Address -and $have -notcontains '*' })
        if ($retry.Count -gt 0) {
            $val = (($have + @($retry | ForEach-Object { $_.Address })) | Select-Object -Unique) -join ','
            Say "Вход на $(($retry | ForEach-Object { $_.Name }) -join ', ') не удался. Если эти ПК в рабочей группе (не в домене этого ПК), Kerberos к ним не работает и на этом ПК нужен TrustedHosts."
            if (-not $DryRun -and -not $NonInteractive -and (Confirm-Yes 'Добавить их в TrustedHosts и повторить вход? (нужна консоль администратора) (д/н)')) {
                try {
                    Set-TrustedHosts $val; Say 'TrustedHosts обновлён, повторяю вход.'
                    foreach ($n in $retry) { $n.Status = ''; $n.Detail = '' }
                    Connect-Nodes $retry $cred
                }
                catch { Warn "не удалось записать TrustedHosts: $($_.Exception.Message)" }
            }
            else { $trustNeeded = $true; $trustValue = $val }
        }
    }
    $script:sessions = @($nodes | Where-Object { $_.Session } | ForEach-Object { $_.Session })
    $failed = @($nodes | Where-Object { $_.Status -eq 'НЕДОСТУПЕН' })
    $live = @($nodes | Where-Object { $_.Session })
    Show-WinRmAdvice $failed ($trustNeeded -and $trustValue) $trustValue
    if ($act -eq 'check') {
        foreach ($n in $live) { $n.Status = 'ГОТОВО'; $n.Detail = 'WinRM работает' }
        Show-Table $nodes
        return $(if ($failed.Count) { 1 } else { 0 })
    }
    if ($live.Count -eq 0) { Show-Table $nodes; Fail 'Ни один ПК не отвечает по WinRM' 'Включите WinRM (команды выше) и повторите; проверка без действий: .\class-deploy.ps1 -Action check' }
    if ($failed.Count -gt 0 -and $act -ne 'status') {
        if ($NonInteractive) { Warn "$($failed.Count) ПК недоступны и будут пропущены" }
        elseif (-not (Confirm-Yes "Продолжить без недоступных ПК ($($failed.Count))? (д/н)")) { Say 'Отменено, ничего не изменено.'; return 1 }
    }

    # --- разведка: права, GPU, место, что уже лежит
    Invoke-OnNodes $live $BlockProbe { param($n) @{ Op = 'probe'; StageDir = $StageDir } } $Throttle {
        param($n, $res, $err)
        if ($res) { $n.Info = $res } else { $n.Status = 'ОШИБКА'; $n.Detail = "разведка не удалась: $err"; $n.Session = $null }
    }
    foreach ($n in @($live | Where-Object { $_.Session })) {
        if ($act -ne 'status' -and -not $n.Info.IsAdmin) {
            $n.Status = 'ПРОПУЩЕН'; $n.Detail = "учётная запись $((Get-NodeCred $n $cred).UserName) не администратор ПК: добавьте её в группу «Администраторы»"; $n.Session = $null; continue
        }
        if ($act -in @('install', 'update')) {
            # Docker CE в WSL с mirrored-сетью требует Windows 11 22H2 (22621); запасной путь docker=desktop (Docker Desktop) работает и на Windows 10
            if ($n.Info.Build -lt 22621 -and $n.Docker -ne 'desktop') { $n.Status = 'ПРОПУЩЕН'; $n.Detail = "Windows сборки $($n.Info.Build): нужен Windows 11 22H2 (22621) или новее; на Windows 10 укажите в списке ПК docker=desktop (нужен установленный Docker Desktop)"; $n.Session = $null; continue }
            if ($n.Info.Build -lt 22621) { Warn "$($n.Name): Windows сборки $($n.Info.Build), нода пойдёт через Docker Desktop (docker=desktop): она держится, только пока в Windows вошёл пользователь (автовход решает администратор)" }
            $n.ImageLoaded = Test-ImageLoaded $answers $n
            $tar = Get-ImageFor $answers $n
            if ($tar) {
                $len = (Get-Item -LiteralPath $tar).Length
                $staged = $(if ((Get-MissingImageBytes $answers $n) -eq 0) { $len } else { 0 })   # уже лежит на ПК и занимает место
                # Архив удаляется сразу после docker load, Docker хранит только распакованные слои (классическое хранилище): пик = архив + слои + дистрибутив, около 8 ГБ сверх архива.
                $need = $(if ($n.ImageLoaded) { 8 } else { [math]::Round(($len - $staged) / 1GB + 8, 1) })
            }
            else { $need = 12 }   # образ скачивается с главного: слои в Docker внутри WSL, дистрибутив и запас (пик установки 11-12 ГБ)
            # Порог -MinFreeGB: диск не должен заполниться из-за ноды (WSL с полным диском даёт ошибки ввода-вывода). Нода уже стоит и обновляется:
            # порог не применяется, хватит места под образ.
            # Как Test-InstallDisk на ПК: нужен новый образ (архив надо загрузить или скачать) — это установка, порог действует; не применяется, только если нода
            # стоит и образ уже загружен.
            if (-not ($act -eq 'update' -and $n.Info.Installed -and $n.ImageLoaded) -and $MinFreeGB -gt $need) { $need = $MinFreeGB }
            if ($n.Info.FreeGB -lt $need) { $n.Status = 'ПРОПУЩЕН'; $n.Detail = "на C: свободно $($n.Info.FreeGB) ГБ, нужно около $need ГБ (образ, его копия в WSL, дистрибутив, модели; порог -MinFreeGB $MinFreeGB). Освободите место на ПК (Параметры, Система, Память, «Временные файлы») и повторите"; $n.Session = $null; continue }
        }
    }
    $todo = @($live | Where-Object { $_.Session })
    if ($todo.Count -eq 0) { Show-Table $nodes; return 1 }

    # --- план и подтверждение
    if ($act -ne 'status') {
        Say ''
        Say "План ($act):"
        foreach ($n in $todo) {
            $ra = Get-RunArgs $act $answers $n (Get-NodeCred $n $cred)
            $upd = $(if ($act -eq 'update' -and -not $n.Info.Installed) { ' (нода ещё не установлена: будет полная установка)' } else { '' })
            if ($act -in @('install', 'update') -and (Test-OldDriver $n)) { $upd += " (драйвер старее ${MinDriver}: cpu, сейчас $($n.Info.Driver))" }
            if ($n.ImageLoaded) { $upd += ' (образ уже загружен, архив не копируется)' }
            Say ("  {0,-8} {1,-16} install-node.ps1 {2}{3}" -f $n.Name, $n.Address, (Format-RunArgs $ra), $upd)
        }
        Say $(if ($RunMode -eq 'task') { "Запуск на ПК: разовая задача планировщика $DeployTaskPath$DeployTask от $(if ($cred) { $cred.UserName } else { 'учётной записи LAPS каждого ПК' }), вывод в $StageDir\run.log." } else { 'Запуск на ПК: внутри сессии WinRM (-RunMode session).' })
        if ($Proxy) { Say "Прокси для установщика: $(Get-MaskedUrl $Proxy)" }
        if ($act -in @('install', 'update') -and $CopyMode -ne 'none') {
            $totalImg = ($todo | ForEach-Object { Get-MissingImageBytes $answers $_ } | Measure-Object -Sum).Sum
            Say "Копирование в $StageDir на каждом ПК (режим $CopyMode)$(if ($totalImg) { ", образы всего $(Format-Size $totalImg) через сеть администратора" })."
        }
        if ($act -eq 'uninstall') { Say 'Ноды будут удалены с ПК и из кластера, дистрибутив WSL удаляется, если его создал установщик.' }
        if ($DryRun) { Say ''; Say 'Пробный прогон (-DryRun): ничего не скопировано и не запущено.'; foreach ($n in $todo) { $n.Status = 'ПЛАН'; $n.Detail = 'dry-run' }; Show-Table $nodes; return 0 }
        if (-not $NonInteractive -and -not $Yes) {
            if (-not (Confirm-Yes "Выполнить на $($todo.Count) ПК? (д/н)")) { Say 'Отменено, ничего не изменено.'; return 1 }
        }
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    }
    elseif (-not $DryRun) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

    # --- раздача файлов
    if ($act -in @('install', 'update') -and $CopyMode -ne 'none') {
        Say ''
        Say 'Раздаю скрипты и образ (по очереди: узкое место — сеть администратора)...'
        foreach ($n in $todo) {
            try {
                $files = Get-LocalFiles $answers $n
                $via = Copy-ToNode $n $files $cred
                Write-NodeLog $n "файлы раздачи ($via): $(($files | ForEach-Object { $_.Name }) -join ', ')"
            }
            catch { $n.Status = 'ОШИБКА'; $n.Detail = ("копирование не удалось: $($_.Exception.Message)" -replace '\r?\n', ' '); Write-NodeLog $n $n.Detail; $n.Session = $null }
        }
        $todo = @($todo | Where-Object { $_.Session })
    }

    # --- запуск
    Say ''
    Say "Запускаю $act на $($todo.Count) ПК, параллельно не более $Throttle..."
    $script:done = 0; $total = $todo.Count
    $t0 = @{}; foreach ($n in $todo) { $t0[$n.Name] = Get-Date }
    $cleanup = $(if ($act -eq 'uninstall') { 'all' } elseif ($act -eq 'status') { 'none' } elseif ($KeepStage) { 'keep-image' } else { 'stage' })
    # В режиме task ПК сам ограничивает время (TimeoutMin); здесь — запас на случай, если сессия WinRM повисла.
    $localLimit = $(if ($RunMode -eq 'task') { $TimeoutMin + 3 } else { $TimeoutMin })
    Invoke-OnNodes $todo $BlockRun {
        param($n)
        $nc = Get-NodeCred $n $cred
        $ra = Get-RunArgs $act $answers $n $nc
        @{ Op = 'run'; Action = $act; StageDir = $StageDir; MinFreeGB = $MinFreeGB; Args = $ra; Cleanup = $cleanup; RunMode = $RunMode; Proxy = $Proxy
            TaskName = $DeployTask; TaskPath = $DeployTaskPath; TaskUser = $nc.UserName; TaskPassword = $nc.Password; TimeoutSec = [int]($TimeoutMin * 60)
            Runner = $script:RunnerText; SecureDir = $script:SecureDirText; ImageMark = $(if ($ra.Contains('ImageTar')) { Get-ImageMark $answers $n } else { '' }) }
    } $Throttle {
        param($n, $res, $err)
        $n.Seconds = [int]((Get-Date) - $t0[$n.Name]).TotalSeconds
        Set-RunResult $n $act $res $err
        if (-not $res -and $RunMode -eq 'task') { $n.Detail = "$($n.Detail) (если связь оборвалась, установка на ПК продолжается в задаче планировщика: повторите ту же команду, она покажет её ход)" }
        $tail = $(if (-not $res -and $n.Session) { Get-RemoteTail $n } else { '' })
        if ($n.Streamed -gt 0) { Write-NodeLog $n "итог: $($n.Status), код $($n.Code): $($n.Detail)" }
        else { Write-NodeLog $n ($(if ($n.Output) { $n.Output } else { $n.Detail })) }
        if ($tail) { Write-NodeLog $n ("последние строки $StageDir\run.log с ПК:`r`n" + $tail) }
        $script:done++
        Say ("  [{0}/{1}] {2,-8} {3}  {4}" -f $script:done, $total, $n.Name, $n.Status, (Hide-Secrets $n.Detail))
    } {
        param($n, $line)
        if ($n.Streamed -eq 0) { Write-NodeLog $n "$act на $($n.Name), запуск: $RunMode" }
        $n.Streamed++
        Write-NodeLog $n $line -Raw
        # На экран — только шаги установщика и ошибки; всё остальное в журнале ПК.
        if ($line -match '^\s*(\[\d+/\d+\]|ОШИБКА)') { Say "  $($n.Name): $(Hide-Secrets $line.Trim())" }
    } $localLimit

    if ($act -in @('install', 'update')) { Update-FreeGB $nodes }
    Show-Table $nodes
    $reboot = @($nodes | Where-Object { $_.Status -eq 'ПЕРЕЗАГРУЗКА' })
    if ($reboot.Count) {
        Say ''
        Say 'Перезагрузить ПК и затем повторить действие (установка идемпотентна, ответы уже сохранены на ПК):'
        Say ('  Restart-Computer -ComputerName ' + (($reboot | ForEach-Object { $_.Address }) -join ',') + ' -Force -Credential $cred')
        Say "  .\class-deploy.ps1 -Action $act -Only $(($reboot | ForEach-Object { $_.Name }) -join ',')"
    }
    if (@($nodes | Where-Object { $_.Status -in @('ОШИБКА', 'НЕДОСТУПЕН', 'ПРОПУЩЕН', 'ПРОБЛЕМЫ') }).Count) { return 1 }
    if ($reboot.Count) { return 2 }
    return 0
}
try {
    $code = Invoke-Main
    exit $code
}
catch {
    Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    $fix = $_.Exception.Data['fix']
    if ($fix) { Write-Host "Как исправить: $fix" }
    if ($env:NSU_LLM_DEBUG) { Write-Host $_.ScriptStackTrace }
    exit 1
}
finally {
    foreach ($s in $script:sessions) { try { Remove-PSSession -Session $s -ErrorAction SilentlyContinue } catch { } }
}
