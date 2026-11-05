# Запускает (или удаляет) контейнер ноды NSU LLM на Windows (Docker Desktop или Docker внутри WSL2).
# Использование:
#   .\run-node.ps1 -Main HOST -Token TOKEN [-Mode k3s|standalone] [-Accel auto|cpu|nvidia]
#                  [-NodeIp IP] [-Name NAME] [-Network bridge|host] [-Image IMAGE] [-DataDir DIR]
#                  [-InsecureTls] [-DryRun] [-Wsl DISTRO] [-AllowFrom ADDR[,ADDR...]]
#                  [-RpcPeers ADDR[,ADDR...]] [-CaSha256 HEX] [-AcceptCa]
#   .\run-node.ps1 -Uninstall -Main HOST -Name NAME [-Token TOKEN] [-Network bridge|host] [-Wsl DISTRO]
#   .\run-node.ps1 -Update [-Wsl DISTRO] [-DryRun]
#   .\run-node.ps1 -SyncFirewall [-Wsl DISTRO] [-DryRun]
# -Update: спросить главного, не пора ли заменить контейнер llm-node (кнопка «Обновить» на странице «Ноды» или NODE_AUTO_UPDATE на
# главном; без запроса администратора «сейчас» только пока нода ничего не обслуживает). Все параметры запуска берутся у работающего
# контейнера; если главный велит обновиться, образ скачивается с главного и контейнер создаётся заново теми же параметрами. Раз в
# минуту его вызывает node-boot.ps1; при Docker Desktop (нет задачи node-boot) запускайте его по расписанию. Сам ничего не делает,
# пока обновлять нечего.
# -Wsl DISTRO: все команды docker выполняются как `wsl -d DISTRO -e docker ...` (DISTRO = default для
# дистрибутива по умолчанию); сеть по умолчанию host, данные лежат внутри WSL.
# -AllowFrom: правило брандмауэра пускает на 18080 (инференс) только с этих адресов или подсетей (IPv4, CIDR); без параметра: только
# с главного (его адрес разрешается из -Main), никогда «с любых». На 18050 (RPC split, без аутентификации) пускают главного
# (не -AllowFrom: он расширяет только 18080) и ноды текущей группы split (-RpcPeers; главный называет их в ответе «desired», поле rpc_peers).
# -SyncFirewall: спросить у главного актуальный rpc_peers и привести правила брандмауэра Windows и Hyper-V firewall к нему (раз в
#                минуту: node-boot.ps1 для WSL, задача NSU-LLM-Node-FwSync для Docker Desktop). Главный, имя и токен берутся из
#                install.json установщика, если не заданы параметрами.
# -CaSha256: SHA-256 (hex, от DER) корня CA главного (https://главный/ca.crt); скачанный корень сверяется с ним. Без него отпечаток
#            печатается, а корень принимается только с -AcceptCa или ответом «да» (доверие при первом обращении).
# -WolMac MAC -WolIp IP -WolPrefix N: физическая сетевая карта хоста (install-node.ps1 её находит), нода сообщает её
# порталу для Wake-on-LAN (WOL_MAC, WOL_IP, WOL_PREFIX).
[CmdletBinding()]
param(
    [string]$Main = '',
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
    [switch]$Update,
    [string]$Wsl = '',
    [string]$AllowFrom = '',
    [string]$DockerUserSid = '',
    [string]$RpcPeers = '',
    [string]$CaSha256 = '',
    [switch]$AcceptCa,
    [switch]$SyncFirewall,
    [string]$WolMac = '',
    [string]$WolIp = '',
    [int]$WolPrefix = 0
)
$ErrorActionPreference = 'Stop'

# Версия образа ноды: единственная константа в скриптах — IMAGE_VERSION рядом (общая с install-node.ps1 и run-node.sh; её проверяет
# release.sh). Это запасное значение: версию называет главный (NODE_IMAGE_TAG, GET /api/node-image и ответ «desired»).
$Version = ''
try { $Version = "$(Get-Content -LiteralPath (Join-Path $PSScriptRoot 'IMAGE_VERSION') -TotalCount 1 -ErrorAction Stop)".Trim() } catch { }
$Container = 'llm-node'
$UseWsl = $PSBoundParameters.ContainsKey('Wsl') -and $Wsl -ne ''
# Защита каталога данных Docker Desktop (общая с install-node.ps1): без неё на C:\ProgramData его мог бы подменить любой пользователь.
$SecureLib = Join-Path $PSScriptRoot 'secure-dir.ps1'
if (Test-Path -LiteralPath $SecureLib) { . $SecureLib }
# -SyncFirewall запускает не человек, а планировщик: главный, имя, режим и токен берутся из ответов install-node.ps1 (install.json:
# DPAPI LocalMachine, доступен только администраторам и SYSTEM). NSU_LLM_INSTALL_JSON — только для тестов.
if ($SyncFirewall) {
    $ij = $(if ($env:NSU_LLM_INSTALL_JSON) { $env:NSU_LLM_INSTALL_JSON } else { Join-Path $env:ProgramData 'NSU-LLM\install.json' })
    $saved = $null
    try { if (Test-Path -LiteralPath $ij) { $saved = ([IO.File]::ReadAllText($ij) | ConvertFrom-Json) } } catch { throw "Не удалось прочитать $ij`: $($_.Exception.Message) (нужен запуск от администратора)" }
    function Get-Saved([string]$n) { if ($saved -and $saved.PSObject.Properties[$n]) { return [string]$saved.$n }; return '' }
    if (-not $Main) { $Main = Get-Saved 'Main' }
    if (-not $PSBoundParameters.ContainsKey('Name') -and (Get-Saved 'Name')) { $Name = Get-Saved 'Name' }
    if (-not $PSBoundParameters.ContainsKey('Mode') -and (Get-Saved 'Mode') -in @('k3s', 'standalone')) { $Mode = Get-Saved 'Mode' }
    if (-not $AllowFrom) { $AllowFrom = Get-Saved 'AllowFrom' }
    if (-not $PSBoundParameters.ContainsKey('Wsl') -and (Get-Saved 'Docker') -eq 'wsl' -and (Get-Saved 'Distro')) { $Wsl = Get-Saved 'Distro'; $UseWsl = $true }
    if (-not $Token) {
        $tm = Get-Saved 'TokenMachine'
        if ($tm) { try { Add-Type -AssemblyName System.Security; $Token = [Text.Encoding]::UTF8.GetString([Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($tm), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)) } catch { } }
    }
    if ((Get-Saved 'InsecureTls') -eq 'True') { $InsecureTls = [switch]$true }
}
if (-not $Network) { $Network = $(if ($UseWsl) { 'host' } else { 'bridge' }) }
if (-not $DataDir) { $DataDir = $(if ($UseWsl) { '/var/lib/nsu-llm-node' } else { $(if ($env:NSU_LLM_DATA_DIR) { $env:NSU_LLM_DATA_DIR } else { Join-Path $env:ProgramData 'nsu-llm-node' }) }) }   # NSU_LLM_DATA_DIR: только для тестов (изолированный каталог данных)
if (-not $Name) { $Name = $env:COMPUTERNAME }
$Name = $Name.ToLowerInvariant()
# То же правило, что у портала (метка DNS-1123): имя, которое он отвергает, не зарегистрировать и не удалить.
if ($Name -cnotmatch '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$') { throw "Недопустимое имя ноды: $Name (латинские буквы, цифры и дефис, до 63 символов)" }
if (-not $Main -and -not $Update) { throw 'Параметр -Main обязателен' }
if ($SyncFirewall -and ($Update -or $Uninstall)) { throw 'Параметр -SyncFirewall не сочетается с -Update и -Uninstall' }
if ($Update -and $Uninstall) { throw 'Параметры -Update и -Uninstall вместе не используются' }
if (-not $Uninstall -and -not $Update -and -not $Token) { throw 'Параметр -Token обязателен' }
# Список адресов попадает в команду брандмауэра: только цифры, точки, косые черты и запятые.
$AllowFrom = $AllowFrom.Replace(' ', '')
if ($AllowFrom -and $AllowFrom -notmatch '^[0-9./]+(,[0-9./]+)*$') { throw "Недопустимый -AllowFrom: $AllowFrom (адреса IPv4 или подсети через запятую, например 192.168.3.0/24,10.0.0.5)" }
if ($DockerUserSid -and $DockerUserSid -notmatch '^S-1-[0-9-]+$') { throw "Недопустимый -DockerUserSid: $DockerUserSid (SID пользователя Windows)" }
$RpcPeers = $RpcPeers.Replace(' ', '')
if ($RpcPeers -and $RpcPeers -notmatch '^[0-9.]+(,[0-9.]+)*$') { throw "Недопустимый -RpcPeers: $RpcPeers (адреса IPv4 через запятую)" }
$CaSha256 = ($CaSha256 -replace '[:\s]', '').ToLowerInvariant()
if ($CaSha256 -and $CaSha256 -notmatch '^[0-9a-f]{64}$') { throw 'Недопустимый -CaSha256: нужны 64 шестнадцатеричные цифры (SHA-256 корня CA главного)' }
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

# Каталог данных ноды (state: токен/секрет ноды, CA; airgap: архив образа, который k3s импортирует при каждом старте) закрывается до
# первой записи. Docker Desktop: он лежит в C:\ProgramData, где Users создают файлы и каталоги в любом наследующем подкаталоге
# (ученик клал свой архив в airgap, и k3s импортировал его под тегом настоящего образа), поэтому Secure-Dir без Users (только SYSTEM и
# Administrators, как у каталога установщика); WSL и Linux: каталог root с правами 0700. Docker Desktop должен работать под учётной
# записью администратора (так он и ставится, docs\deploy-class.md): чужой процесс каталог больше не откроет.
function Protect-DataDir {
    if ($UseWsl) {
        if ($DryRun) { Write-Host "wsl: umask 077; mkdir -p '$DataDir' && chmod 700 '$DataDir'"; return }
        Invoke-Sh "umask 077; mkdir -p '$DataDir' && chmod 700 '$DataDir'"
        if ($LASTEXITCODE -ne 0) { throw "Не удалось создать и закрыть каталог $DataDir в WSL" }
        return
    }
    if (-not (Test-Path Function:\Secure-Dir)) { throw "Нет secure-dir.ps1 рядом с run-node.ps1 ($PSScriptRoot): каталог данных $DataDir нельзя защитить. Скопируйте каталог node целиком." }
    # Docker Desktop читает каталог для bind-mount от имени пользователя, который его запустил. У администратора без повышения группа
    # Administrators в токене «только для запрета», поэтому одной записи на группу мало: этому пользователю выдаётся доступ явно
    # (-DockerUserSid, его называет install-node; без него пользователь, запустивший скрипт). Не проверено на стенде с Docker Desktop.
    $dockerSid = $(if ($DockerUserSid) { $DockerUserSid } else { $id = [Security.Principal.WindowsIdentity]::GetCurrent(); if ($id.IsSystem) { '' } else { $id.User.Value } })
    try { Secure-Dir -Path $DataDir -NoUsers -ExtraSids @($dockerSid) -DryRun:$DryRun }
    catch {
        # Каталог прежней установки (с правами Users) откладывается переименованием, а с открытыми контейнером файлами Windows его
        # не переименует: останавливаем контейнер (он всё равно создаётся заново ниже) и повторяем один раз.
        if ($DryRun -or -not (Test-Docker inspect $Container)) { throw }
        Write-Host "Останавливаю контейнер $Container, чтобы закрыть каталог данных $DataDir."
        Invoke-DockerTry stop $Container
        Secure-Dir -Path $DataDir -NoUsers -ExtraSids @($dockerSid)
    }
}
# k3s импортирует каждый архив каталога airgap при старте: кроме node.tar (его пишет этот скрипт из образа, который уже на ноде) и
# node.id там ничему не место.
function Clear-Airgap {
    if ($DryRun) { Write-Host "удалить из $AirgapDir всё, кроме node.tar и node.id"; return }
    if ($UseWsl) { Invoke-Sh "find '$AirgapDir' -mindepth 1 -maxdepth 1 ! -name node.tar ! -name node.id -exec rm -rf {} +" | Out-Null; return }
    foreach ($f in @(Get-ChildItem -LiteralPath $AirgapDir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @('node.tar', 'node.id') })) {
        Write-Warning "в $AirgapDir лежит посторонний объект $($f.Name): удалён (k3s импортирует каждый архив каталога)"
        Remove-Item -LiteralPath $f.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}
# ------------------------------------------------------------------ обновление (кнопка «Обновить», NODE_AUTO_UPDATE)
# Каждый вызов ноды к главному несёт токен агента (Authorization: Bearer) и, когда контейнер ноды зарегистрировался, ключ, который главный
# выдал именно этому имени (X-Node-Key; файл state/node.key пишет registrar.sh, 0600, 43 символа [A-Za-z0-9_-]). Без файла главный
# принимает только старую ноду с её известного адреса. Всегда две строки заголовков (вторая запасная: скрипту WSL нужно фиксированное число строк).
$script:NodeKey = $null
function Get-NodeKey {
    if ($null -eq $script:NodeKey) {
        $script:NodeKey = ''
        if (-not $DryRun) { $t = "$(Read-Text (Join-Data 'state/node.key'))".Trim(); if ($t -match '^[A-Za-z0-9_-]{43}$') { $script:NodeKey = $t } }
    }
    return $script:NodeKey
}
function Get-AuthHeader {
    $k = Get-NodeKey
    return "Authorization: Bearer $Token`n" + $(if ($k) { "X-Node-Key: $k" } else { 'Accept: */*' })
}
# Сообщение отказа главного ({"detail":{"code":..,"message":".."}}) из тела ответа
function Get-ApiMessage([string]$body) {
    try { $m = ($body | ConvertFrom-Json).detail.message; if ($m) { return "$m" } } catch { }
    return ''
}
# Запрос к API главного: токен уходит файлом заголовков (WSL: через stdin в файл во временном каталоге дистрибутива), а не через
# argv: командную строку процесса видят все. Тело запроса (JSON с кириллицей и кавычками) идёт вторым файлом по той же дороге, в
# аргументах wsl.exe двойных кавычек нет. Возвращает @{ Code; Body }; Code 0 — связи нет.
$CurlExe = $(if ($env:NSU_LLM_CURL) { $env:NSU_LLM_CURL } else { 'curl.exe' })   # NSU_LLM_CURL: только для тестов
function Invoke-MainApi([string]$Method, [string]$Path, [string]$Json = '', [int]$TimeoutSec = 20) {
    $url = "$MainUrl$Path"
    $ca = Join-Data 'state/ca.crt'
    $header = Get-AuthHeader
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $lines = @()
    try {
        if ($UseWsl) {
            $tlsSh = $(if ($InsecureTls) { 'set -- -k' } else { "set --; if [ -s '$ca' ]; then set -- --cacert '$ca'; fi" })
            $dataSh = $(if ($Json) { "-H 'Content-Type: application/json' -d @`$d/b " } else { '' })
            $sh = "$tlsSh; umask 077; set -C; d=/tmp/nsu-llm-api.`$`$; trap 'rm -rf `$d' EXIT; mkdir `$d || exit 1; " +
                "sed -e '1s/^\xef\xbb\xbf//' -e 's/\r`$//' > `$d/in || exit 1; sed -n 1,2p `$d/in > `$d/h; sed -n 3p `$d/in > `$d/b; " +
                "curl -sS --connect-timeout 5 -m $TimeoutSec -X $Method -w '\n%{http_code}' `$@ -H @`$d/h $dataSh'$url'"
            $w = Get-ShCmd $sh
            $stdin = $header + "`n" + $Json
            $prevEnc = $global:OutputEncoding; $global:OutputEncoding = New-Object System.Text.UTF8Encoding $false
            try { $lines = @($stdin | & $w[0] $w[1..($w.Count - 1)] 2>&1 | ForEach-Object { "$_" }) } finally { $global:OutputEncoding = $prevEnc }
        }
        else {
            $hdr = [IO.Path]::GetTempFileName(); $body = [IO.Path]::GetTempFileName()
            try {
                Set-Content -Path $hdr -Value $header -NoNewline -Encoding ascii
                # `\n` goes to curl as two characters (curl turns it into a newline itself): a real line break would cut a command line
                $cargs = @('-sS', '--connect-timeout', '5', '-m', "$TimeoutSec", '-X', $Method, '-w', '\n%{http_code}')
                if ($InsecureTls) { $cargs += '-k' } elseif (Test-Path $ca) { $cargs += @('--cacert', $ca, '--ssl-no-revoke') }
                $cargs += @('-H', "@$hdr")
                if ($Json) {
                    [IO.File]::WriteAllText($body, $Json, (New-Object System.Text.UTF8Encoding $false))
                    $cargs += @('-H', 'Content-Type: application/json', '-d', "@$body")
                }
                $cargs += $url
                $lines = @(& $CurlExe @cargs 2>&1 | ForEach-Object { "$_" })
            }
            finally { Remove-Item -Force -Path $hdr, $body -ErrorAction SilentlyContinue }
        }
    }
    finally { $ErrorActionPreference = $prev }
    $text = ($lines -join "`n").TrimEnd()
    $nl = $text.LastIndexOf("`n")
    $code = $(if ($nl -ge 0) { $text.Substring($nl + 1).Trim() } else { $text.Trim() })
    if ($code -notmatch '^\d{3}$') { return @{ Code = 0; Body = $text } }
    return @{ Code = [int]$code; Body = $(if ($nl -ge 0) { $text.Substring(0, $nl) } else { '' }) }
}
# Неудача обновления уходит главному: страница «Ноды» её показывает, и главный какое-то время не зовёт ноду снова. Текст сообщения
# фиксированный (плюс код ошибки), токена в нём нет.
$script:UpdateStage = ''
function Send-UpdateFailure([string]$text) {
    if ($DryRun) { return }
    try {
        $json = ([ordered]@{ version = ''; revision = ''; digest = ''; tag = ''; update_error = $text } | ConvertTo-Json -Compress)
        [void](Invoke-MainApi 'POST' "/api/nodes/$Name/image" $json)
    }
    catch { }
}
trap {
    if ($script:UpdateStage -eq 'pull') { Send-UpdateFailure ("образ не удалось скачать с главного, контейнер не тронут: " + "$($_.Exception.Message)".Split("`n")[0]) }
    elseif ($script:UpdateStage -eq 'run') { Send-UpdateFailure ("контейнер не удалось создать заново, проверьте docker на ноде: " + "$($_.Exception.Message)".Split("`n")[0]) }
    break
}

# ------------------------------------------------------------------ брандмауэр: 18080 только главный, 18050 главный и ноды группы split
# 18080 (инференс): только адреса из -AllowFrom (по умолчанию главный: шлюз и пробы портала ходят только с него).
# 18050 (ggml-rpc-server split-группы, без аутентификации: любой, кто до него дотянулся, меняет веса и читает KV-кэш группы):
# главный и IP нод текущей группы split (rpc_peers в ответе «desired»; пока главный их не назвал, только главный). Два правила
# на каждый брандмауэр: Windows Firewall (Docker Desktop и всё, что слушает Windows) и Hyper-V firewall (входящие в WSL).
$FwRule = 'NSU-LLM-Node'
$FwRuleRpc = 'NSU-LLM-Node-RPC'
$WslCreator = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'   # VMCreatorId WSL в Hyper-V firewall
function Test-Admin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
# Windows хранит подсеть как 10.1.2.0/255.255.255.0: для сравнения приводим к виду 10.1.2.0/24.
function ConvertTo-Cidr([string]$a) {
    if ($a -match '^(.+)/(\d+\.\d+\.\d+\.\d+)$') {
        $bits = (([Net.IPAddress]$matches[2]).GetAddressBytes() | ForEach-Object { [Convert]::ToString($_, 2) }) -join ''
        return $matches[1] + '/' + ($bits -replace '0', '').Length
    }
    return $a
}
# IPv4 главного: адрес из -Main или первый IPv4 его имени.
function Resolve-MainIpv4([string]$m) {
    $ip = $null
    if ([Net.IPAddress]::TryParse($m, [ref]$ip) -and $ip.AddressFamily -eq 'InterNetwork') { return $ip.IPAddressToString }
    try { return ([Net.Dns]::GetHostAddresses($m) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1).IPAddressToString } catch { return '' }
}
# Адреса, которым открыт 18080: -AllowFrom, без него только главный. «С любых» не бывает.
function Get-InferAllow {
    if ($AllowFrom) { return $AllowFrom }
    $mi = Resolve-MainIpv4 ($Main -replace ':\d+$', '')
    if (-not $mi) { throw "Не удалось определить IPv4 главного ($Main): брандмауэр без адресов не открывается. Укажите -AllowFrom <адрес главного>" }
    return $mi
}
# Адреса, которым открыт 18050: главный и ноды группы. Каждый адрес проверяется как IPv4.
function Get-RpcAllow([string]$peers) {
    $mi = Resolve-MainIpv4 ($Main -replace ':\d+$', '')
    $l = New-Object System.Collections.ArrayList
    if ($mi) { [void]$l.Add($mi) }
    foreach ($p in @($peers -split ',' | Where-Object { $_ })) {
        $ip = $null
        if ($p -match '^\d{1,3}(\.\d{1,3}){3}$' -and [Net.IPAddress]::TryParse($p, [ref]$ip) -and $l -notcontains $ip.IPAddressToString) { [void]$l.Add($ip.IPAddressToString) }
    }
    if ($l.Count -eq 0) { throw "Не удалось определить IPv4 главного ($Main): без него и без rpc_peers 18050 не открывается" }
    return ($l -join ',')
}
# Правило брандмауэра Windows: порты и адреса сверяются при каждом запуске (смена режима, группы split). Замена без окна «открыто всем»:
# новое правило (своё уникальное Name, общее DisplayName) создаётся ДО удаления старого; для Block-правил это важно (иначе между удалением и
# созданием запрет не действует). Любая ошибка оставляет прежнее правило на месте.
function Sync-WinRule([string]$name, [string]$ports, [string]$addrs, [string]$action = 'Allow') {
    $cmd = "New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $ports -RemoteAddress $addrs -Action $action"
    $del = "Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue"
    if ($DryRun) { Write-Host "$del; $cmd"; return }
    $rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    $have = ''
    if ($rule) {
        $have = (@($rule | ForEach-Object { "$($_.Action)" }) | Sort-Object -Unique) -join ',' ; $have += '|' +
        ((@($rule | Get-NetFirewallPortFilter | ForEach-Object { $_.LocalPort }) | Sort-Object -Unique) -join ',') + '|' +
        ((@($rule | Get-NetFirewallAddressFilter | ForEach-Object { $_.RemoteAddress } | ForEach-Object { ConvertTo-Cidr $_ }) | Sort-Object -Unique) -join ',')
    }
    $want = $action + '|' + ((($ports -split ',') | Sort-Object -Unique) -join ',') + '|' + (($addrs -split ',' | Sort-Object -Unique) -join ',')
    if ($have -eq $want) { return }
    if (-not (Test-Admin)) {
        if ($rule) { Write-Host "Обновите правило, выполнив в PowerShell от имени администратора:`n  $del; $cmd" }
        else { Write-Host "Откройте порты, выполнив в PowerShell от имени администратора:`n  $cmd" }
        return
    }
    $newName = "$name-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    New-NetFirewallRule -Name $newName -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort ($ports -split ',') -RemoteAddress ($addrs -split ',') -Action $action -ErrorAction Stop | Out-Null
    # старые правила с тем же DisplayName (кроме только что созданного) убираются после
    foreach ($old in @($rule)) { if ($old -and $old.Name -ne $newName) { Remove-NetFirewallRule -Name $old.Name -ErrorAction SilentlyContinue } }
    Write-Host "Правило брандмауэра $name`: $action TCP $ports $(if ($action -eq 'Block') { 'для всех, кроме главного и нод группы' } else { "с $addrs" })."
}
function Remove-WinRule([string]$name) {
    if ($DryRun) { Write-Host "Remove-NetFirewallRule -DisplayName $name"; return }
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) { return }
    if (Test-Admin) { Remove-NetFirewallRule -DisplayName $name; Write-Host "Правило брандмауэра $name удалено." }
    else { Write-Host "Удалите правило брандмауэра, выполнив в PowerShell от имени администратора:`n  Remove-NetFirewallRule -DisplayName $name" }
}
# Docker Desktop при первом запуске создаёт разрешающее правило для com.docker.backend.exe на любые порты и адреса. Разрешающие правила
# складываются, поэтому узкий Allow для 18080/18050 ничего не ограничивает, пока порт слушает этот процесс. Запрет сильнее разрешения:
# для Docker Desktop (не WSL) ставится Block на тот же порт для ВСЕХ адресов, кроме разрешённых (дополнение до 0.0.0.0-255.255.255.255;
# loopback Windows и так не фильтрует, 127/8 в запрет не входит).
function ConvertTo-IpNum([string]$ip) { $b = [Net.IPAddress]::Parse($ip).GetAddressBytes(); [Array]::Reverse($b); return [uint64][BitConverter]::ToUInt32($b, 0) }
function ConvertFrom-IpNum([uint64]$n) { $b = [BitConverter]::GetBytes([uint32]$n); [Array]::Reverse($b); return ([Net.IPAddress]$b).IPAddressToString }
# Список «адрес или адрес/длина» -> диапазоны всего IPv4 без этих адресов и без 127/8: «a-b,c-d»
function Get-ComplementRanges([string]$list) {
    $iv = New-Object System.Collections.ArrayList
    foreach ($e in @($list -split ',' | Where-Object { $_ })) {
        $p = $e -split '/'; $bits = $(if ($p.Count -gt 1) { [int]$p[1] } else { 32 })
        $lo = ConvertTo-IpNum $p[0]; $size = [uint64][math]::Pow(2, 32 - $bits)
        $lo = $lo - ($lo % $size)
        [void]$iv.Add(@($lo, ($lo + $size - 1)))
    }
    [void]$iv.Add(@([uint64]2130706432, [uint64]2147483647))   # 127.0.0.0/8
    $sorted = @($iv | Sort-Object { $_[0] })
    $out = New-Object System.Collections.ArrayList
    $next = [uint64]0
    foreach ($r in $sorted) {
        if ($r[0] -gt $next) { [void]$out.Add("$(ConvertFrom-IpNum $next)-$(ConvertFrom-IpNum ($r[0] - 1))") }
        if ($r[1] + 1 -gt $next) { $next = $r[1] + 1 }
    }
    if ($next -le 4294967295) { [void]$out.Add("$(ConvertFrom-IpNum $next)-255.255.255.255") }
    return ($out -join ',')
}
# Hyper-V firewall (Windows 11 22H2+): входящие в WSL по умолчанию закрыты, правило Allow открывает только перечисленные адреса.
# На Windows 10 такого брандмауэра нет, там порты закрывает только Windows Firewall. Замена идёт через временное правило, чтобы
# всё время действовало хотя бы одно разрешение, а не «ни одного» (при DefaultInboundAction=Block это закрытый порт, не открытый).
function Sync-HyperVRule([string]$name, [string]$ports, [string]$addrs) {
    if (-not (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) { return }
    $cmd = "New-NetFirewallHyperVRule -Name $name -DisplayName 'NSU LLM Node' -Direction Inbound -VMCreatorId '$WslCreator' -Protocol TCP -LocalPorts $ports -RemoteAddresses $addrs -Action Allow"
    if ($DryRun) { Write-Host "Remove-NetFirewallHyperVRule -Name $name; $cmd"; return }
    $hv = $null; try { $hv = Get-NetFirewallHyperVRule -Name $name -ErrorAction Stop } catch { }
    if ($hv) {
        $have = ((@($hv.LocalPorts) | Sort-Object -Unique) -join ',') + '|' + ((@($hv.RemoteAddresses | ForEach-Object { ConvertTo-Cidr $_ }) | Sort-Object -Unique) -join ',')
        $want = ((($ports -split ',') | Sort-Object -Unique) -join ',') + '|' + (($addrs -split ',' | Sort-Object -Unique) -join ',')
        if ($have -eq $want) { return }
    }
    if (-not (Test-Admin)) { Write-Host "Обновите правило Hyper-V firewall, выполнив в PowerShell от имени администратора:`n  $cmd"; return }
    $tmp = "$name-upd"
    try { Remove-NetFirewallHyperVRule -Name $tmp -ErrorAction Stop } catch { }
    New-NetFirewallHyperVRule -Name $tmp -DisplayName 'NSU LLM Node' -Direction Inbound -VMCreatorId $WslCreator -Protocol TCP `
        -LocalPorts ($ports -split ',') -RemoteAddresses ($addrs -split ',') -Action Allow -ErrorAction Stop | Out-Null
    try { Remove-NetFirewallHyperVRule -Name $name -ErrorAction Stop } catch { }
    New-NetFirewallHyperVRule -Name $name -DisplayName 'NSU LLM Node' -Direction Inbound -VMCreatorId $WslCreator -Protocol TCP `
        -LocalPorts ($ports -split ',') -RemoteAddresses ($addrs -split ',') -Action Allow -ErrorAction Stop | Out-Null
    try { Remove-NetFirewallHyperVRule -Name $tmp -ErrorAction Stop } catch { }
    Write-Host "Правило Hyper-V firewall $name`: TCP $ports с $addrs."
}
function Remove-HyperVRule([string]$name) {
    if (-not (Get-Command Remove-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) { return }
    if ($DryRun) { Write-Host "Remove-NetFirewallHyperVRule -Name $name"; return }
    foreach ($n in @($name, "$name-upd")) { try { Remove-NetFirewallHyperVRule -Name $n -ErrorAction Stop } catch { } }
}
# DefaultInboundAction=Allow для WSL (его часто советуют для mirrored) делает все правила Hyper-V бесполезными: порты открыты всем. Block
# ставится и проверяется; без прав администратора печатается предупреждение с командой.
function Sync-HyperVDefault {
    if (-not $UseWsl -or -not (Get-Command Set-NetFirewallHyperVVMSetting -ErrorAction SilentlyContinue)) { return }
    $fix = "Set-NetFirewallHyperVVMSetting -Name '$WslCreator' -DefaultInboundAction Block"
    if ($DryRun) { Write-Host "если сейчас DefaultInboundAction=Allow для WSL: $fix"; return }
    $v = $null; try { $v = Get-NetFirewallHyperVVMSetting -Name $WslCreator -ErrorAction Stop } catch { }
    if (-not $v -or "$($v.DefaultInboundAction)" -ne 'Allow') { return }
    if (Test-Admin) {
        try { Set-NetFirewallHyperVVMSetting -Name $WslCreator -DefaultInboundAction Block -ErrorAction Stop; Write-Host 'Hyper-V firewall для WSL: входящие по умолчанию были разрешены (правила были бы бесполезны), теперь запрещены; открыты только порты ноды.' }
        catch { Write-Warning "Hyper-V firewall для WSL разрешает входящие по умолчанию, и все правила бесполезны; не удалось запретить: $($_.Exception.Message). Выполните: $fix" }
    }
    else { Write-Warning "Hyper-V firewall для WSL разрешает входящие по умолчанию, и все правила бесполезны. Выполните от администратора: $fix" }
}
# WSL в mirrored-режиме делит loopback с Windows: любой процесс любого пользователя Windows подключается к 127.0.0.1:18050 и к адресу самого
# ПК, и ни Windows Firewall, ни Hyper-V firewall (они не фильтруют loopback) этого не остановят. Проверено на WSL 2.6 (mirrored): такой трафик
# приходит в дистрибутив через интерфейс loopback0 (не lo), а лидер группы ходит к своему rpc-серверу по lo. Поэтому в дистрибутиве
# 18050 закрывается для loopback0 (INPUT и FORWARD: hostPort-под k3s получает трафик через DNAT). Правила iptables не переживают
# перезапуск WSL: их возвращает тот же раз в минуту -SyncFirewall (node-boot.ps1).
function Sync-WslLoopback {
    if (-not $UseWsl) { return }
    $rule = '-i loopback0 -p tcp -m conntrack --ctorigdstport 18050 --ctdir ORIGINAL -j DROP'
    if ($Mode -eq 'k3s') {
        # цепочка && : первая же ошибка (нет iptables) даёт ненулевой код, и предупреждение ниже срабатывает
        $cmds = (@('INPUT', 'FORWARD') | ForEach-Object { "{ iptables -w -C $_ $rule 2>/dev/null || iptables -w -I $_ 1 $rule; }" }) -join ' && '
    }
    else {
        # без RPC правило не нужно: убираем, если осталось от режима k3s
        $cmds = ((@('INPUT', 'FORWARD') | ForEach-Object { "iptables -w -D $_ $rule 2>/dev/null" }) -join '; ') + '; true'
    }
    if ($DryRun) { Write-Host "wsl: $cmds"; return }
    $out = (Invoke-Sh $cmds 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) { Write-Warning "не удалось закрыть 18050 для loopback0 в дистрибутиве (iptables не найден или не принял правило): процессы Windows этого ПК могут подключиться к RPC. $out" }
}
function Sync-BlockRule([string]$name, [string]$ports, [string]$allowList) {
    $ranges = Get-ComplementRanges $allowList
    if ($ranges) { Sync-WinRule $name $ports $ranges 'Block' } else { Remove-WinRule $name }   # разрешён весь IPv4: запрещать некого
}
# Все правила ноды разом: инференс (18080) всегда; RPC (18050) только в режиме k3s, иначе правило RPC убирается.
function Set-NodeFirewall([string]$peers) {
    $infer = Get-InferAllow
    Sync-WinRule $FwRule '18080' $infer
    Sync-HyperVRule $FwRule '18080' $infer
    $rpc = ''
    if ($Mode -eq 'k3s') {
        $rpc = Get-RpcAllow $peers
        Sync-WinRule $FwRuleRpc '18050' $rpc
        Sync-HyperVRule $FwRuleRpc '18050' $rpc
    }
    else { Remove-WinRule $FwRuleRpc; Remove-HyperVRule $FwRuleRpc }
    if (-not $UseWsl) {
        # Docker Desktop: запрет для всех остальных (см. выше); список пересобирается вместе с разрешением
        Sync-BlockRule "$FwRule-Block" '18080' $infer
        if ($Mode -eq 'k3s') { Sync-BlockRule "$FwRuleRpc-Block" '18050' $rpc } else { Remove-WinRule "$FwRuleRpc-Block" }
    }
    Sync-HyperVDefault
    Sync-WslLoopback
}
# rpc_peers из ответа «desired»: список IPv4 нод группы split; $null, если главный поля не прислал (старый главный): правила не трогаются.
function Get-RpcPeersFromPlan($plan) {
    if (-not $plan -or -not $plan.PSObject.Properties['rpc_peers']) { return $null }
    $ok = New-Object System.Collections.ArrayList
    foreach ($p in @($plan.rpc_peers)) {
        $ip = $null
        if ("$p" -match '^\d{1,3}(\.\d{1,3}){3}$' -and [Net.IPAddress]::TryParse("$p", [ref]$ip)) { [void]$ok.Add($ip.IPAddressToString) }
    }
    return ($ok -join ',')
}

if ($SyncFirewall) {
    # Раз в минуту (node-boot.ps1, задача NSU-LLM-Node-FwSync): главный называет ноды текущей группы split, правила приводятся к ним.
    # Главный не отвечает или не знает поля rpc_peers: правила остаются как есть (при первом запуске их создаёт установщик).
    $r = Invoke-MainApi 'GET' "/api/nodes/$Name/desired?accel=cpu" '' 15
    if ($r.Code -ne 200) { Write-Host "Главный не ответил (ответ: $($r.Code)): правила брандмауэра не менялись."; return }
    $plan = $null
    try { $plan = $r.Body | ConvertFrom-Json } catch { }
    $peers = Get-RpcPeersFromPlan $plan
    if ($null -eq $peers) { Write-Host 'Главный не назвал rpc_peers (старая версия): правила брандмауэра не менялись.'; return }
    Set-NodeFirewall $peers
    return
}

if ($Update) {
    # Параметры запуска у работающего контейнера (docker inspect); явно заданные параметры сильнее.
    $raw = ((Get-DockerOut inspect $Container) -join "`n").Trim()
    $info = $null
    if ($raw) { try { $info = @($raw | ConvertFrom-Json)[0] } catch { $info = $null } }
    if (-not $info) { Write-Host "Контейнера $Container нет: обновлять нечего."; return }
    $ce = @{}
    foreach ($e in @($info.Config.Env)) { $s = "$e"; $i = $s.IndexOf('='); if ($i -gt 0) { $ce[$s.Substring(0, $i)] = $s.Substring($i + 1) } }
    if (-not $Main) { $Main = $ce['MAIN_HOST'] }
    if (-not $Token) { $Token = $ce['CLUSTER_TOKEN'] }
    if (-not $PSBoundParameters.ContainsKey('Name') -and $ce['NODE_NAME']) { $Name = $ce['NODE_NAME'] }
    if (-not $NodeIp) { $NodeIp = $ce['NODE_IP'] }
    if (-not $PSBoundParameters.ContainsKey('Mode') -and $ce['MODE'] -in @('k3s', 'standalone')) { $Mode = $ce['MODE'] }
    if (-not $PSBoundParameters.ContainsKey('Accel') -or $Accel -eq 'auto') { if ($ce['ACCEL'] -in @('cpu', 'nvidia')) { $Accel = $ce['ACCEL'] } }
    if ($ce['MAIN_INSECURE_TLS'] -eq '1') { $InsecureTls = [switch]$true }
    if (-not $PSBoundParameters.ContainsKey('Network')) { $Network = $(if ("$($info.HostConfig.NetworkMode)" -eq 'host') { 'host' } else { 'bridge' }) }
    if (-not $WolMac -and $ce['WOL_MAC']) { $WolMac = $ce['WOL_MAC']; $WolIp = $ce['WOL_IP']; $WolPrefix = [int]$(if ($ce['WOL_PREFIX'] -match '^\d+$') { $ce['WOL_PREFIX'] } else { 0 }) }
    # внутри WSL каталог данных виден по монтированию каталога состояния; у Docker Desktop путь монтирования — путь виртуальной машины,
    # поэтому там каталог данных стандартный (или заданный -DataDir)
    if ($UseWsl -and -not $PSBoundParameters.ContainsKey('DataDir')) {
        $mount = @($info.Mounts) | Where-Object { $_.Destination -eq '/var/lib/llm-node' } | Select-Object -First 1
        if ($mount -and "$($mount.Source)" -match '^(/[A-Za-z0-9._/-]+)/state$') { $DataDir = $matches[1] }
    }
    foreach ($v in @('HF_ENDPOINT', 'HF_TOKEN', 'THREADS', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy')) {
        if ($ce[$v] -and -not [Environment]::GetEnvironmentVariable($v)) { [Environment]::SetEnvironmentVariable($v, $ce[$v]) }
    }
    if ($Main -cnotmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$') { throw "Недопустимый адрес главного у контейнера $Container : '$Main'" }
    if (-not $Token) { throw "У контейнера $Container нет CLUSTER_TOKEN: он запущен не run-node?" }
    $Name = $Name.ToLowerInvariant()
    if ($Name -cnotmatch '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$') { throw "Недопустимое имя ноды у контейнера: $Name" }
    if ($NodeIp -and $NodeIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "Недопустимый NODE_IP у контейнера: $NodeIp" }
    if ($WolMac -and ($WolMac -notmatch '^[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}$' -or $WolIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$' -or $WolPrefix -lt 1 -or $WolPrefix -gt 32)) {
        throw "Недопустимая сетевая карта для Wake-on-LAN у контейнера: $WolMac $WolIp/$WolPrefix"
    }
    $MainUrl = "https://$Main"
    $CurAccel = $(if ($Accel -eq 'nvidia') { 'nvidia' } else { 'cpu' })

    $r = Invoke-MainApi 'GET' "/api/nodes/$Name/desired?accel=$CurAccel"
    if ($r.Code -ne 200) { Write-Host "Главный не ответил на вопрос об обновлении (ответ: $($r.Code)): ничего не меняю."; return }
    $plan = $null
    try { $plan = $r.Body | ConvertFrom-Json } catch { }
    if (-not $plan -or -not $plan.update -or -not $plan.update.go -or -not $plan.image) {
        $why = $(if ($plan -and $plan.update -and $plan.update.reason) { " ($($plan.update.reason))" } else { '' })
        Write-Host "Обновление не требуется$why."
        return
    }
    $newImage = "$($plan.image.name)"; $newDigest = "$($plan.image.digest)"
    if ($newImage -cnotmatch '^nsu-llm-node:[A-Za-z0-9._-]+$') { throw "Главный назвал недопустимый образ: $newImage" }
    if ($newDigest -cnotmatch '^sha256:[0-9a-f]{64}$') { throw "Главный назвал недопустимый digest: $newDigest" }
    if ($ce['NODE_IMAGE_DIGEST'] -eq $newDigest) { Write-Host "Контейнер уже работает на образе ${newDigest}: главный узнает об этом при его запуске."; return }
    Write-Host "Обновление ноды $Name до $newImage ($newDigest), запрос: $($plan.update.mode)$(if ($plan.update.when) { ', ' + $plan.update.when })."
    $Image = $newImage
    $script:UpdateStage = 'pull'
    # каталог данных закрывается до первой записи (digest образа пишется в него сразу после загрузки)
    Protect-DataDir
    $recordPath = Join-Data 'state/image.digest'
    if ($UseWsl) {
        # pull-image.sh в дистрибутиве: корень главного закреплён для Docker один раз, digest записывается в state/image.digest
        $full = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'pull-image.sh'))
        if ($full -notmatch '^([A-Za-z]):\\(.*)$') { throw "Путь $full недоступен из WSL" }
        $sh = '/mnt/' + $matches[1].ToLowerInvariant() + '/' + $matches[2].Replace('\', '/')
        # --digest: образ берётся по digest, который назвал главный (не по тегу, который можно перезаписать в реестре), и сверяется с ним
        $pull = (Get-WslPrefix) + @('-e', 'bash', $sh, '--main', $Main, '--image', $Image, '--digest', $newDigest, '--no-tofu', '--record', $recordPath)
        $ca = Join-Data 'state/ca.crt'
        if (-not $InsecureTls -and (Read-Text $ca)) { $pull += @('--ca', $ca) }
        elseif (-not $DryRun) {
            # Корня нет ни у ноды, ни у Docker: pull-image.sh при сертификате, который системные корни не приняли, сам взял бы /ca.crt с -k и
            # закрепил (подмена CA в сети = чужой образ с root). Поэтому системные корни проверяются заранее; не приняли: отказ.
            Invoke-Sh ("test -s '/etc/docker/certs.d/$Main/ca.crt' && exit 0; c=`$(curl -sS -m 15 -o /dev/null -w '%{http_code}' https://$Main/v2/ 2>/dev/null); case `$c in 000|'') exit 1;; esac; exit 0") | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Реестр https://$Main/v2/ не проверяется: корень главного не закреплён, а системные корни его сертификат не принимают. Обновление остановлено (pull-image.sh принял бы чужой корень без проверки); переустановите ноду через install-node.ps1 с токеном K10 или -CaSha256." }
        }
        if ($DryRun) { Write-Host (($pull | ForEach-Object { Format-Arg $_ }) -join ' ') }
        else {
            & $pull[0] $pull[1..($pull.Count - 1)]
            if ($LASTEXITCODE -ne 0) { throw "pull-image.sh завершился с кодом $LASTEXITCODE" }
        }
    }
    else {
        # Docker Desktop: docker.exe качает, только если сертификат главного принимает Windows (см. install-node.ps1, Invoke-ImagePull)
        # по digest, который назвал главный, а не по тегу: перезаписанный в реестре тег не подменит образ
        $remote = "$Main/nsu-llm-node@$newDigest"
        Invoke-Docker pull $remote
        if (-not $DryRun) {
            $digests = ((Get-DockerOut image inspect '--format={{.RepoDigests}}' $remote) -join ' ')
            $pulled = $(if ($digests -match ([regex]::Escape("$Main/nsu-llm-node@") + '(sha256:[0-9a-f]{64})')) { $matches[1] } else { '' })
            if ($pulled -ne $newDigest) { Invoke-DockerTry rmi $remote; throw "Скачанный образ имеет digest '$pulled', а главный назвал ${newDigest}: образ отклонён, контейнер не тронут" }
        }
        Invoke-Docker tag $remote $Image
        Invoke-DockerTry rmi $remote
        if (-not $DryRun) {
            $pulledId = ((Get-DockerOut image inspect -f '{{.Id}}' $Image) -join '').Trim()
            Ensure-Dir (Join-Data 'state')
            if ($pulled -and $pulledId) { Write-Text $recordPath "$pulledId $pulled" }
            elseif (Test-Path $recordPath) { Remove-Item -Force $recordPath }
        }
    }
    $script:UpdateStage = 'run'
    $Accel = $CurAccel
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
        $header = Get-AuthHeader
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
                "sed -e '1s/^\xef\xbb\xbf//' -e 's/\r`$//' > `$h || exit 1; curl -X DELETE -sS -w '\n%{http_code}' -m 15 `$@ -H @`$h '$url'")
        }
        else {
            $hdr = $(if ($DryRun) { '<файл заголовков>' } else { [IO.Path]::GetTempFileName() })
            $del = @('curl.exe', '-X', 'DELETE', '-sS', '-w', '\n%{http_code}', '-m', '15')
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
                    try { $out = ($header | & $del[0] $del[1..($del.Count - 1)] 2>&1) -join "`n" } finally { $OutputEncoding = $prevEnc }
                }
                else {
                    # Временный каталог пользователя: файл читает только он, удаляется сразу после запроса.
                    Set-Content -Path $hdr -Value $header -NoNewline -Encoding ascii
                    $out = (& $del[0] $del[1..($del.Count - 1)] 2>&1) -join "`n"
                }
            }
            finally {
                $ErrorActionPreference = $prev
                if ($hdr) { Remove-Item -Force -Path $hdr -ErrorAction SilentlyContinue }
            }
            # Тело ответа (отказ главного несёт сообщение) и код: последняя строка вывода
            $outT = "$out".Trim(); $nl = $outT.LastIndexOf("`n")
            $code = $(if ($nl -ge 0) { $outT.Substring($nl + 1).Trim() } else { $outT })
            $apiMsg = $(if ($nl -ge 0) { Get-ApiMessage $outT.Substring(0, $nl) } else { '' })
            # 204 — удалена, 404 — её уже нет: оба исхода годятся; иначе (нет связи, 401, 403, 5xx) объект ноды остаётся.
            if ($LASTEXITCODE -eq 0 -and $code -in @('403', '409')) {
                Write-Warning "DELETE $MainUrl/api/nodes/$Name отклонён главным (ответ: $code)$(if ($apiMsg) { ': ' + $apiMsg }). Удалите ноду на странице «Ноды» портала (меню строки, «Удалить из кластера…») или на главном сервере (docker exec llm-main k3s kubectl delete node $Name)."
            }
            elseif ($LASTEXITCODE -eq 0 -and $code -eq '401') {
                Write-Warning "DELETE $MainUrl/api/nodes/$Name не выполнен (curl 0, ответ: 401). Главный не принял токен: он мог устареть после переустановки главного или смены токена агента. Повторите с актуальным токеном (-Token, страница «Подключение ноды») или удалите ноду на странице «Ноды» портала (меню строки, «Удалить из кластера…») или на главном сервере (docker exec llm-main k3s kubectl delete node $Name)."
            }
            elseif ($LASTEXITCODE -ne 0 -or ($code -notin @('204', '404'))) {
                Write-Warning "DELETE $MainUrl/api/nodes/$Name не выполнен (curl $LASTEXITCODE, ответ: $outT). Нода останется в кластере: удалите её на странице «Ноды» портала (меню строки, «Удалить из кластера…») или на главном сервере (docker exec llm-main k3s kubectl delete node $Name)."
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
    foreach ($rn in @($FwRule, $FwRuleRpc)) { Remove-WinRule $rn; Remove-HyperVRule $rn; Remove-WinRule "$rn-Block" }
    if (-not $DryRun) { Write-Host "Нода $Name удалена." }
    return
}

# ------------------------------------------------------------------ установка
if (-not $DryRun -and -not (Test-Docker info)) { throw 'docker недоступен (docker info завершился ошибкой)' }

# CA главного один раз: по нему -Uninstall проверяет сервер при DELETE, а первый вопрос главному (тег образа) уже отправляет токен и
# должен проверять сервер. Корень не принимается на веру: он сверяется с -CaSha256 (SHA-256 от DER корня, его показывает страница
# «Подключение ноды»); без отпечатка печатается его значение, а принять корень можно только с -AcceptCa или ответом «да» человека:
# первый, кто ответил в сети (ARP, DNS), иначе получил бы токен и закрепился бы корнем доверия ноды.
function Get-PemSha256([string]$pem) {
    if ($pem -notmatch '(?s)-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----') { return '' }
    $der = [Convert]::FromBase64String(($matches[1] -replace '\s', ''))
    $h = [Security.Cryptography.SHA256]::Create().ComputeHash($der)
    return (($h | ForEach-Object { $_.ToString('x2') }) -join '')
}
# $true: корень можно закрепить. Ошибка: отпечаток не совпал (подмена) или некому подтвердить.
function Confirm-CaFingerprint([string]$fp) {
    if ($CaSha256) {
        if ($fp -eq $CaSha256) { Write-Host "Корень CA главного совпал с -CaSha256 ($fp)."; return $true }
        throw "Корень CA, который отдал $MainUrl/ca.crt, не совпал с -CaSha256: получен $fp, ожидался $CaSha256. Возможна подмена в сети (ARP, DNS) или CA главного сменили: корень не сохранён, токен не отправлялся. Сверьте отпечаток со страницей «Подключение ноды»."
    }
    Write-Host "Отпечаток корня CA главного (SHA-256): $fp"
    if ($AcceptCa) { Write-Warning 'отпечаток не задан (-CaSha256), корень принят по -AcceptCa без сверки: доверие при первом обращении. Сверьте отпечаток со страницей «Подключение ноды».'; return $true }
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        return ((Read-Host 'Сверьте отпечаток со страницей «Подключение ноды» портала. Доверять этому корню? (д/н)') -match '^(д|да|y|yes)$')
    }
    throw "Корень CA главного ($fp) не подтверждён: нет -CaSha256 и некому ответить на вопрос. Передайте отпечаток со страницы «Подключение ноды» (-CaSha256) или, если сеть доверенная, -AcceptCa."
}
# Корень, проверенный по токену K10 (Test-MainCaByToken из secure-dir.ps1), кладётся в state/ca.crt (WSL: внутрь дистрибутива, root 0600)
function Save-CaPem([string]$pem) {
    $ca = Join-Data 'state/ca.crt'
    if ($UseWsl) {
        $save = Get-ShCmd "umask 077; cat > '$ca.tmp' && mv '$ca.tmp' '$ca'"
        $pem | & $save[0] $save[1..($save.Count - 1)]
        if ($LASTEXITCODE -ne 0) { throw "Не удалось сохранить корень CA в $ca" }
    }
    else { [IO.File]::WriteAllText("$ca.tmp", $pem); Move-Item -Force "$ca.tmp" $ca }
}
function Ensure-Ca {
    if ($DryRun -or $InsecureTls) { return }
    Ensure-Dir (Join-Data 'state')
    $ca = Join-Data 'state/ca.crt'
    $haveCa = $(if ($UseWsl) { "$((Invoke-Sh "test -s '$ca' && echo have") -join '')" -eq 'have' } else { Test-Path $ca })
    if ($haveCa) { return }
    # Токен K10<sha256 CA k3s>::... проверяет корень главного без доверия первому ответившему; явный -CaSha256 имеет приоритет
    if (-not $CaSha256 -and (Test-Path Function:\Test-MainCaByToken)) {
        $t = Test-MainCaByToken $Main $Token $CurlExe
        if ($t.Status -eq 'verified') { Write-Host "Корень CA главного проверен по токену (CA k3s), отпечаток SHA-256 $($t.Fp)."; Save-CaPem $t.Pem; return }
        if ($t.Status -eq 'mismatch') { throw "$($t.Why). Корень не сохранён, токен не отправлялся." }
        if ($t.Status -in @('public', 'unreachable')) { return }
        Write-Host "Проверить корень главного по токену нечем: $($t.Why)."
    }
    if ($UseWsl) {
        # Внутри WSL: там же его читает DELETE при -Uninstall. Нет curl или openssl в дистрибутиве — CA не сохранится (системное хранилище).
        $fp = ((Invoke-Sh "test -s '$ca' && echo have || { rm -f '$ca.tmp'; curl -sSf -k --connect-timeout 5 -m 15 -o '$ca.tmp' '$MainUrl/ca.crt' 2>/dev/null && openssl x509 -in '$ca.tmp' -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1; }") -join '').Trim()
        if ($fp -eq 'have') { return }
        if ($fp -notmatch '^[0-9a-f]{64}$') { Invoke-Sh "rm -f '$ca.tmp'" | Out-Null; return }
        $ok = $false
        try { $ok = Confirm-CaFingerprint $fp } finally { if (-not $ok) { Invoke-Sh "rm -f '$ca.tmp'" | Out-Null } }
        if ($ok) { Invoke-Sh "mv '$ca.tmp' '$ca'" | Out-Null } else { Write-Host 'Корень CA не принят: нода будет проверять главного по системному хранилищу.' }
    }
    elseif (-not (Test-Path $ca)) {
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & curl.exe -sSf -k --connect-timeout 5 -m 15 -o "$ca.tmp" "$MainUrl/ca.crt" 2>$null } finally { $ErrorActionPreference = $prev }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$ca.tmp")) { Remove-Item -Force "$ca.tmp" -ErrorAction SilentlyContinue; return }
        $ok = $false
        try { $ok = Confirm-CaFingerprint (Get-PemSha256 ([IO.File]::ReadAllText("$ca.tmp"))) } finally { if (-not $ok) { Remove-Item -Force "$ca.tmp" -ErrorAction SilentlyContinue } }
        if ($ok) { Move-Item -Force "$ca.tmp" $ca } else { Write-Host 'Корень CA не принят: нода будет проверять главного по системному хранилищу.' }
    }
}
# Тег образа ноды — NODE_IMAGE_TAG главного, а не константа этого скрипта (портал называет образ в ответе на «desired»): раньше скрипт
# искал nsu-llm-node:0.1.0-gpu, когда главный работал на 0.1.1, и установка падала на проверке GPU. Нет ответа (-DryRun, старый главный,
# нет сети): версия этого скрипта.
# Запасное имя (версия из IMAGE_VERSION) нужно только здесь: -Uninstall и -Update файл не читают.
function Get-FallbackImage([string]$flavour) {
    if ($Version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$') { throw 'Главный не назвал образ ноды, а рядом со скриптом нет файла IMAGE_VERSION с версией вида 1.2.3: скопируйте каталог node целиком или укажите -Image' }
    return "nsu-llm-node:$Version-$flavour"
}
function Get-MainImageName([string]$accel) {
    $flavour = $(if ($accel -eq 'nvidia') { 'gpu' } else { 'cpu' })
    if ($DryRun -or -not $Token) { return (Get-FallbackImage $flavour) }
    try {
        Ensure-Ca
        $r = Invoke-MainApi 'GET' "/api/nodes/$Name/desired?accel=$accel" '' 8
        if ($r.Code -eq 200) {
            $n = "$((($r.Body | ConvertFrom-Json)).name)"
            if ($n -cmatch '^nsu-llm-node:[A-Za-z0-9._-]+$') { return $n }
        }
    }
    catch { }
    return (Get-FallbackImage $flavour)
}

# GPU проверяется локальным образом ноды (без обращения к реестру): nvidia-smi в контейнер монтирует nvidia-container-toolkit.
$GpuImage = $(if ($Image) { $Image } else { Get-MainImageName 'nvidia' })
function Test-Gpu { return (Test-Docker run --rm --pull never --gpus all --entrypoint nvidia-smi $GpuImage) }
if ($Accel -eq 'auto') {
    if ($DryRun) { $Accel = 'cpu' }
    elseif (-not (Test-Docker image inspect $GpuImage)) { Write-Host "Образ $GpuImage не загружен: GPU не проверяется, нода запускается как cpu."; $Accel = 'cpu' }
    elseif (Test-Gpu) { $Accel = 'nvidia' } else { $Accel = 'cpu' }
}
elseif ($Accel -eq 'nvidia' -and -not $DryRun -and -not (Test-Gpu)) {
    throw "GPU недоступен: docker run --rm --pull never --gpus all --entrypoint nvidia-smi $GpuImage завершился ошибкой (образ должен быть загружен)"
}
if (-not $Image) {
    $Image = $(if ($Accel -eq 'nvidia') { $GpuImage } else { Get-MainImageName 'cpu' })
}

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

if (-not $DryRun -and -not (Test-Docker image inspect $Image)) { throw "Образ $Image не найден (docker load или docker build)" }
Protect-DataDir
if (-not $DryRun) { Ensure-Dir $StateDir; Ensure-Dir $AirgapDir }
Clear-Airgap
if (-not $DryRun) {
    Ensure-Ca
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
    # 18050 только на адрес ноды: 127.0.0.1:18050 Docker Desktop не открывает, до RPC не достучится локальный пользователь этого ПК
    if ($Mode -eq 'k3s') { $run += @('-p', "${NodeIp}:18050:18050") }
}
if ($Accel -eq 'nvidia') {
    $run += @('--gpus', 'all')
    # Docker Desktop gives the container libcuda and /dev/dxg but not /usr/lib/wsl/lib (libdxcore, the WSL NVML): without
    # them the k3s pods inside (nvidia-device-plugin, llama) get NVML "Not Supported". Docker CE in WSL mounts it itself.
    if (-not $UseWsl) { $run += @('-v', '/usr/lib/wsl:/usr/lib/wsl:ro') }
}
$run += @('-e', "MODE=$Mode", '-e', "MAIN_HOST=$Main", '-e', 'CLUSTER_TOKEN', '-e', "NODE_NAME=$Name",
    '-e', "NODE_IP=$NodeIp", '-e', "ACCEL=$Accel")
# Что запускает этот контейнер, для «Версий» на страницах администратора: локальное имя образа и digest, который ему выдал реестр
# главного (pull-image.sh --record, при -Update записывается здесь же). Запись верна, только пока её id совпадает с id этого образа:
# образ из архива или загруженный заново чужой записи не наследует.
$run += @('-e', "NODE_IMAGE=$Image")
if (-not $DryRun) {
    $rec = Read-Text (Join-Data 'state/image.digest')
    $curId = ((Get-DockerOut image inspect -f '{{.Id}}' $Image) -join '').Trim()
    if ($rec -match '^(sha256:[0-9a-f]{64}) (sha256:[0-9a-f]{64})$' -and $matches[1] -eq $curId) { $run += @('-e', "NODE_IMAGE_DIGEST=$($matches[2])") }
}
if ($InsecureTls) { $run += @('-e', 'MAIN_INSECURE_TLS=1') }
# Wake-on-LAN: сетевая карта хоста (не секрет), нода сообщает её порталу.
if ($WolMac) { $run += @('-e', "WOL_MAC=$WolMac", '-e', "WOL_IP=$WolIp", '-e', "WOL_PREFIX=$WolPrefix") }
# Память, которую Docker даёт ноде (в WSL — память WSL): метка llm.nsu/ram-mib для размещения split.
if (-not $DryRun) { $mem = ((Get-DockerOut info --format '{{.MemTotal}}') -join '').Trim(); if ($mem -match '^\d+$') { $run += @('-e', "RAM_MIB=$([math]::Floor([int64]$mem / 1MB))") } }
# Как run-node.sh: HF_ENDPOINT, HF_TOKEN, THREADS из окружения скрипта передаются в контейнер; прокси тоже
# (обе формы имени: curl читает только http_proxy в нижнем регистре, Go — любую).
$passEnv = @('CLUSTER_TOKEN') + @('HF_ENDPOINT', 'HF_TOKEN', 'THREADS', 'MODELS_REPORT_S', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy' |
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

$script:UpdateStage = ''
if ($Update) {
    # брандмауэр и порты при замене контейнера не меняются
    if (-not $DryRun) { Write-Host "Нода $Name обновлена и запущена: контейнер $Container, образ $Image, режим $Mode, адрес $NodeIp." }
    return
}

# Брандмауэр: 18080 только главному (-AllowFrom), 18050 главному и нодам группы split (-RpcPeers, дальше их поддерживает
# -SyncFirewall по ответам главного).
Set-NodeFirewall $RpcPeers
if (-not $DryRun) { Write-Host "Нода $Name запущена: контейнер $Container, образ $Image, режим $Mode, адрес $NodeIp." }
