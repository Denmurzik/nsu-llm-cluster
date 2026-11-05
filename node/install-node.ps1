# Установка Windows-ноды NSU LLM без присмотра: WSL2 + Ubuntu + Docker CE (без Docker Desktop), автозапуск без входа
# пользователя, брандмауэр только для класса и главного, без сна и гибернации. Запуск ноды — node\run-node.ps1 -Wsl.
# PowerShell 5.1, от имени администратора. Идемпотентен: повторный запуск доводит машину до нужного состояния.
#
#   .\install-node.ps1                          # интерактивно: определяет всё, что может, спрашивает недостающее
#   .\install-node.ps1 -NonInteractive -Main 10.0.0.5 -Token T -TaskPassword P   # без вопросов
#   .\install-node.ps1 -NonInteractive -AnswersFile D:\class.json             # ответы из файла (ключи как в install.json)
#   .\install-node.ps1 -Status                  # короткий отчёт о состоянии ноды
#   .\install-node.ps1 -Uninstall               # откатывает всё, что поставил скрипт
#   .\install-node.ps1 -DryRun ...              # печатает действия, ничего не меняет
#   -Docker wsl|desktop: где работает нода, если на машине уже есть Docker Desktop (по умолчанию спрашивает;
#   с -NonInteractive остаётся Docker Desktop). -NonInteractive никогда не читает ввод: годится для ssh.
#   -Proxy http://host:port: прокси для wsl --install/--update, apt и Docker CE в WSL, демона Docker и контейнера ноды.
#   Образ ноды берётся с главного сервера (реестр https://<главный>/v2/, по локальной сети, не из интернета): это умолчание, если
#   образа ещё нет на машине и нет архива (-ImageTar или nsu-llm-node-*.tar.gz рядом со скриптом). -ImageSource main качает с
#   главного всегда (обновление образа), -ImageSource tar берёт только архив. Корень CA главного закрепляется в Docker один раз,
#   только для этого реестра (/etc/docker/certs.d), и дальше проверяется по нему (Docker Desktop: только если Windows доверяет сертификату). Подробности: docs\release.md.
#   -ViaTask: шаги выполняет одноразовая задача планировщика со входом по паролю (сеанс ssh по ключу или WinRM не
#   имеет учётных данных пользователя); включается сама, если такой сеанс обнаружен. Из сеанса ssh или WinRM с -TaskPassword
#   (или NSU_LLM_TASK_PASSWORD) установка тоже идёт такой задачей, на WSL и на Docker Desktop: обрыв сеанса убивает всё, что он
#   запустил, а задача планировщика живёт сама. После обрыва повторный запуск показывает ход (журнал задачи), -Status тоже.
#   Брандмауэр: 18080 (инференс) открыт только главному (-AllowFrom добавляет адреса), 18050 (RPC split, без аутентификации) только
#   главному и нодам текущей группы split: главный называет их в ответе «desired» (rpc_peers), правила обновляются раз в минуту
#   (node-boot.ps1; Docker Desktop: задача NSU-LLM-Node-FwSync от SYSTEM). Подсеть класса целиком никогда не открывается.
#   -CaSha256 <hex>: отпечаток корня CA главного (SHA-256 от DER, страница «Подключение ноды»); скачанный /ca.crt сверяется с ним.
#   Без него отпечаток печатается, а корень принимается только по ответу «да» (или -AcceptCa: доверие при первом обращении).
#   Wake-on-LAN: физическая сетевая карта с адресом ноды получает «Wake on Magic Packet» и право будить ПК, быстрый
#   запуск Windows выключается, MAC и подсеть уходят ноде (портал будит выключенный ПК). -PowerButton nothing (по
#   умолчанию) — кнопка питания ничего не делает, -PowerButton keep — оставить как есть.
#
# Ответы сохраняются в C:\ProgramData\NSU-LLM\install.json (токен зашифрован DPAPI LocalMachine, файл доступен только
# администраторам и SYSTEM),
# пароль задачи планировщика не сохраняется. Токен и пароль без вопросов: -Token и -TaskPassword или переменные
# CLUSTER_TOKEN и NSU_LLM_TASK_PASSWORD. Подробности: docs\deploy-class.md.
[CmdletBinding()]
param(
    [string]$Main = '',
    [string]$Token = '',
    [ValidateSet('', 'k3s', 'standalone')][string]$Mode = '',
    [ValidateSet('', 'auto', 'cpu', 'nvidia')][string]$Accel = '',
    [ValidateSet('', 'auto', 'wsl', 'desktop')][string]$Docker = '',
    [string]$Name = '',
    [string]$NodeIp = '',
    [string]$AllowFrom = '',
    [string]$CaSha256 = '',
    [int]$MemoryGB = 0,
    [string]$Distro = '',
    [string]$DistroSource = '',
    [string]$ImageTar = '',
    [ValidateSet('', 'main', 'tar')][string]$ImageSource = '',
    [string]$TaskPassword = '',
    [string]$InstallDir = '',
    [string]$WslConfigPath = '',
    [string]$AnswersFile = '',
    [string]$Proxy = '',
    [ValidateSet('', 'nothing', 'keep')][string]$PowerButton = '',
    [switch]$InsecureTls,
    [switch]$AcceptCa,
    [switch]$NonInteractive,
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$Status,
    [switch]$Uninstall,
    [switch]$KeepDistro,
    [switch]$ViaTask,
    [switch]$FromTask
)
$ErrorActionPreference = 'Stop'
# --- PSModulePath begin
# Windows PowerShell 5.1, запущенный из pwsh 7 (или по ssh, где оболочка по умолчанию — pwsh 7), наследует его PSModulePath, и модули
# 7.x (Microsoft.PowerShell.Security и др.) в нём не грузятся. Пути pwsh 7 убираются, свой каталог модулей — на месте (как в class-deploy.ps1).
if ($PSVersionTable.PSEdition -ne 'Core') {
    $mp = @("$env:PSModulePath" -split ';' | Where-Object { $_ -and $_ -notmatch '\\PowerShell\\(\d+\\)?Modules\\?$' })
    if ($mp -notcontains "$PSHOME\Modules") { $mp += "$PSHOME\Modules" }
    $env:PSModulePath = $mp -join ';'
}
# --- PSModulePath end
# Защита каталогов и файлов (общая с class-deploy.ps1); без неё ставить нельзя.
$SecureLib = Join-Path $PSScriptRoot 'secure-dir.ps1'
if (-not (Test-Path -LiteralPath $SecureLib)) { Write-Host "ОШИБКА: нет $SecureLib рядом с install-node.ps1" -ForegroundColor Red; Write-Host 'Как исправить: скопируйте каталог node целиком (install-node.ps1, run-node.ps1, node-boot.ps1, wsl-setup.sh, pull-image.sh, IMAGE_VERSION, secure-dir.ps1, class-run.ps1).'; exit 1 }
. $SecureLib
$env:WSL_UTF8 = '1'   # иначе wsl.exe пишет UTF-16 и вывод не разобрать
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# Версия образа ноды: единственная константа в скриптах — IMAGE_VERSION рядом (общая с install-node.ps1 и run-node.sh; её проверяет
# release.sh). Это запасное значение: версию называет главный (NODE_IMAGE_TAG, GET /api/node-image и ответ «desired»).
$Version = ''
try { $Version = "$(Get-Content -LiteralPath (Join-Path $PSScriptRoot 'IMAGE_VERSION') -TotalCount 1 -ErrorAction Stop)".Trim() } catch { }
if ($Version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$') { throw "Нет файла IMAGE_VERSION рядом со скриптом (или в нём не версия вида 1.2.3): скопируйте каталог node целиком" }
$TaskName = 'NSU-LLM-Node'
$TaskPath = '\NSU-LLM\'
$FwRule = 'NSU-LLM-Node'
$FwRuleRpc = 'NSU-LLM-Node-RPC'
$SyncTaskName = 'NSU-LLM-Node-FwSync'                  # Docker Desktop: раз в минуту run-node.ps1 -SyncFirewall (для WSL это делает node-boot.ps1)
$WslCreator = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'   # VMCreatorId WSL в Hyper-V firewall
$MinWsl = [version]'2.4.4'                                # wsl --install --name/--location/--from-file
$MinDriver = 570                                          # образ собран на CUDA 12.8
$UbuntuDistro = 'Ubuntu-24.04'
$CurlExe = $(if ($env:NSU_LLM_CURL) { $env:NSU_LLM_CURL } else { 'curl.exe' })   # не алиас curl из PowerShell 5.1
$InstallTaskName = 'NSU-LLM-Install'
$DockerDesktopExe = $(if ($env:NSU_LLM_DOCKER_DESKTOP_EXE) { $env:NSU_LLM_DOCKER_DESKTOP_EXE } else { Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe' })
$DriverFix = "Скачайте драйвер NVIDIA $MinDriver или новее на https://www.nvidia.com/Download/index.aspx (GeForce, RTX 40 Series, ваша модель, Windows 11) или через приложение NVIDIA App, установите и перезагрузите ПК; проверка: nvidia-smi (Driver Version не ниже $MinDriver). Затем запустите установку снова."
$DesktopFix = 'Войдите в Windows под этой учётной записью и запустите Docker Desktop (по ssh он не запускается), дождитесь «Engine running» и повторите; чтобы он стартовал сам: Settings, General, «Start Docker Desktop when you sign in». ПК класса на Windows 10 держит ноду, только пока в Windows вошёл пользователь; автовход (netplwiz, Winlogon AutoAdminLogon) решает администратор.'
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData 'NSU-LLM' }
if (-not $WslConfigPath) { $WslConfigPath = Join-Path $env:USERPROFILE '.wslconfig' }
$ScriptsDir = Join-Path $InstallDir 'scripts'
# wsl\ — диск дистрибутива: Secure-Dir не трогает его содержимое (право ВМ Hyper-V на ext4.vhdx нужно для запуска WSL).
$SecureExclude = @('wsl')
$AnswersPath = Join-Path $InstallDir 'install.json'     # сюда пишутся ответы этой машины
$AnswersIn = $(if ($AnswersFile) { [IO.Path]::GetFullPath($AnswersFile) } else { $AnswersPath })   # отсюда читаются
$StatePath = Join-Path $InstallDir 'state.json'
$BootLog = Join-Path $InstallDir 'boot.log'
$RunDir = Join-Path $InstallDir 'run'                    # разовая задача (-ViaTask): параметры, журнал, код
$InstallLog = Join-Path $RunDir 'install-task.log'
$InstallActionFile = Join-Path $RunDir 'action.txt'      # что делает задача: install или uninstall (повторный запуск подключается к ней)
$ScriptFiles = @('install-node.ps1', 'run-node.ps1', 'node-boot.ps1', 'wsl-setup.sh', 'pull-image.sh', 'secure-dir.ps1', 'class-run.ps1', 'IMAGE_VERSION')
$Self = $(if ($PSCommandPath) { $PSCommandPath } else { Join-Path $ScriptsDir 'install-node.ps1' })

# ------------------------------------------------------------------ вспомогательное
function Fail([string]$msg, [string]$fix = '') {
    $e = New-Object System.Exception($msg); $e.Data['fix'] = $fix; throw $e
}
function Format-Cmd([string[]]$c) {
    return (($c | ForEach-Object { if ($_ -match '[\s;|&<>]' -or $_ -eq '') { "'" + $_.Replace("'", "''") + "'" } else { $_ } }) -join ' ')
}
function Show-Dry([string]$text) { Write-Host "[dry-run] $text" }
function Say([string]$text) { Write-Host $text }
function Warn([string]$text) { Write-Host "Внимание: $text" -ForegroundColor Yellow }
# Внешняя команда без изменения системы: код и вывод (stderr тоже), отсутствие команды — код 9009.
function Invoke-Read([string[]]$c) {
    $rest = @($c | Select-Object -Skip 1)
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $o = @(& $c[0] @rest 2>&1 | ForEach-Object { "$_" }); $code = $LASTEXITCODE }
    catch { $o = @("$_"); $code = 9009 }
    finally { $ErrorActionPreference = $prev }
    return [pscustomobject]@{ Code = $code; Out = (($o -join "`n") -replace "`0", '').Trim() }
}
# Внешняя команда, меняющая систему: при -DryRun только печать; вывод показывается сразу; ошибка останавливает установку.
function Invoke-Change([string[]]$c, [string]$what, [string]$fix = '', [switch]$AllowFail) {
    if ($DryRun) { Show-Dry (Format-Cmd $c); return [pscustomobject]@{ Code = 0; Out = '' } }
    $rest = @($c | Select-Object -Skip 1)
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $o = @(& $c[0] @rest 2>&1 | ForEach-Object { $s = ("$_" -replace "`0", ''); Write-Host "  $s"; $s }); $code = $LASTEXITCODE }
    catch { $o = @("$_"); $code = 9009 }
    finally { $ErrorActionPreference = $prev }
    if ($code -ne 0 -and -not $AllowFail) { Fail "$what (код $code): $(Format-Cmd $c)" $fix }
    return [pscustomobject]@{ Code = $code; Out = ($o -join "`n") }
}
function Get-WslRoot([string[]]$cmd) { return @('wsl', '-d', $Distro, '-u', 'root', '-e') + $cmd }
function ConvertTo-WslPath([string]$p) {
    $full = [IO.Path]::GetFullPath($p)
    if ($full -notmatch '^([A-Za-z]):\\(.*)$') { Fail "Путь $p недоступен из WSL" 'Скопируйте файл на локальный диск (например, C:\NSU-LLM) и укажите этот путь.' }
    return '/mnt/' + $matches[1].ToLowerInvariant() + '/' + $matches[2].Replace('\', '/')
}
function Test-Admin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function ConvertTo-Plain([Security.SecureString]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Read-Json([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    try { $raw = [IO.File]::ReadAllText($path) }
    catch [UnauthorizedAccessException] { Warn "нет доступа к $path (файл доступен только администраторам): запустите PowerShell от имени администратора"; return $null }
    try { return ($raw | ConvertFrom-Json) }
    catch { Warn "файл $path не читается как JSON и пропущен"; return $null }
}
# Текст файла или '' (в PowerShell 5.1 [string]$null — это $null, а не пустая строка).
function Read-TextFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    $t = Get-Content -Raw -LiteralPath $path
    if ($null -eq $t) { return '' }
    return [string]$t
}
function Get-Prop($obj, [string]$n) { if ($obj -and $obj.PSObject.Properties[$n]) { return $obj.$n }; return $null }
# Вопрос с подставленным значением по умолчанию. Ввод из конвейера (тесты, автоматизация) читается построчно.
function Read-Answer([string]$q, [string]$def = '', [switch]$Secret) {
    if ($NonInteractive) { Fail "Нужен ответ на вопрос «$q», а задан -NonInteractive" 'Передайте значение параметром или в файле ответов (-AnswersFile).' }
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
# Порты ноды, которые уже слушает Windows: в режиме mirrored Linux не сможет их занять.
function Get-BusyPorts([string]$ports) {
    $busy = @()
    foreach ($p in ($ports -split ',')) {
        $l = $null; try { $l = Get-NetTCPConnection -State Listen -LocalPort ([int]$p) -ErrorAction Stop | Select-Object -First 1 } catch { }
        if ($l) {
            $pn = ''; try { $pn = (Get-Process -Id $l.OwningProcess -ErrorAction Stop).ProcessName } catch { }
            $busy += "$p ($(if ($pn) { $pn } else { "PID $($l.OwningProcess)" }))"
        }
    }
    return $busy
}

# ------------------------------------------------------------------ определение состояния (только чтение)
function Get-WslVersion {
    $r = Invoke-Read @('wsl', '--version')
    if ($r.Code -ne 0) { return $null }
    if ($r.Out -match '(\d+\.\d+\.\d+(\.\d+)?)') { return [version]$matches[1] }
    return $null
}
function Get-Distros([switch]$Running) {
    $a = $(if ($Running) { @('wsl', '-l', '--running', '-q') } else { @('wsl', '-l', '-q') })
    $r = Invoke-Read $a
    if ($r.Code -ne 0) { return @() }
    return @($r.Out -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
function Get-WinGpu {
    $r = Invoke-Read @('nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader,nounits')
    if ($r.Code -ne 0 -or -not $r.Out) { return $null }
    $f = @(($r.Out -split "`r?`n")[0] -split ',' | ForEach-Object { $_.Trim() })
    if ($f.Count -lt 3) { return $null }
    $major = 0; [void][int]::TryParse(($f[1] -split '\.')[0], [ref]$major)
    return [pscustomobject]@{ Name = $f[0]; Driver = $f[1]; DriverMajor = $major; VramMib = $f[2] }
}
# Docker Desktop: его дистрибутив docker-desktop или установленный exe.
function Test-DockerDesktop([string[]]$distros) {
    return [bool](($distros | Where-Object { $_ -like 'docker-desktop*' }) -or (Test-Path -LiteralPath $DockerDesktopExe))
}
function Test-WinDocker { return ((Invoke-Read @('docker', 'info', '--format', '{{.OperatingSystem}}')).Code -eq 0) }
function Test-WinImage([string]$img) { return ((Invoke-Read @('docker', 'image', 'inspect', $img)).Code -eq 0) }
function Test-WslGpu { return ((Invoke-Read (Get-WslRoot @('/usr/lib/wsl/lib/nvidia-smi', '-L'))).Code -eq 0) }
function Test-WslImage([string]$img) { return ((Invoke-Read (Get-WslRoot @('docker', 'image', 'inspect', $img))).Code -eq 0) }
# Текущие значения параметра электропитания (секунды): последние два 0x… в выводе powercfg /query — от сети и от батареи.
function Get-PowerIndex([string]$setting, [string]$sub = 'SUB_SLEEP') {
    $r = Invoke-Read @('powercfg', '/query', 'SCHEME_CURRENT', $sub, $setting)
    $m = @([regex]::Matches($r.Out, ':\s*0x([0-9a-fA-F]+)') | ForEach-Object { [Convert]::ToInt64($_.Groups[1].Value, 16) })
    if ($m.Count -lt 2) { return $null }
    return [pscustomobject]@{ Ac = $m[$m.Count - 2]; Dc = $m[$m.Count - 1] }
}
function Get-HibernateEnabled {
    try { return [int](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled -ErrorAction Stop).HibernateEnabled } catch { return 0 }
}
$HiberbootKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
# Быстрый запуск Windows: 1 — «Завершение работы» на деле гибернация ядра, и ПК не просыпается по сети. NSU_LLM_TEST_HIBERBOOT — для тестов.
function Get-Hiberboot {
    if ($env:NSU_LLM_TEST_HIBERBOOT) { return [int]$env:NSU_LLM_TEST_HIBERBOOT }
    try { return [int](Get-ItemProperty -Path $HiberbootKey -Name HiberbootEnabled -ErrorAction Stop).HiberbootEnabled } catch { return $null }
}
# Сетевые карты с адресами IPv4: имя, описание (имя устройства для powercfg), MAC, адрес, префикс, тип.
# NSU_LLM_TEST_NICS подменяет ответ в тестах: JSON-массив с теми же полями и состоянием Wake-on-LAN (см. Get-NicWakeState).
function Get-NicList {
    if ($env:NSU_LLM_TEST_NICS) { return @((ConvertFrom-Json -InputObject $env:NSU_LLM_TEST_NICS) | ForEach-Object { $_ }) }
    $res = @()
    try {
        $ads = @{}; foreach ($a in @(Get-NetAdapter -ErrorAction Stop)) { $ads[[int]$a.ifIndex] = $a }
        foreach ($ip in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)) {
            $a = $ads[[int]$ip.InterfaceIndex]; if (-not $a) { continue }
            $res += [pscustomobject]@{ Name = [string]$a.Name; Description = [string]$a.InterfaceDescription; Mac = [string]$a.MacAddress
                Ip = [string]$ip.IPAddress; Prefix = [int]$ip.PrefixLength; Hardware = [bool]$a.HardwareInterface; Virtual = [bool]$a.Virtual
                Media = [string]$a.PhysicalMediaType }
        }
    }
    catch { }
    return $res
}
# Виртуальные адаптеры, которые не будят ПК: WSL и Hyper-V (vEthernet), VPN, туннели, loopback, гипервизоры.
$VirtualNic = 'vEthernet|Hyper-V|WSL|VPN|AnyConnect|TAP-|Wintun|WireGuard|Tailscale|ZeroTier|Loopback|VirtualBox|VMware|Bluetooth|Miniport'
# Физическая карта с адресом ноды (её MAC будит ПК): {Nic, Why}; Nic $null — почему Wake-on-LAN не настроить.
function Find-WakeNic([string]$ip) {
    if (-not $ip) { return [pscustomobject]@{ Nic = $null; Why = 'адрес ноды не известен' } }
    $n = @(Get-NicList | Where-Object { $_.Ip -eq $ip }) | Select-Object -First 1
    if (-not $n) { return [pscustomobject]@{ Nic = $null; Why = "ни у одной сетевой карты нет адреса $ip" } }
    if (-not $n.Hardware -or $n.Virtual -or "$($n.Name) $($n.Description)" -match $VirtualNic) {
        return [pscustomobject]@{ Nic = $null; Why = "адрес $ip у виртуального адаптера $($n.Name) ($($n.Description)), а не у физической сетевой карты" }
    }
    if ($n.Mac -notmatch '^[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}$' -or $n.Prefix -lt 1 -or $n.Prefix -gt 32) {
        return [pscustomobject]@{ Nic = $null; Why = "у карты $($n.Name) не распознаны MAC $($n.Mac) или префикс /$($n.Prefix)" }
    }
    return [pscustomobject]@{ Nic = $n; Why = '' }
}
function Get-AdvValue([string]$nic, [string]$kw) {
    $p = $null; try { $p = Get-NetAdapterAdvancedProperty -Name $nic -RegistryKeyword $kw -ErrorAction Stop | Select-Object -First 1 } catch { }
    if (-not $p) { return $null }
    return [string](@($p.RegistryValue)[0])
}
# Состояние Wake-on-LAN карты: PmWake — «Wake on Magic Packet» управления питанием (Enabled, Disabled, Unsupported);
# MagicAdv и S5 — свойства драйвера *WakeOnMagicPacket и S5WakeOnLan (пробуждение из выключенного состояния), $null, если их
# у драйвера нет; Armed — устройству разрешено будить ПК (powercfg /devicequery wake_armed).
function Get-NicWakeState($n) {
    if ($env:NSU_LLM_TEST_NICS) {
        return [pscustomobject]@{ PmWake = (Get-Prop $n 'PmWake'); MagicAdv = (Get-Prop $n 'MagicAdv'); S5 = (Get-Prop $n 'S5'); Armed = [bool](Get-Prop $n 'Armed') }
    }
    $pm = $null; try { $pm = [string](Get-NetAdapterPowerManagement -Name $n.Name -ErrorAction Stop).WakeOnMagicPacket } catch { }
    $armed = @((Invoke-Read @('powercfg', '/devicequery', 'wake_armed')).Out -split "`r?`n" | ForEach-Object { $_.Trim() }) -contains $n.Description
    return [pscustomobject]@{ PmWake = $pm; MagicAdv = (Get-AdvValue $n.Name '*WakeOnMagicPacket'); S5 = (Get-AdvValue $n.Name 'S5WakeOnLan'); Armed = $armed }
}
function Format-PsName([string]$n) { if ($n -match '[\s;|&<>''()]') { return "'" + $n.Replace("'", "''") + "'" }; return $n }
# Командлет, меняющий систему: при -DryRun только печать; ошибка — предупреждение (драйверы сетевых карт умеют разное).
function Invoke-CmdletChange([string]$text, [scriptblock]$do) {
    if ($DryRun) { Show-Dry $text; return }
    try { & $do | Out-Null } catch { Warn "$text`: $($_.Exception.Message)" }
}
# Wake on Magic Packet и свойства драйвера (-NoRestart: карта не перезапускается во время установки, действует после перезагрузки).
function Set-NicWake([string]$nic, [string]$pm, $adv, $s5) {
    if ($pm) { Invoke-CmdletChange "Set-NetAdapterPowerManagement -Name $(Format-PsName $nic) -WakeOnMagicPacket $pm -NoRestart" { Set-NetAdapterPowerManagement -Name $nic -WakeOnMagicPacket $pm -NoRestart -ErrorAction Stop } }
    foreach ($p in @(@('*WakeOnMagicPacket', $adv), @('S5WakeOnLan', $s5))) {
        $kw = $p[0]; $v = [string]$p[1]
        if ($v -match '^\d+$') { Invoke-CmdletChange "Set-NetAdapterAdvancedProperty -Name $(Format-PsName $nic) -RegistryKeyword $kw -RegistryValue $v -NoRestart" { Set-NetAdapterAdvancedProperty -Name $nic -RegistryKeyword $kw -RegistryValue $v -NoRestart -ErrorAction Stop } }
    }
}
function Resolve-MainIp([string]$m) {
    $ip = $null
    if ([Net.IPAddress]::TryParse($m, [ref]$ip)) { return $ip.IPAddressToString }
    try { return ([Net.Dns]::GetHostAddresses($m) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1).IPAddressToString } catch { return '' }
}
function Get-LocalIp([string]$mainIp) {
    if (-not $mainIp) { return '' }
    try { return [string](Find-NetRoute -RemoteIPAddress $mainIp -ErrorAction Stop | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress } catch { return '' }
}
# Входит ли адрес IPv4 в список адресов и подсетей CIDR.
function Test-AddrAllowed([string]$ip, [string]$list) {
    $a = [Net.IPAddress]::Parse($ip).GetAddressBytes(); [Array]::Reverse($a); $ai = [BitConverter]::ToUInt32($a, 0)
    foreach ($e in ($list -split ',')) {
        $p = $e -split '/'; $bits = $(if ($p.Count -gt 1) { [int]$p[1] } else { 32 })
        $ref = $null; if (-not [Net.IPAddress]::TryParse($p[0], [ref]$ref)) { continue }
        $b = $ref.GetAddressBytes(); [Array]::Reverse($b); $bi = [BitConverter]::ToUInt32($b, 0)
        $mask = [uint32]$(if ($bits -le 0) { 0 } else { ([uint64]4294967295 -shl (32 - $bits)) -band [uint64]4294967295 })
        if (($ai -band $mask) -eq ($bi -band $mask)) { return $true }
    }
    return $false
}
# Сохранённый список прежней версии: подсеть (не одиночный адрес), в которую входит сама нода.
function Test-LegacyAllow([string]$list, [string]$nodeIp) {
    if (-not $nodeIp) { return $false }
    foreach ($e in ($list -split ',')) {
        if ($e -match '/(\d+)$' -and [int]$matches[1] -lt 32 -and (Test-AddrAllowed $nodeIp $e)) { return $true }
    }
    return $false
}
# Округление: ПК «32 ГБ» сообщает около 31,8 ГиБ.
function Get-TotalRamGB { try { return [int][math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB) } catch { return 0 } }
# Ключи .wslconfig по https://learn.microsoft.com/windows/wsl/wsl-config (секции [wsl2], [general], [experimental]).
function Get-WslConfigWant([int]$gb) {
    return @(
        @{ S = 'wsl2'; K = 'memory'; V = "${gb}GB" },
        @{ S = 'wsl2'; K = 'swap'; V = '0' },
        @{ S = 'wsl2'; K = 'networkingMode'; V = 'mirrored' },
        @{ S = 'wsl2'; K = 'dnsTunneling'; V = 'false' },
        @{ S = 'wsl2'; K = 'vmIdleTimeout'; V = '-1' },
        @{ S = 'general'; K = 'instanceIdleTimeout'; V = '-1' },
        @{ S = 'experimental'; K = 'autoMemoryReclaim'; V = 'gradual' }
    )
}
# Вписывает ключи в INI-текст: заменяет существующие, добавляет недостающие в свои секции, остальное не трогает.
function Set-IniValues([string]$text, [object[]]$want) {
    $lines = New-Object System.Collections.Generic.List[string]
    if ($text) { foreach ($l in ($text -split "`r?`n")) { $lines.Add($l) } }
    while ($lines.Count -gt 0 -and -not $lines[$lines.Count - 1].Trim()) { $lines.RemoveAt($lines.Count - 1) }
    foreach ($w in $want) {
        $sec = -1; $end = -1; $found = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            $l = $lines[$i].Trim()
            if ($l -match '^\[(.+)\]$') {
                if ($sec -ge 0 -and $end -lt 0) { $end = $i }
                if ($sec -lt 0 -and $matches[1].Trim() -ieq $w.S) { $sec = $i }
                continue
            }
            if ($sec -ge 0 -and $end -lt 0 -and $l -match '^([^=#;]+)=' -and $matches[1].Trim() -ieq $w.K) { $found = $i }
        }
        $line = "$($w.K)=$($w.V)"
        if ($found -ge 0) { $lines[$found] = $line }
        elseif ($sec -ge 0) {
            if ($end -lt 0) { $end = $lines.Count }
            while ($end -gt $sec + 1 -and -not $lines[$end - 1].Trim()) { $end-- }
            $lines.Insert($end, $line)
        }
        else { if ($lines.Count -gt 0) { $lines.Add('') }; $lines.Add("[$($w.S)]"); $lines.Add($line) }
    }
    return (($lines -join "`r`n") + "`r`n")
}
function Get-IniValue([string]$text, [string]$section, [string]$key) {
    $cur = ''
    foreach ($l in ($text -split "`r?`n")) {
        $t = $l.Trim()
        if ($t -match '^\[(.+)\]$') { $cur = $matches[1].Trim(); continue }
        if ($cur -ieq $section -and $t -match '^([^=#;]+)=(.*)$' -and $matches[1].Trim() -ieq $key) { return $matches[2].Trim() }
    }
    return ''
}
# Имя образа называет главный (GET /api/node-image, без токена: NODE_IMAGE_TAG главного), а не константа скрипта: раньше скрипт искал
# nsu-llm-node:0.1.0-gpu, когда главный работал на 0.1.1. Нет ответа (главный старее, недоступен, -DryRun без сети): IMAGE_VERSION.
$ImageNames = @{}
$ImageDigests = @{}
function Get-ImageDigest([string]$img) {
    $flavour = $(if ($img -like '*-gpu') { 'nvidia' } else { 'cpu' })
    [void](Get-ImageName $flavour)
    if ($ImageDigests.ContainsKey($flavour) -and $ImageNames[$flavour] -eq $img) { return $ImageDigests[$flavour] }
    return ''
}
function Get-ImageName([string]$acc) {
    $flavour = $(if ($acc -eq 'nvidia') { 'nvidia' } else { 'cpu' })
    if (-not $ImageNames.ContainsKey($flavour)) {
        $n = ''
        if ($Main) {
            try {
                $r = Invoke-HttpsGet "https://$Main/api/node-image"
                if ($r.Code -eq '200') {
                    $j = $r.Body | ConvertFrom-Json
                    $n = "$($j.$flavour)"
                    # digest образа в реестре главного (portal отдаёт его вместе с именем): первая загрузка идёт по нему, а не по тегу
                    $dg = ''; try { $dg = "$($j.digest.$flavour)" } catch { }
                    if ($n -cmatch '^nsu-llm-node:[A-Za-z0-9._-]+-(cpu|gpu)$' -and $dg -cmatch '^sha256:[0-9a-f]{64}$') { $ImageDigests[$flavour] = $dg }
                }
            }
            catch { }
        }
        if ($n -cnotmatch '^nsu-llm-node:[A-Za-z0-9._-]+-(cpu|gpu)$') { $n = "nsu-llm-node:$Version-" + $(if ($flavour -eq 'nvidia') { 'gpu' } else { 'cpu' }) }
        $ImageNames[$flavour] = $n
    }
    return $ImageNames[$flavour]
}
# Версия образа, как её назвал главный: для имён архивов nsu-llm-node-<версия>-cpu|gpu.tar.gz
function Get-NodeVersion { return ((Get-ImageName 'cpu') -replace '^nsu-llm-node:(.+)-cpu$', '$1') }
# Файловая система тома (FAT32/exFAT не хранят прав доступа). NSU_LLM_TEST_FS подменяет ответ в тестах.
function Get-FsFormat([string]$p) {
    if ($env:NSU_LLM_TEST_FS) { return $env:NSU_LLM_TEST_FS }
    try { return ([IO.DriveInfo]::new([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($p)))).DriveFormat } catch { return '' }
}
function Find-ImageTar([string]$acc) {
    $tag = $(if ($acc -eq 'nvidia') { 'gpu' } else { 'cpu' })
    # Только каталог скрипта: родитель раздачи — C:\ProgramData, куда может писать любой пользователь, а образ запускается с
    # токеном кластера в окружении. Найденный архив должен быть доверенным (владелец и права на запись — администраторы).
    foreach ($pat in @("nsu-llm-node-$(Get-NodeVersion)-$tag.tar*", "nsu-llm-node-*-$tag.tar*")) {
        foreach ($f in @(Get-ChildItem -Path $PSScriptRoot -Filter $pat -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
            if (Test-TrustedPath $f.FullName) { return $f.FullName }
            $fs = Get-FsFormat $f.FullName
            if ($fs -and $fs -notin @('NTFS', 'ReFS')) { Warn "архив $($f.FullName) пропущен: он на диске $fs без прав доступа, доверять ему нельзя: скопируйте архив на NTFS-диск (например, C:\) и укажите -ImageTar." }
            else { Warn "архив $($f.FullName) пропущен: его создал или может менять не администратор" }
        }
    }
    return ''
}
# Образ ноды с главного (реестр https://<главный>/v2/): имя после загрузки nsu-llm-node:<версия>-cpu|gpu, как у образа из архива.
# WSL: скрипт pull-image.sh в дистрибутиве. Корень CA главного закрепляется ОДИН раз в /etc/docker/certs.d/<главный>/ca.crt (только для
# этого реестра), дальше реестр проверяется только по нему, а несовпадение это ошибка: заново корень не скачивается (подмена CA в сети
# дала бы ноде чужой образ с правами root).
# Docker Desktop: корень CA нельзя задать для одного реестра без ручных действий (Docker Desktop берёт доверенные корни из хранилища
# Windows, каталог ~/.docker/certs.d он не гарантирует), а добавлять CA главного в хранилище всей машины установщик не вправе. Поэтому
# docker.exe качает образ, только если сертификат главного уже принимает Windows (свой сертификат НГУ или корень главного, добавленный
# администратором); иначе остановка с понятным сообщением: запасные пути -Docker wsl и -ImageTar.
# Корень CA главного для Docker в WSL закрепляется только после сверки с отпечатком: pull-image.sh без --ca берёт его с https://<главный>/ca.crt
# без проверки (первый, кто ответил в сети, получил бы токен и образ ноды с правами root). Здесь корень скачивается, его SHA-256 (от DER)
# сверяется с -CaSha256; без отпечатка он печатается и принимается только по ответу «да» или с -AcceptCa. Проверенный корень кладётся в
# state ноды (его читает run-node.ps1) и передаётся pull-image.sh через --ca.
function Get-PemSha256([string]$pem) {
    if ($pem -notmatch '(?s)-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----') { return '' }
    $der = [Convert]::FromBase64String(($matches[1] -replace '\s', ''))
    return ((([Security.Cryptography.SHA256]::Create().ComputeHash($der)) | ForEach-Object { $_.ToString('x2') }) -join '')
}
function Confirm-MainCa([string]$fp) {
    if ($CaSha256) {
        if ($fp -eq $CaSha256) { Say "Корень CA главного совпал с -CaSha256 ($fp)."; return }
        Fail "Корень CA, который отдал https://$Main/ca.crt, не совпал с -CaSha256: получен $fp, ожидался $CaSha256" 'Возможна подмена в сети (ARP, DNS) или CA главного сменили. Корень не сохранён, токен не отправлялся. Сверьте отпечаток со страницей «Подключение ноды».'
    }
    Say "Отпечаток корня CA главного (SHA-256): $fp"
    if ($AcceptCa) { Warn 'отпечаток не задан (-CaSha256), корень принят по -AcceptCa без сверки: доверие при первом обращении. Сверьте отпечаток со страницей «Подключение ноды».'; return }
    if ($NonInteractive) { Fail "Корень CA главного ($fp) не подтверждён: нет -CaSha256, а ответить некому (-NonInteractive)" 'Передайте отпечаток со страницы «Подключение ноды» (-CaSha256) или, если сеть доверенная, -AcceptCa.' }
    if ((Read-Answer 'Сверьте отпечаток со страницей «Подключение ноды». Доверять этому корню? (д/н)' 'н') -notmatch '^(д|да|y|yes)$') { Fail 'Корень CA главного не принят' 'Повторите с -CaSha256 <отпечаток> или проверьте сеть.' }
}
$WslCaPath = '/var/lib/nsu-llm-node/state/ca.crt'
function Save-WslCa([string]$pem) {
    $save = Get-WslRoot @('bash', '-c', "umask 077; mkdir -p /var/lib/nsu-llm-node/state && chmod 700 /var/lib/nsu-llm-node /var/lib/nsu-llm-node/state && cat > $WslCaPath")
    $pem | & $save[0] $save[1..($save.Count - 1)]
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось сохранить корень CA в $WslCaPath дистрибутива $Distro" "wsl -d $Distro -u root -e ls -la /var/lib/nsu-llm-node" }
}
function Confirm-WslCa {
    if ($InsecureTls) { return $false }
    if ($DryRun) { Show-Dry "проверить корень CA главного (https://$Main/ca.crt) по отпечатку $(if ($CaSha256) { $CaSha256 } else { '(не задан: печатается и подтверждается)' }), сохранить в $WslCaPath дистрибутива $Distro и передать pull-image.sh через --ca"; return $true }
    if ((Invoke-Read (Get-WslRoot @('test', '-s', $WslCaPath))).Code -eq 0) { return $true }
    # Токен K10<sha256 CA k3s>::... проверяет корень главного сам (подпись /ca.crt.p7s CA k3s): доверять первому ответившему не нужно.
    # Явный -CaSha256 имеет приоритет; токен без K10 или главный без подписи: решает отпечаток ниже.
    if (-not $CaSha256) {
        $t = Test-MainCaByToken $Main $Token $CurlExe
        if ($t.Status -eq 'verified') { Say "Корень CA главного проверен по токену (CA k3s), отпечаток SHA-256 $($t.Fp)."; Save-WslCa $t.Pem; return $true }
        if ($t.Status -eq 'mismatch') { Fail "$($t.Why)" 'Корень не сохранён, токен не отправлялся. Сверьте токен со страницей «Подключение ноды»; если CA главного сменили намеренно, возьмите новый токен.' }
        if ($t.Status -eq 'unreachable') { Fail "Корень главного не удалось проверить: $($t.Why)" 'Проверьте, что главный запущен, а порты 443 и 6443 доступны с этого ПК, и повторите. Без проверки образ не качается: pull-image.sh взял бы корень без сверки.' }
        if ($t.Status -eq 'public') {
            # У главного свой публичный сертификат: pull-image.sh должен проверить его системными корнями дистрибутива. Если те его не приняли,
            # pull-image.sh сам взял бы /ca.crt с -k и закрепил (подмена CA = чужой образ с root): проверяем заранее и отказываем.
            $sh = "c=`$(curl -sS -m 15 -o /dev/null -w '%{http_code}' https://$Main/v2/ 2>/dev/null); case `$c in 000|'') exit 1;; esac; exit 0"
            if ((Invoke-Read (Get-WslRoot @('bash', '-c', $sh))).Code -ne 0) { Fail "Реестр https://$Main/v2/ не принят системными корнями дистрибутива $Distro, а /ca.crt главный не отдаёт" 'Это подмена в сети или сертификат главного не из доверенных. Образ не качается: используйте архив (-ImageTar) или проверьте сертификат главного.' }
            return $false
        }
        Say "Проверить корень главного по токену нечем: $($t.Why)."
    }
    $tmp = [IO.Path]::GetTempFileName()
    $pem = ''
    try {
        $r = Invoke-Read @($CurlExe, '-sSf', '-k', '--ssl-no-revoke', '--connect-timeout', '5', '-m', '15', '-o', $tmp, "https://$Main/ca.crt")
        if ($r.Code -eq 0 -and (Test-Path -LiteralPath $tmp)) { $pem = [IO.File]::ReadAllText($tmp) }
    }
    finally { Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue }
    # Портал со своим сертификатом (--certs) /ca.crt не отдаёт: проверка по системным корням, закреплять нечего.
    if ($pem -notmatch 'BEGIN CERTIFICATE') { return $false }
    Confirm-MainCa (Get-PemSha256 $pem)
    Save-WslCa $pem
    return $true
}
function Invoke-ImagePull([string]$img, [bool]$desktop) {
    $fixArchive = "Или возьмите образ из архива: -ImageTar <путь к nsu-llm-node-$(Get-NodeVersion)-$(if ($img -like '*-gpu') { 'gpu' } else { 'cpu' }).tar.gz> (docs\deploy-class.md, «Распространение образов»)."
    if (-not $desktop) {
        $sh = ConvertTo-WslPath (Join-Path $ScriptsDir 'pull-image.sh')
        $caOk = Confirm-WslCa
        $pullArgs = @('bash', $sh, '--main', $Main, '--image', $img, '--no-tofu')   # --no-tofu: без закреплённого корня pull-image.sh не берёт /ca.crt без проверки
        if ($caOk) { $pullArgs += @('--ca', $WslCaPath) }
        $imgDigest = Get-ImageDigest $img
        if ($imgDigest) { $pullArgs += @('--digest', $imgDigest) }
        # pull-image.sh записывает «<id образа> <digest реестра>» в state ноды (run-node.ps1 передаёт его контейнеру, нода сообщает главному);
        # флаг есть не у всех версий скрипта, а без него всё работает, только digest неизвестен.
        $pullSh = Join-Path $ScriptsDir 'pull-image.sh'
        if ((Test-Path -LiteralPath $pullSh) -and (Select-String -LiteralPath $pullSh -Pattern '--record' -SimpleMatch -Quiet)) { $pullArgs += @('--record', '/var/lib/nsu-llm-node/state/image.digest') }
        Invoke-Change (Get-WslRoot $pullArgs) "Не удалось скачать образ $img с главного" "Проверьте, что главный запущен и в нём есть реестр (https://$Main/v2/, docs\release.md) и что адрес ПК входит в REGISTRY_ALLOW_FROM главного. Сообщение «не совпал с сохранённым корнем»: либо подмена в сети, либо CA главного сменили намеренно (тогда удалите /etc/docker/certs.d/$Main/ca.crt в дистрибутиве $Distro). $fixArchive" | Out-Null
        if (-not $DryRun -and -not (Test-WslImage $img)) { Fail "После загрузки с главного образа $img нет" $fixArchive }
        return
    }
    if ($DryRun) { Show-Dry "$CurlExe -sS https://$Main/v2/ (сертификат главного должен приниматься Windows: CA в Docker Desktop установщик не добавляет)" }
    else {
        # --ssl-no-revoke: a private CA has no revocation list, and Schannel otherwise fails with CRYPT_E_NO_REVOCATION_CHECK
        $r = Invoke-Read @($CurlExe, '-sS', '--ssl-no-revoke', '-m', '10', '-o', 'NUL', '-w', '%{http_code}', "https://$Main/v2/")
        if ($r.Out -notmatch '^(200|401)$') {
            Fail "Реестр главного https://$Main/v2/ недоступен для Windows (ответ: $($r.Out)): сертификат главного, вероятно, не доверенный (внутренний CA), а Docker Desktop не позволяет доверить CA одному реестру без ручной настройки" "Используйте -Docker wsl (Docker CE в WSL закрепляет корень главного сам). $fixArchive Либо добавьте корень главного (http://$Main/ca.crt) в доверенные корневые центры Windows вручную: так вы доверяете ему на всей машине."
        }
    }
    $imgDigest = Get-ImageDigest $img
    $remoteRef = $(if ($imgDigest) { "$Main/nsu-llm-node@$imgDigest" } else { "$Main/$img" })   # по digest, который назвал главный, если он его назвал
    Invoke-Change @('docker', 'pull', $remoteRef) "Не удалось скачать образ $img с главного" "Реестр главного: https://$Main/v2/. $fixArchive" | Out-Null
    Invoke-Change @('docker', 'tag', $remoteRef, $img) "docker tag $img не выполнился" | Out-Null
    Invoke-Change @('docker', 'rmi', $remoteRef) "docker rmi $remoteRef" -AllowFail | Out-Null
    if (-not $DryRun -and -not (Test-WinImage $img)) { Fail "После загрузки с главного образа $img нет" $fixArchive }
}
function Get-FileSig([string]$p) { $i = Get-Item -LiteralPath $p; return "$($i.Length)|$($i.LastWriteTimeUtc.Ticks)" }
function Get-CurrentUser { return [Security.Principal.WindowsIdentity]::GetCurrent().Name }
function Get-BootArgs { return "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $ScriptsDir 'node-boot.ps1')`" -Distro $Distro -Log `"$BootLog`"" }
function Get-BootTask { try { return (Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop) } catch { return $null } }
function Get-SyncTask { try { return (Get-ScheduledTask -TaskPath $TaskPath -TaskName $SyncTaskName -ErrorAction Stop) } catch { return $null } }
function Get-FwPorts([string]$m) { return $(if ($m -eq 'standalone') { '18080' } else { '18080,18050' }) }

# ------------------------------------------------------------------ ответы
function Initialize-Answers {
    if ($AnswersFile -and -not (Test-Path -LiteralPath $AnswersIn)) { Fail "Файл ответов $AnswersIn не найден" 'Проверьте путь -AnswersFile.' }
    # Каталог, который могли менять не администраторы, не источник ответов: install.json с чужим адресом главного увёл бы токен.
    $script:DirTrusted = Test-TrustedPath $InstallDir -Exclude $SecureExclude
    if (-not $script:DirTrusted) {
        Warn "каталог $InstallDir не доверенный (владелец или права на запись не у администраторов): install.json и state.json из него не читаются; установка сбросит его права."
    }
    $script:answers = $(if ($AnswersFile) { Read-Json $AnswersIn } elseif ($script:DirTrusted) { Read-Json $AnswersPath } else { $null })
    $script:state = $(if ($script:DirTrusted) { Read-Json $StatePath } else { $null })
    if (-not $script:state) { $script:state = New-Object psobject }
}
# Перед первой записью в $InstallDir: владелец Administrators, защищённый DACL (secure-dir.ps1).
function Initialize-InstallDir {
    if ($script:DirSecured) { return }
    Secure-Dir -Path $InstallDir -Exclude $SecureExclude -DryRun:$DryRun
    $script:DirSecured = $true
}
function Copy-ScriptFiles {
    if ($DryRun) { Show-Dry "скопировать $($ScriptFiles -join ', ') -> $ScriptsDir (новые файлы вместо старых)"; return }
    Initialize-InstallDir
    New-Item -ItemType Directory -Force -Path $ScriptsDir | Out-Null
    foreach ($f in $ScriptFiles) {
        $src = Join-Path $PSScriptRoot $f; $dst = Join-Path $ScriptsDir $f
        if ([IO.Path]::GetFullPath($src) -eq [IO.Path]::GetFullPath($dst)) { continue }
        $b = [IO.File]::ReadAllBytes($src)
        # bash не выполнит скрипт с CRLF (копия из zip или checkout с autocrlf): переводы строк приводятся к LF.
        if ($f -like '*.sh') { $b = [Text.Encoding]::UTF8.GetBytes(([Text.Encoding]::UTF8.GetString($b) -replace "`r`n", "`n")) }
        Write-SecureFile -Path $dst -Bytes $b
    }
}
function Set-State([string]$n, $v) { $state | Add-Member -NotePropertyName $n -NotePropertyValue $v -Force }
function Save-State {
    if ($DryRun) { return }
    Initialize-InstallDir
    Write-SecureFile -Path $StatePath -Text ($state | ConvertTo-Json -Depth 5)
}
function Pick([string]$given, [string]$name, [string]$def = '') {
    if ($given) { return $given }
    $v = Get-Prop $answers $name
    if ($null -ne $v -and "$v" -ne '' -and "$v" -ne '0') { return "$v" }
    return $def
}
# Секреты в файлах: DPAPI LocalMachine (работает и в сеансах без учётных данных: ssh по ключу, WinRM), файл — только
# администраторам и SYSTEM. Без командлетов *-SecureString: их модуль не грузится, если PSModulePath унаследован от pwsh 7.
function Protect-Secret([string]$plain) {
    Add-Type -AssemblyName System.Security
    $b = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($plain), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [Convert]::ToBase64String($b)
}
function Unprotect-Secret([string]$enc) {
    Add-Type -AssemblyName System.Security
    $b = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($enc), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [Text.Encoding]::UTF8.GetString($b)
}
# Прежний формат (TokenProtected): вывод ConvertFrom-SecureString — шестнадцатеричный DPAPI CurrentUser от строки UTF-16.
function Unprotect-UserSecret([string]$hex) {
    Add-Type -AssemblyName System.Security
    $bytes = New-Object byte[] ([int]($hex.Length / 2))
    for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($hex.Substring(2 * $i, 2), 16) }
    $b = [Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    return [Text.Encoding]::Unicode.GetString($b)
}
# Есть ли у сеанса учётные данные пользователя (DPAPI CurrentUser работает). Вход по ssh-ключу (S4U) и WinRM их не дают.
function Test-UserCredentials {
    if ($env:NSU_LLM_ASSUME_NO_CREDENTIALS) { return $false }
    try {
        Add-Type -AssemblyName System.Security
        [void][Security.Cryptography.ProtectedData]::Protect([byte[]](1, 2, 3), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        return $true
    }
    catch { return $false }
}
# Сеанс ssh или WinRM (PowerShell remoting): когда он обрывается, Windows завершает всё, что он запустил (задания сеанса), и установка
# умирает на полпути. Задача планировщика живёт сама. NSU_LLM_ASSUME_REMOTE=1|0 — только для тестов.
function Test-RemoteSession {
    if ($env:NSU_LLM_ASSUME_REMOTE) { return $env:NSU_LLM_ASSUME_REMOTE -eq '1' }
    if ($env:SSH_CONNECTION -or $env:SSH_CLIENT -or $env:SSH_TTY) { return $true }
    return [bool](Get-Variable -Name PSSenderInfo -ErrorAction SilentlyContinue)
}
# Идёт ли разовая задача установки или удаления (NSU_LLM_TEST_INSTALL_TASK=Running — только для тестов).
function Get-InstallTaskRunning {
    if ($env:NSU_LLM_TEST_INSTALL_TASK) { return $(if ($env:NSU_LLM_TEST_INSTALL_TASK -eq 'Running') { [pscustomobject]@{ State = 'Running' } } else { $null }) }
    try { $t = Get-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName -ErrorAction Stop; if ("$($t.State)" -eq 'Running') { return $t } } catch { }
    return $null
}
# Кто вошёл в Windows (владельцы explorer.exe, в любом сеансе, включая RDP); $null, если узнать не удалось. NSU_LLM_TEST_LOGON=none|unknown|имя — только для тестов.
function Get-LoggedOnUsers {
    if ($env:NSU_LLM_TEST_LOGON) {
        if ($env:NSU_LLM_TEST_LOGON -eq 'unknown') { return $null }
        return ,@($env:NSU_LLM_TEST_LOGON -split ',' | Where-Object { $_ -and $_ -ne 'none' })
    }
    $names = @()
    try {
        foreach ($proc in @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop)) {
            $o = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction Stop
            if ($o.User) { $names += "$($o.Domain)\$($o.User)" }
        }
    }
    catch { return $null }   # запрос не удался (нет прав): «неизвестно», а не «никто не вошёл»
    return ,@($names | Select-Object -Unique)   # запятая: пустой список остаётся списком (известно, что никто не вошёл), $null не путается с ним
}
# end secrets
# Прокси с паролем: в выводе и в открытом поле install.json — http://user:***@host; полный адрес — только под DPAPI.
function Get-MaskedUrl([string]$u) { return ($u -replace '^(https?://[^:@/]+):[^@/]+@', '$1:***@') }
# Полный путь относительно текущего каталога PowerShell (разовая задача стартует в другом каталоге).
function Get-AbsPath([string]$p) { if (-not $p) { return '' }; return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p) }
function Get-SavedToken {
    $m = Get-Prop $answers 'TokenMachine'
    if ($m) {
        try { return Unprotect-Secret $m } catch { Warn 'сохранённый токен не расшифровывается (файл ответов с другой машины?): нужен -Token или CLUSTER_TOKEN'; return '' }
    }
    $enc = Get-Prop $answers 'TokenProtected'
    if (-not $enc) { return '' }
    try { return Unprotect-UserSecret $enc } catch { Warn 'сохранённый токен (старый формат, DPAPI пользователя) не расшифровывается в этом сеансе (другой пользователь или вход по ssh-ключу): нужен -Token или CLUSTER_TOKEN'; return '' }
}
# Сетевая проверка главного: HTTPS GET (без проверки сертификата: CA портала частный; мимо прокси).
function Invoke-HttpsGet([string]$url) {
    $tmp = [IO.Path]::GetTempFileName()
    try {
        $r = Invoke-Read @($CurlExe, '-sk', '--noproxy', '*', '--connect-timeout', '4', '-m', '8', '-o', $tmp, '-w', '%{http_code}', $url)
        $code = $(if ($r.Out -match '(\d{3})\s*$') { $matches[1] } else { '000' })
        return [pscustomobject]@{ Code = $code; Body = (Read-TextFile $tmp) }
    }
    finally { Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue }
}
# Готов ли главный: /readyz портала (200 и JSON {db, litellm}) и, для k3s, TLS на 6443. Список {Ok, Text, Fix}.
function Get-MainProbe([string]$m, [string]$mode) {
    $res = @()
    $fix = "проверьте адрес -Main, сеть и брандмауэр главного (входящие TCP 443$(if ($mode -ne 'standalone') { ', 6443' })); на главном: curl -sk https://localhost/readyz"
    $r = Invoke-HttpsGet "https://$m/readyz"
    $j = $null; try { $j = $r.Body | ConvertFrom-Json } catch { }
    $portal = $j -and $j.PSObject.Properties['db'] -and $j.PSObject.Properties['litellm']
    if ($r.Code -eq '000') { $res += [pscustomobject]@{ Ok = $false; Text = "главный $m недоступен по HTTPS: нет ответа от https://$m/readyz"; Fix = $fix } }
    elseif ($portal -and $r.Code -eq '200') { $res += [pscustomobject]@{ Ok = $true; Text = "главный $m`: портал готов (https://$m/readyz: 200)"; Fix = '' } }
    elseif ($portal) { $res += [pscustomobject]@{ Ok = $false; Text = "портал на $m отвечает, но не готов (/readyz $($r.Code): db=$($j.db), litellm=$($j.litellm))"; Fix = 'на главном: docker logs llm-main; подождите запуска Postgres и LiteLLM' } }
    else { $res += [pscustomobject]@{ Ok = $false; Text = "по https://$m/readyz отвечает не портал NSU LLM (код $($r.Code))"; Fix = 'проверьте адрес -Main: это должен быть главный сервер NSU LLM' } }
    if ($mode -ne 'standalone') {
        $k = Invoke-HttpsGet "https://${m}:6443/ping"
        if ($k.Code -eq '000') { $res += [pscustomobject]@{ Ok = $false; Text = "порт 6443 (k3s) на $m не отвечает по TLS: нода k3s не присоединится"; Fix = $fix } }
        else { $res += [pscustomobject]@{ Ok = $true; Text = "k3s на ${m}:6443 отвечает (TLS, код $($k.Code))"; Fix = '' } }
    }
    return $res
}
# Прокси: адрес без пробелов и знаков оболочки; NO_PROXY — главный, класс, эта машина и сети k3s.
function Test-ProxyUrl([string]$u) { return ($u -match '^https?://[A-Za-z0-9._~:@%/+\-\[\]]+$') }
function Get-NoProxy([string]$main, [string]$mainIp, [string]$allow, [string]$nodeIp) {
    $l = @('localhost', '127.0.0.1', '::1', $main, $mainIp, $nodeIp) + @($allow -split ',') + @('100.64.0.0/16', '100.65.0.0/16', '.svc', '.cluster.local')
    return ((@($l | Where-Object { $_ }) | Select-Object -Unique) -join ',')
}

function Resolve-Answers {
    $script:Main = Pick $Main 'Main'
    $m = Pick $Mode 'Mode'
    if ($m -notin @('', 'k3s', 'standalone')) { Fail "Неизвестный режим $m в $AnswersIn" 'Укажите -Mode k3s или -Mode standalone.' }
    $script:Mode = $m
    $a = Pick $Accel 'Accel' 'auto'
    if ($a -notin @('auto', 'cpu', 'nvidia')) { Fail "Неизвестный ускоритель $a в $AnswersIn" 'Укажите -Accel auto, cpu или nvidia.' }
    $script:Accel = $a
    $d = Pick $Docker 'Docker' 'auto'
    if ($d -notin @('auto', 'wsl', 'desktop')) { Fail "Неизвестное значение Docker=$d в $AnswersIn" 'Укажите -Docker wsl или -Docker desktop.' }
    $script:Docker = $d
    $script:Name = (Pick $Name 'Name' $env:COMPUTERNAME).ToLowerInvariant()
    $script:Distro = Pick $Distro 'Distro' 'NSU-LLM'
    $script:DistroSource = Get-AbsPath (Pick $DistroSource 'DistroSource')
    $script:ImageTarExplicit = [bool]$ImageTar   # -ImageTar в командной строке: исчезнувший архив — ошибка, а не повод качать с главного
    $script:ImageTar = Get-AbsPath (Pick $ImageTar 'ImageTar')
    $is = Pick $ImageSource 'ImageSource'
    if ($is -notin @('', 'main', 'tar')) { Fail "Неизвестный источник образа $is в $AnswersIn" 'Укажите -ImageSource main (с главного) или -ImageSource tar (из архива).' }
    $script:ImageSource = $is
    # Адрес ноды сохраняется, только если его задали явно (иначе он определяется заново: DHCP может его сменить).
    $script:NodeIp = Pick $NodeIp 'NodeIp'
    $script:NodeIpExplicit = [bool]$NodeIp
    $script:AllowFromExplicit = [bool]$AllowFrom
    $script:AllowFrom = (Pick $AllowFrom 'AllowFrom').Replace(' ', '')
    $script:CaSha256 = ((Pick $CaSha256 'CaSha256') -replace '[:\s]', '').ToLowerInvariant()
    if ($CaSha256 -and $script:CaSha256 -notmatch '^[0-9a-f]{64}$') { Fail 'Недопустимый -CaSha256: нужны 64 шестнадцатеричные цифры' 'Отпечаток SHA-256 корня CA главного: страница «Подключение ноды» портала.' }
    $px = Pick $Proxy 'Proxy'
    $pm = Get-Prop $answers 'ProxyMachine'
    if (-not $Proxy -and $pm) { try { $px = Unprotect-Secret $pm } catch { Warn 'прокси с паролем из файла ответов не расшифровывается: передайте -Proxy' } }
    $script:Proxy = $px
    if ($Proxy -and -not (Test-ProxyUrl $Proxy)) { Fail "Недопустимый -Proxy: $(Get-MaskedUrl $Proxy)" 'Пример: -Proxy http://proxy.nsu.ru:3128 (схема http или https, без пробелов и кавычек).' }
    $pb = Pick $PowerButton 'PowerButton'
    if ($pb -notin @('', 'nothing', 'keep')) { Fail "Неизвестное значение PowerButton=$pb в $AnswersIn" 'Укажите -PowerButton nothing или -PowerButton keep.' }
    $script:PowerButton = $pb
    $script:MemoryGB = [int](Pick $(if ($MemoryGB -gt 0) { "$MemoryGB" } else { '' }) 'MemoryGB' '0')
    if (-not $InsecureTls -and (Get-Prop $answers 'InsecureTls')) { $script:InsecureTls = [switch]$true }
    if (-not $Token) { $script:Token = $(if ($env:CLUSTER_TOKEN) { $env:CLUSTER_TOKEN } else { Get-SavedToken }) }
    if (-not $TaskPassword -and $env:NSU_LLM_TASK_PASSWORD) { $script:TaskPassword = $env:NSU_LLM_TASK_PASSWORD }
    if ($Name -cnotmatch '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$') { Fail "Недопустимое имя ноды: $Name" 'Укажите -Name из латинских букв, цифр и дефиса (до 63 символов).' }
}

# ================================================================== -Status
function Show-Status {
    $script:bad = 0
    function Ok([string]$m) { Write-Host "[ok] $m" }
    function Info([string]$m) { Write-Host "[--] $m" }
    function Bad([string]$m, [string]$fix = '') { Write-Host "[!!] $m" -ForegroundColor Yellow; if ($fix) { Write-Host "     исправить: $fix" }; $script:bad++ }
    $installer = "powershell -ExecutionPolicy Bypass -File $(Join-Path $ScriptsDir 'install-node.ps1')"
    if (-not (Test-Path (Join-Path $ScriptsDir 'install-node.ps1'))) { $installer = "powershell -ExecutionPolicy Bypass -File $Self" }
    $m = $(if ($Mode) { $Mode } else { 'k3s' })
    $desk = $Docker -eq 'desktop'
    Write-Host "Нода $Name, главный $(if ($Main) { $Main } else { '(не задан)' }), режим $m, $(if ($desk) { 'Docker Desktop' } else { "дистрибутив $Distro" })"

    $v = Get-WslVersion
    if (-not $v) { Bad 'WSL не установлен' $installer } elseif ($v -lt $MinWsl) { Bad "WSL $v старее $MinWsl" 'wsl --update' } else { Ok "WSL $v" }

    $cfg = Read-TextFile $WslConfigPath
    $nm = Get-IniValue $cfg 'wsl2' 'networkingMode'; $mem = Get-IniValue $cfg 'wsl2' 'memory'
    if ($desk) { Info ".wslconfig: networkingMode=$(if ($nm) { $nm } else { 'nat' }), memory=$(if ($mem) { $mem } else { 'по умолчанию' }) (Docker Desktop: скрипт его не меняет)" }
    elseif ($nm -ine 'mirrored') { Bad "$WslConfigPath`: networkingMode=$(if ($nm) { $nm } else { 'nat' }), нужен mirrored" $installer }
    else { Ok ".wslconfig: networkingMode=mirrored, memory=$(if ($mem) { $mem } else { 'по умолчанию' })" }

    $gpu = Get-WinGpu
    if ($gpu) {
        $line = "GPU: $($gpu.Name), драйвер $($gpu.Driver), $($gpu.VramMib) МиБ"
        if ($gpu.DriverMajor -lt $MinDriver) { Bad "$line — драйвер старее $MinDriver, GPU-нода невозможна" $DriverFix } else { Ok $line }
    }
    else { Info 'GPU NVIDIA не найден (nvidia-smi): нода работает как cpu' }

    $running = $false
    if ($desk) {
        $logonQ = Get-LoggedOnUsers; $logonKnown = ($null -ne $logonQ); $logon = @($logonQ)
        if (Test-WinDocker) {
            Ok 'Docker Desktop работает'
            $c = Invoke-Read @('docker', 'ps', '-a', '--filter', 'name=llm-node', '--format', '{{.Status}}')
            $cs = (($c.Out -split "`r?`n") | Select-Object -First 1)
            if ($cs -match '^Up') { Ok "контейнер llm-node: $cs" }
            elseif (-not $cs) { Bad 'контейнер llm-node не создан' $installer }
            else { Bad "контейнер llm-node: $cs" 'docker logs --tail 50 llm-node' }
            $imgs = (Invoke-Read @('docker', 'images', 'nsu-llm-node', '--format', '{{.Repository}}:{{.Tag}}')).Out -split "`r?`n" | Where-Object { $_ }
            Info "образы: $(if ($imgs) { $imgs -join ', ' } else { 'нет' })"
        }
        else { Bad "Docker Desktop не запущен (docker info завершился ошибкой)$(if ($logonKnown -and -not $logon) { '; в Windows никто не вошёл' })" $DesktopFix }
        Info 'автозапуск: Docker Desktop, а с ним и нода, стартует только после входа пользователя в Windows (на Windows 10 ПК класса держит ноду, пока кто-то вошёл; автовход — netplwiz или Winlogon AutoAdminLogon — решает администратор); без входа: -Docker wsl (Windows 11 22H2+)'
        if ($logon) { Info "вошёл в Windows: $($logon -join ', ')" }
    }
    elseif ($v) {
        $all = Get-Distros
        if ($all -notcontains $Distro) { Bad "Дистрибутив $Distro не установлен" $installer }
        elseif ((Get-Distros -Running) -notcontains $Distro) { Bad "Дистрибутив $Distro не запущен" "Start-ScheduledTask -TaskPath '$TaskPath' -TaskName $TaskName" }
        else { $running = $true; Ok "Дистрибутив $Distro запущен" }
    }
    if ($running) {
        $d = Invoke-Read (Get-WslRoot @('systemctl', 'is-active', 'docker'))
        if ($d.Out -match '^active') { Ok 'docker в WSL работает' } else { Bad "docker в WSL: $($d.Out)" "wsl -d $Distro -u root -e journalctl -u docker -n 50" }
        $c = Invoke-Read (Get-WslRoot @('docker', 'ps', '-a', '--filter', 'name=llm-node', '--format', '{{.Status}}'))
        $cs = (($c.Out -split "`r?`n") | Select-Object -First 1)
        if ($cs -match '^Up') { Ok "контейнер llm-node: $cs" }
        elseif (-not $cs) { Bad 'контейнер llm-node не создан' $installer }
        else { Bad "контейнер llm-node: $cs" "wsl -d $Distro -u root -e docker logs --tail 50 llm-node" }
        $imgs = (Invoke-Read (Get-WslRoot @('docker', 'images', 'nsu-llm-node', '--format', '{{.Repository}}:{{.Tag}}'))).Out -split "`r?`n" | Where-Object { $_ }
        Info "образы: $(if ($imgs) { $imgs -join ', ' } else { 'нет' })"
        if ($gpu) {
            if (Test-WslGpu) { Ok 'GPU виден в WSL (/usr/lib/wsl/lib/nvidia-smi)' } else { Bad 'GPU не виден в WSL' 'обновите драйвер NVIDIA (570+) и выполните wsl --update, затем wsl --shutdown' }
        }
    }

    $t = Get-BootTask
    if ($desk) { }
    elseif (-not $t) { Bad "Задача $TaskName не найдена: после перезагрузки нода не поднимется" $installer }
    else {
        $i = $null; try { $i = Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $TaskName } catch { }
        $last = $(if ($i) { "последний запуск $($i.LastRunTime), результат 0x{0:X}" -f $i.LastTaskResult } else { '' })
        if ("$($t.State)" -eq 'Running') { Ok "Задача $TaskName выполняется ($($t.Principal.UserId)); $last" }
        else { Bad "Задача $TaskName в состоянии $($t.State); $last" "Start-ScheduledTask -TaskPath '$TaskPath' -TaskName $TaskName" }
    }

    # Права wsl\ (диск дистрибутива с токеном): без Users, Everyone и CREATOR OWNER; запись ВМ Hyper-V — от WSL.
    $wd = Join-Path $InstallDir 'wsl'
    if ([IO.Directory]::Exists($wd)) {
        try {
            $wa = [IO.Directory]::GetAccessControl($wd)
            $rules = @($wa.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
                    $n = $_.IdentityReference.Value; try { $n = $_.IdentityReference.Translate([Security.Principal.NTAccount]).Value } catch { }
                    "$n`:$($_.FileSystemRights)$(if ($_.IsInherited) { ' (унасл.)' })" })
            $line = "$wd`: владелец $(Get-SdOwner $wd), защищён от наследования: $($wa.AreAccessRulesProtected); $($rules -join '; ')"
            $open = @($wa.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $SdStripSids -contains $_.IdentityReference.Value })
            if ($open.Count) { Bad "$line — диск дистрибутива доступен не только администраторам" $installer } else { Ok $line }
        }
        catch { Bad "права $wd не читаются: $($_.Exception.Message)" 'запустите -Status от имени администратора' }
    }
    $it = Get-InstallTaskRunning
    $act = ''; try { $act = ([IO.File]::ReadAllText($InstallActionFile)).Trim() } catch { }
    $doneFile = Join-Path $RunDir 'run.done'
    if ($it) {
        Info "идёт $(if ($act -eq 'uninstall') { 'удаление' } else { 'установка' }) в разовой задаче $InstallTaskName (журнал $InstallLog); повторный запуск install-node.ps1 покажет ход"
        if (Test-Path -LiteralPath $InstallLog) { Get-Content -LiteralPath $InstallLog -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host "     $_" } }
    }
    elseif (Test-Path -LiteralPath $doneFile) {
        $dc = ''; try { $dc = ([IO.File]::ReadAllText($doneFile)).Trim() } catch { }
        if ($dc -in @('0', '3010')) { Info "последний запуск в разовой задаче $InstallTaskName завершился успешно (код $dc), журнал $InstallLog" }
        else { Bad "последний запуск в разовой задаче $InstallTaskName завершился с кодом $dc" "прочитайте журнал $InstallLog и повторите установку" }
    }
    foreach ($rn in @($FwRule) + @($(if ($m -ne 'standalone') { $FwRuleRpc }))) {
        $hv = $null; try { $hv = Get-NetFirewallHyperVRule -Name $rn -ErrorAction Stop } catch { }
        if (-not (Get-Command Get-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) { Ok "Hyper-V firewall в этой Windows нет (Windows 10): порты закрывает брандмауэр Windows" }
        elseif ($hv) { Ok "Hyper-V firewall $rn`: порты $($hv.LocalPorts -join ','), с адресов $($hv.RemoteAddresses -join ',')" }
        else { Bad "Нет правила Hyper-V firewall $rn (входящие в WSL закрыты)" $installer }
        $wr = Get-NetFirewallRule -DisplayName $rn -ErrorAction SilentlyContinue
        if ($wr) { Ok "Брандмауэр Windows $rn`: с адресов $((@($wr | Get-NetFirewallAddressFilter | ForEach-Object { $_.RemoteAddress }) | Select-Object -Unique) -join ',')" }
        else { Bad "Нет правила брандмауэра Windows $rn" $installer }
    }
    if ($desk) {
        # Docker Desktop разрешает com.docker.backend.exe на любые порты; запрет сильнее разрешения
        $blk = Get-NetFirewallRule -DisplayName "$FwRule-Block" -ErrorAction SilentlyContinue
        if ($blk) { Ok "Брандмауэр Windows $FwRule-Block`: запрет 18080 для всех, кроме главного (правило Docker Desktop на любые порты его не перекрывает)" }
        else { Bad "Нет запрещающего правила $FwRule-Block: правило Docker Desktop на любые порты пропускает 18080 и 18050 всем" $installer }
    }
    elseif (Get-Command Get-NetFirewallHyperVVMSetting -ErrorAction SilentlyContinue) {
        $vm = $null; try { $vm = Get-NetFirewallHyperVVMSetting -Name $WslCreator -ErrorAction Stop } catch { }
        if ($vm -and "$($vm.DefaultInboundAction)" -eq 'Allow') { Bad 'Hyper-V firewall для WSL разрешает входящие по умолчанию: все правила бесполезны, порты 18080 и 18050 открыты всем' "Set-NetFirewallHyperVVMSetting -Name '$WslCreator' -DefaultInboundAction Block (от администратора; run-node.ps1 -SyncFirewall делает это сам)" }
        elseif ($vm) { Ok "Hyper-V firewall для WSL: входящие по умолчанию $($vm.DefaultInboundAction)" }
    }
    if ($desk -and $m -ne 'standalone') {
        if (Get-SyncTask) { Ok "задача $SyncTaskName есть (правила для 18050 следуют за группой split)" }
        else { Bad "нет задачи ${SyncTaskName}: правила для 18050 не обновляются при смене группы split" $installer }
    }

    $sip = $(if ($NodeIp) { $NodeIp } elseif ($Main) { Get-LocalIp (Resolve-MainIp $Main) } else { '' })
    $w = Find-WakeNic $sip
    if ($w.Nic) {
        $ws = Get-NicWakeState $w.Nic
        $line = "Wake-on-LAN: $($w.Nic.Name) ($($w.Nic.Description)), MAC $($w.Nic.Mac), $($w.Nic.Ip)/$($w.Nic.Prefix)"
        if ($ws.PmWake -ne 'Enabled') { Bad "$line — Wake on Magic Packet выключен ($(if ($ws.PmWake) { $ws.PmWake } else { 'не читается' }))" $installer }
        elseif (-not $ws.Armed) { Bad "$line — карте не разрешено будить ПК" "powercfg /deviceenablewake `"$($w.Nic.Description)`"" }
        else { Ok $line }
    }
    else { Bad "Wake-on-LAN: $($w.Why)" 'нужна проводная сетевая карта с адресом ноды (-NodeIp)' }
    $hbNow = Get-Hiberboot
    if ($hbNow -eq 0) { Ok 'быстрый запуск Windows выключен' } else { Bad 'быстрый запуск Windows включён: выключенный ПК не проснётся по сети' $installer }
    $p = Get-PowerIndex 'STANDBYIDLE'
    if ($p -and $p.Ac -eq 0) { Ok 'сон от сети выключен' } elseif ($p) { Bad "сон от сети через $([int]($p.Ac / 60)) мин" 'powercfg /change standby-timeout-ac 0' }

    if ($Main) { foreach ($x in (Get-MainProbe $Main $m)) { if ($x.Ok) { Ok $x.Text } else { Bad $x.Text $x.Fix } } }
    if ($Proxy) { Info "прокси: $(Get-MaskedUrl $Proxy)" }
    if (Test-Path $BootLog) { Info 'журнал автозапуска:'; Get-Content -Path $BootLog -Tail 3 | ForEach-Object { Write-Host "     $_" } }
    if ($script:bad -eq 0) { Write-Host 'Всё в порядке.' } else { Write-Host "Проблем: $($script:bad)." }
}

# ================================================================== -Uninstall
function Invoke-Uninstall {
    $runNode = Join-Path $PSScriptRoot 'run-node.ps1'
    $created = [bool](Get-Prop $state 'CreatedDistro')
    # .wslconfig, дистрибутив и задача принадлежат учётной записи, от которой ставили: другой учётной записью не удаляем.
    $owner = [string](Get-Prop $state 'InstallUser'); $ownerSid = [string](Get-Prop $state 'InstallUserSid')
    if (-not $owner) { $bt = Get-BootTask; if ($bt -and $bt.Principal.UserId) { $owner = [string]$bt.Principal.UserId } }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent()
    $mine = $(if ($ownerSid) { $ownerSid -eq $me.User.Value } elseif ($owner) { $me.Name -eq $owner -or $me.Name -like "*\$owner" } else { $true })
    if (-not $mine) {
        Fail "Нода установлена от имени $owner, а -Uninstall запущен от $($me.Name): .wslconfig, дистрибутив $Distro и задача автозапуска принадлежат $owner" "Запустите -Uninstall от имени $owner (по ssh или с -ViaTask -TaskPassword под этой учётной записью)."
    }
    if ($Docker -ne 'desktop' -and -not $FromTask -and ($ViaTask -or -not (Test-UserCredentials))) {
        if (-not $ViaTask) { Warn "сеанс без учётных данных пользователя (вход по ssh-ключу или WinRM): удаление выполнит одноразовая задача $InstallTaskName" }
        if (-not $TaskPassword) { Fail "Нужен пароль учётной записи $($me.Name) для одноразовой задачи удаления" 'Передайте -TaskPassword <пароль> или переменную NSU_LLM_TASK_PASSWORD.' }
        $ta = [ordered]@{ Uninstall = $true; NonInteractive = $true; Yes = $true; FromTask = $true; InstallDir = $InstallDir; Name = $Name }
        if ($KeepDistro) { $ta.KeepDistro = $true }
        if ($Main) { $ta.Main = $Main }
        # The token given now (-Token, CLUSTER_TOKEN) goes along: the task would otherwise read the one saved at install time, and
        # that one is stale once the main was reinstalled or its agent token changed (the DELETE answered 401, the node object stayed).
        # The file with the task's parameters is DPAPI LocalMachine, Administrators and SYSTEM only, like the TaskPassword in it.
        if ($Token) { $ta.Token = $Token }
        return (Invoke-InstallTask $ta -Removing)
    }
    $wslVer = Get-WslVersion
    $distros = $(if ($wslVer) { Get-Distros } else { @() })
    $hasDistro = $distros -contains $Distro
    # Диск дистрибутива, созданного скриптом, лежит в $InstallDir\wsl. Если WSL его не показывает (или не отвечает), удалять
    # каталог нельзя: дистрибутив может оставаться зарегистрированным (другой пользователь, сбой wsl.exe).
    $disk = @(Get-ChildItem -LiteralPath (Join-Path $InstallDir 'wsl') -Filter '*.vhdx' -File -Force -ErrorAction SilentlyContinue)
    # Каталог недоверенный: state.json не читался, и «создан ли дистрибутив скриптом» неизвестно. Диск на месте — тоже стоп.
    if ($disk.Count -gt 0 -and -not $hasDistro -and (($Docker -ne 'desktop' -and $created) -or -not $script:DirTrusted)) {
        Fail "WSL $(if ($wslVer) { "не показывает дистрибутив $Distro" } else { 'не отвечает' }), а его диск $($disk[0].FullName) на месте: диск дистрибутива $Distro не удаляется" "Проверьте wsl -l -v от имени $(if ($owner) { $owner } else { $me.Name }). Если дистрибутив действительно удалён, удалите диск вручную: Remove-Item -Recurse -Force '$(Join-Path $InstallDir 'wsl')', затем повторите -Uninstall."
    }
    $desk = $Docker -eq 'desktop'
    $remove = $created -and $hasDistro -and -not $KeepDistro
    if ($remove -and -not $NonInteractive -and -not $Yes -and -not $DryRun) {
        $a = Read-Answer "Удалить дистрибутив $Distro вместе с моделями и образами? (д/н)" 'д'
        if ($a -notmatch '^(д|да|y|yes)$') { $remove = $false }
    }
    Say 'Будет сделано:'
    if ($desk) { Say "  - остановить и удалить ноду $Name в Docker Desktop (run-node.ps1 -Uninstall)" }
    elseif ($hasDistro) { Say "  - остановить и удалить ноду $Name (run-node.ps1 -Uninstall -Wsl $Distro)" }
    Say "  - удалить задачи $TaskName и $SyncTaskName, правила брандмауэра $FwRule и $FwRuleRpc, вернуть электропитание, Wake-on-LAN, быстрый запуск, кнопку питания и .wslconfig"
    if ($remove) { Say "  - удалить дистрибутив $Distro (wsl --unregister)" }
    Say "  - удалить $InstallDir$(if ($hasDistro -and -not $remove) { ' (кроме диска оставленного дистрибутива)' })"
    if (-not $NonInteractive -and -not $Yes -and -not $DryRun) {
        if ((Read-Answer 'Продолжить? (д/н)' 'д') -notmatch '^(д|да|y|yes)$') { Say 'Отменено.'; return 1 }
    }

    if ($desk -or $hasDistro) {
        $p = @{ Uninstall = $true; Main = $(if ($Main) { $Main } else { 'localhost' }); Name = $Name }
        if (-not $desk) { $p.Wsl = $Distro }
        if ($Token -and $Main) { $p.Token = $Token }
        if ($InsecureTls) { $p.InsecureTls = $true }
        if ($DryRun) { $p.DryRun = $true }
        try { & $runNode @p | Out-Host } catch { Warn "run-node.ps1 -Uninstall: $($_.Exception.Message)" }
        # Docker Desktop остаётся на машине: образы ноды (6 ГБ и больше) удаляются отдельно.
        if ($desk -and (Test-WinDocker)) {
            foreach ($img in @((Get-ImageName 'nvidia'), (Get-ImageName 'cpu'))) {
                if (Test-WinImage $img) { Invoke-Change @('docker', 'rmi', $img) "docker rmi $img" -AllowFail | Out-Null }
            }
        }
    }
    if (-not ($Token -and $Main)) { Warn "без токена и адреса главного нода не удалена из кластера. Если нода ещё значится на странице «Ноды» портала, удалите её там (меню строки, «Удалить из кластера…») или на главном выполните docker exec llm-main k3s kubectl delete node $Name" }

    if ($DryRun) { Show-Dry "Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName; Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:`$false" }
    elseif (Get-BootTask) { Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false }

    if ($DryRun) { Show-Dry "Stop-ScheduledTask -TaskPath $TaskPath -TaskName $SyncTaskName; Unregister-ScheduledTask -TaskName $SyncTaskName -TaskPath $TaskPath -Confirm:`$false" }
    elseif (Get-SyncTask) { Stop-ScheduledTask -TaskPath $TaskPath -TaskName $SyncTaskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $SyncTaskName -TaskPath $TaskPath -Confirm:$false }

    foreach ($rn in @($FwRule, $FwRuleRpc, "$FwRule-Block", "$FwRuleRpc-Block")) {
        if ($DryRun) { Show-Dry "Remove-NetFirewallHyperVRule -Name $rn; Remove-NetFirewallRule -DisplayName $rn" }
        else {
            try { Remove-NetFirewallHyperVRule -Name $rn -ErrorAction Stop } catch { }
            Remove-NetFirewallRule -DisplayName $rn -ErrorAction SilentlyContinue
        }
    }

    $pw = Get-Prop $state 'Power'
    if ($pw) {
        foreach ($s in @(@('STANDBYIDLE', 'StandbyAc', 'StandbyDc'), @('HIBERNATEIDLE', 'HibernateAc', 'HibernateDc'))) {
            # Пустое или нечисловое значение из state.json в powercfg не передаётся.
            $ac = [string](Get-Prop $pw $s[1]); $dc = [string](Get-Prop $pw $s[2])
            if ($ac -match '^\d+$') { Invoke-Change @('powercfg', '/setacvalueindex', 'SCHEME_CURRENT', 'SUB_SLEEP', $s[0], $ac) 'powercfg' -AllowFail | Out-Null }
            if ($dc -match '^\d+$') { Invoke-Change @('powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', 'SUB_SLEEP', $s[0], $dc) 'powercfg' -AllowFail | Out-Null }
        }
        Invoke-Change @('powercfg', '/setactive', 'SCHEME_CURRENT') 'powercfg' -AllowFail | Out-Null
        if ((Get-Prop $pw 'HibernateEnabled') -eq 1) { Invoke-Change @('powercfg', '/hibernate', 'on') 'powercfg' -AllowFail | Out-Null }
    }
    # Wake-on-LAN, быстрый запуск и кнопка питания: как было до установки (сохранено в state.json перед изменением).
    $wk = Get-Prop $state 'Wake'
    if ($wk -and (Get-Prop $wk 'Nic')) {
        $pm = [string](Get-Prop $wk 'PmWake')
        Set-NicWake ([string]$wk.Nic) $(if ($pm -in @('Enabled', 'Disabled')) { $pm } else { '' }) (Get-Prop $wk 'MagicAdv') (Get-Prop $wk 'S5')
        $desc = [string](Get-Prop $wk 'Description')
        if ($desc -and (Get-Prop $wk 'Armed') -eq $false) { Invoke-Change @('powercfg', '/devicedisablewake', $desc) 'powercfg /devicedisablewake' -AllowFail | Out-Null }
    }
    $hbOld = [string](Get-Prop $state 'Hiberboot')
    if ($hbOld -match '^\d+$') { Invoke-CmdletChange "Set-ItemProperty '$HiberbootKey' -Name HiberbootEnabled -Value $hbOld" { Set-ItemProperty -Path $HiberbootKey -Name HiberbootEnabled -Value ([int]$hbOld) -Type DWord -ErrorAction Stop } }
    $pbOld = Get-Prop $state 'PowerButton'
    if ($pbOld) {
        $ac = [string](Get-Prop $pbOld 'Ac'); $dc = [string](Get-Prop $pbOld 'Dc')
        if ($ac -match '^\d+$') { Invoke-Change @('powercfg', '/setacvalueindex', 'SCHEME_CURRENT', 'SUB_BUTTONS', 'PBUTTONACTION', $ac) 'powercfg' -AllowFail | Out-Null }
        if ($dc -match '^\d+$') { Invoke-Change @('powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', 'SUB_BUTTONS', 'PBUTTONACTION', $dc) 'powercfg' -AllowFail | Out-Null }
        Invoke-Change @('powercfg', '/setactive', 'SCHEME_CURRENT') 'powercfg' -AllowFail | Out-Null
    }

    $had = Get-Prop $state 'WslConfigExisted'
    if ($null -ne $had) {
        if ($had) {
            if ($DryRun) { Show-Dry "восстановить $WslConfigPath из резервной копии" }
            else { [IO.File]::WriteAllText($WslConfigPath, [string](Get-Prop $state 'WslConfigOriginal')) }
        }
        elseif ($DryRun) { Show-Dry "Remove-Item $WslConfigPath" }
        else { Remove-Item -Force -Path $WslConfigPath -ErrorAction SilentlyContinue }
        Say 'Изменения .wslconfig вступят в силу после wsl --shutdown или перезагрузки.'
    }

    if ($remove) { Invoke-Change @('wsl', '--unregister', $Distro) "Не удалось удалить дистрибутив $Distro" -AllowFail | Out-Null }
    elseif ($hasDistro) { Say "Дистрибутив $Distro оставлен (создан не этим скриптом или -KeepDistro). Удалить: wsl --unregister $Distro" }

    # Оставленный дистрибутив, поставленный скриптом, лежит в $InstallDir\wsl: его диск не удаляется.
    $keepWsl = $hasDistro -and -not $remove
    $keepNames = @($(if ($keepWsl) { 'wsl' }), $(if ($FromTask) { 'run' })) | Where-Object { $_ }
    if ($keepNames) {
        if ($DryRun) { Show-Dry "удалить содержимое $InstallDir, кроме $($keepNames -join ', ')$(if ($keepWsl) { " (диск дистрибутива $Distro)" })" }
        elseif (Test-Path $InstallDir) { Get-ChildItem -Force -Path $InstallDir | Where-Object { $keepNames -notcontains $_.Name } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
    }
    elseif ($DryRun) { Show-Dry "Remove-Item -Recurse -Force $InstallDir" }
    elseif (Test-Path $InstallDir) { Remove-Item -Recurse -Force -Path $InstallDir -ErrorAction SilentlyContinue }
    Say 'Сам WSL не удаляется (им могут пользоваться другие программы). Удалить: wsl --uninstall'
    if (-not $DryRun) { Say "Нода $Name удалена с этой машины." }
    return 0
}

# ================================================================== установка
function Invoke-Install {
    $isAdmin = Test-Admin
    if (-not $isAdmin -and -not $DryRun) { Fail 'Нужны права администратора' 'Запустите PowerShell от имени администратора и повторите команду.' }
    if ([Security.Principal.WindowsIdentity]::GetCurrent().IsSystem) {
        Fail 'Скрипт запущен от SYSTEM: дистрибутивы WSL принадлежат пользователю, от SYSTEM они не работают' 'Запустите от учётной записи администратора, под которой нода будет работать (её пароль сохранит планировщик).'
    }
    $build = [Environment]::OSVersion.Version.Build

    # --- что уже есть на машине
    $wslVer = Get-WslVersion
    $distros = $(if ($wslVer) { Get-Distros } else { @() })
    $hasDistro = $distros -contains $Distro
    $hasDesktop = Test-DockerDesktop $distros
    $gpu = Get-WinGpu
    $ram = Get-TotalRamGB
    $cpus = [Environment]::ProcessorCount
    Say "Обнаружено: Windows $build, ОЗУ $ram ГБ, ЦП $cpus, WSL $(if ($wslVer) { $wslVer } else { 'не установлен' }), дистрибутив $Distro $(if ($hasDistro) { 'есть' } else { 'нет' }), Docker Desktop $(if ($hasDesktop) { 'есть' } else { 'нет' })"
    Say "GPU: $(if ($gpu) { "$($gpu.Name), драйвер $($gpu.Driver), $($gpu.VramMib) МиБ" } else { 'NVIDIA не найден' })"
    if (-not $isAdmin) { Warn 'нет прав администратора: -DryRun только печатает действия' }

    # --- вопросы (только о недостающем)
    $ask = -not $NonInteractive
    if (-not $Main -and $ask) { $script:Main = Read-Answer 'Адрес главного сервера (IP или имя)' }
    if (-not $Main) { Fail 'Не задан адрес главного сервера (-Main)' "Пример: .\install-node.ps1 -Main 10.0.0.5 -Token <токен кластера>   (токен: портал, «Подключение ноды»)" }
    if (-not $Token -and $ask) { $script:Token = Read-Answer 'Токен кластера (портал, «Подключение ноды»; ввод скрыт)' -Secret }
    if (-not $Token) { Fail 'Не задан токен кластера (-Token или переменная CLUSTER_TOKEN)' 'Скопируйте токен на странице «Подключение ноды» портала и передайте его через -Token.' }
    if (-not $Mode) { $script:Mode = $(if ($ask) { Read-Answer 'Режим ноды: k3s или standalone' 'k3s' } else { 'k3s' }) }
    if ($Mode -notin @('k3s', 'standalone')) { Fail "Неизвестный режим $Mode" 'Укажите -Mode k3s или -Mode standalone.' }

    # --- Docker Desktop уже стоит: оставить его (запасной путь) или Docker CE в своём дистрибутиве WSL (основной)
    $dk = $Docker
    if ($dk -eq 'auto') {
        if (-not $hasDesktop) { $dk = 'wsl' }
        elseif ($ask) {
            Say 'Найден Docker Desktop. Где запускать ноду:'
            Say '  desktop — в Docker Desktop (запасной путь): WSL и .wslconfig не меняются, нода поднимается только после входа пользователя в Windows;'
            Say "  wsl     — Docker CE в отдельном дистрибутиве $Distro (основной путь): автозапуск без входа, mirrored-сеть; wsl --shutdown остановит и Docker Desktop."
            $dk = (Read-Answer 'Вариант: desktop или wsl' 'desktop').ToLowerInvariant()
        }
        else { Warn "найден Docker Desktop: с -NonInteractive нода ставится в него (запасной путь, автозапуск только после входа пользователя: на Windows 10 ПК класса должен входить в Windows, автовход решает администратор). Основной путь: -Docker wsl"; $dk = 'desktop' }
    }
    if ($dk -notin @('wsl', 'desktop')) { Fail "Неизвестный вариант Docker: $dk" 'Ответьте desktop или wsl (параметр -Docker desktop|wsl).' }
    if ($dk -eq 'desktop' -and -not $hasDesktop) { Fail 'Docker Desktop не найден' 'Установите Docker Desktop или выберите основной путь: -Docker wsl.' }
    $script:DockerFinal = $dk
    $desk = $dk -eq 'desktop'
    if (-not $desk -and $build -lt 22621) { Fail "Windows сборки ${build}: режим networkingMode=mirrored требует Windows 11 22H2 (22621) или новее" 'Обновите Windows или используйте запасной путь с Docker Desktop: -Docker desktop (docs\deploy-class.md).' }
    if (-not $desk -and $hasDesktop) { Warn "Docker Desktop останется установленным, но нода будет в $Distro. Запись .wslconfig и wsl --shutdown остановят его движок; выключите его автозапуск, чтобы два Docker не делили память WSL." }
    # Одноразовая задача со входом по паролю выполняет шаги вместо этого сеанса:
    # - обязательно: -ViaTask, и для WSL сеанс без учётных данных (ssh по ключу, WinRM): WSL и DPAPI в нём не работают;
    # - из сеанса ssh/WinRM, если пароль есть (-TaskPassword, NSU_LLM_TASK_PASSWORD): обрыв связи не должен убить установку,
    #   на WSL и на Docker Desktop. Без пароля такая установка идёт в самом сеансе, о чём предупреждает ниже.
    $noCredentials = (-not $desk) -and -not (Test-UserCredentials)
    $remote = (-not $FromTask) -and (Test-RemoteSession)
    $script:UseTask = (-not $FromTask) -and ($ViaTask -or $noCredentials -or ($remote -and [bool]$TaskPassword))
    $script:TaskWhy = $(if ($ViaTask) { 'via' } elseif ($noCredentials) { 'credentials' } elseif ($script:UseTask) { 'remote' } else { '' })
    if ($remote -and -not $script:UseTask) {
        Warn 'сеанс ssh или WinRM: если связь оборвётся, Windows завершит установку на полпути. Передайте -TaskPassword (или NSU_LLM_TASK_PASSWORD): установка пойдёт разовой задачей планировщика, переживёт обрыв, а повторный запуск покажет её ход.'
    }
    $ddUp = $false
    if ($desk) {
        $ddUp = Test-WinDocker
        if (-not $ddUp) {
            if ($DryRun) { Warn "Docker Desktop не запущен (docker info завершился ошибкой). $DesktopFix" }
            else { Fail 'Docker Desktop не запущен (docker info завершился ошибкой)' $DesktopFix }
        }
    }

    $defMem = [math]::Max(4, $ram - 4)
    if (-not $desk -and $MemoryGB -le 0) { $script:MemoryGB = [int]$(if ($ask) { Read-Answer 'Память для WSL, ГБ' "$defMem" } else { $defMem }) }
    $mainIp = Resolve-MainIp $Main
    if (-not $mainIp) { Warn "адрес $Main не разрешается в IPv4" }
    if (-not $NodeIp) { $script:NodeIp = Get-LocalIp $mainIp }
    if (-not $NodeIp) { Fail "Не удалось определить адрес этой машины в сторону главного ($Main)" 'Проверьте сеть или укажите -NodeIp <LAN-адрес этой машины>.' }
    # AllowFrom по умолчанию: только главный (шлюз и пробы портала ходят только с него). Прежние версии открывали всю подсеть ноды,
    # и ученик с соседнего ПК подключался к RPC (18050) без аутентификации: такой сохранённый список без явного -AllowFrom отбрасывается.
    if ($AllowFrom -and -not $script:AllowFromExplicit -and (Test-LegacyAllow $AllowFrom $NodeIp)) {
        Warn "сохранённый AllowFrom ($AllowFrom) открывал порты всей подсети ноды: теперь 18080 открыт только главному, 18050 главному и нодам группы split. Свой список: -AllowFrom."
        $script:AllowFrom = ''
    }
    if (-not $AllowFrom -and -not $mainIp) { Fail "Адрес главного $Main не разрешается в IPv4: брандмауэру нечего разрешить" 'Укажите -AllowFrom <адрес главного> (через запятую можно несколько адресов).' }
    if (-not $AllowFrom) { $script:AllowFrom = $(if ($ask) { Read-Answer 'С каких адресов пускать на 18080 (инференс; через запятую; на 18050 пускают главного и ноды группы split)' $mainIp } else { $mainIp }).Replace(' ', '') }
    if ($AllowFrom -notmatch '^[0-9./]+(,[0-9./]+)*$') { Fail "Недопустимый список адресов: $AllowFrom" 'Пример: -AllowFrom 10.0.0.5 или 10.0.0.5,192.168.3.7' }
    if ($mainIp -and -not (Test-AddrAllowed $mainIp $AllowFrom)) { Warn "главный $mainIp не входит в -AllowFrom ($AllowFrom): шлюз не достанет до ноды" }
    foreach ($x in (Get-MainProbe $Main $Mode)) {
        if ($x.Ok) { Say "$($x.Text)." }
        else { Warn "$($x.Text). Как исправить: $($x.Fix). Установку можно продолжить." }
    }
    $script:NoProxyList = $(if ($Proxy) { Get-NoProxy $Main $mainIp $AllowFrom $NodeIp } else { '' })
    $busy = @(Get-BusyPorts (Get-FwPorts $Mode))
    if ($busy.Count -gt 0) { Warn "порты уже заняты в Windows: $($busy -join ', '). В режиме mirrored нода не сможет их слушать: остановите эти программы." }

    # --- ускоритель
    $acc = $Accel
    $accNote = ''
    if ($acc -ne 'cpu') {
        if (-not $gpu) { if ($acc -eq 'nvidia') { Fail 'GPU NVIDIA не найден (nvidia-smi)' "$DriverFix Или запустите с -Accel cpu." }; $acc = 'cpu'; $accNote = 'GPU не найден' }
        elseif ($gpu.DriverMajor -lt $MinDriver) {
            $msg = "драйвер NVIDIA $($gpu.Driver) старее ${MinDriver}: образ собран на CUDA 12.8, GPU-нода с ним не работает"
            if ($acc -eq 'nvidia') { Fail "GPU: $msg" $DriverFix }
            Warn "$msg. Как обновить: $DriverFix"
            if ($ask -and (Read-Answer 'Продолжить как CPU-нода? (д/н)' 'д') -notmatch '^(д|да|y|yes)$') { Fail "Установка остановлена: $msg" $DriverFix }
            Warn 'нода ставится как cpu; после обновления драйвера запустите установку снова, она станет GPU-нодой.'
            $acc = 'cpu'; $accNote = 'старый драйвер'
        }
        elseif (-not $desk -and $hasDistro -and -not (Test-WslGpu)) {
            if ($acc -eq 'nvidia') { Fail 'GPU не виден в WSL (/usr/lib/wsl/lib/nvidia-smi)' 'Выполните wsl --update и wsl --shutdown; проверьте драйвер NVIDIA 570+.' }
            Warn 'GPU не виден в WSL — нода будет cpu. Выполните wsl --update, wsl --shutdown и запустите установку снова.'; $acc = 'cpu'; $accNote = 'GPU не виден в WSL'
        }
        else { $acc = 'nvidia' }
    }
    $script:AccelFinal = $acc
    $image = Get-ImageName $acc

    # --- образ ноды: архив (-ImageTar или рядом со скриптом) или главный сервер (по умолчанию)
    $pullImage = $false
    if ($ImageSource -eq 'main') { $script:ImageTar = '' }
    elseif (-not $ImageTar) { $script:ImageTar = Find-ImageTar $acc }
    $haveImage = $(if ($desk) { $ddUp -and (Test-WinImage $image) } else { $hasDistro -and (Test-WslImage $image) })
    # Архив из файла ответов мог исчезнуть (class-deploy удаляет его после установки): нужен он, только если образа нет.
    if ($ImageTar -and -not (Test-Path -LiteralPath $ImageTar)) {
        $gone = $ImageTar
        $alt = Find-ImageTar $acc
        if ($haveImage) { Say "Архив образа $gone не найден, но образ $image уже загружен: архив не нужен."; $script:ImageTar = '' }
        elseif ($alt) { Warn "архив образа $gone не найден, используется $alt"; $script:ImageTar = $alt }
        else {
            $ask2 = $(if ($ask) { Read-Answer "Архив образа $gone не найден, а образа $image нет. Путь к архиву образа (nsu-llm-node-*.tar.gz; пусто: скачать образ с главного)" } else { '' })
            $fix = "Укажите -ImageTar <путь к nsu-llm-node-$(Get-NodeVersion)-$(if ($acc -eq 'nvidia') { 'gpu' } else { 'cpu' }).tar.gz> (сборка и docker save: docs\deploy-class.md, «Распространение образов»)."
            if ($ask2) { $ask2 = Get-AbsPath $ask2; $script:ImageTar = $ask2 }
            elseif ($ImageSource -ne 'tar' -and -not $script:ImageTarExplicit) { Warn "архив образа $gone не найден: образ $image будет скачан с главного (https://$Main/v2/)."; $script:ImageTar = '' }
            elseif ($script:UseTask) { Warn "архив образа $gone не найден, а образа $image этот сеанс не видит: образ проверит задача установки." }
            elseif ($DryRun) { Warn "архив образа $gone не найден, а образа $image нет: установка остановится. $fix" }
            else { Fail "Архив образа $gone не найден, а образа $image нет" $fix }
            if ($ask2 -and -not $DryRun -and -not (Test-Path -LiteralPath $ask2)) { Fail "Архив образа $ask2 не найден" $fix }
        }
    }
    # Архив называется nsu-llm-node-<версия>-cpu|gpu.tar.gz: если его версия не та, что у образа главного (NODE_IMAGE_TAG), docker load даст
    # образ с другим именем, а ноде нужен образ главного. Найденный сам или из файла ответов архив тогда не берётся, образ скачивается с главного
    # (так же, как без архива); названный явно (-ImageTar, -ImageSource tar) это ошибка с понятным текстом.
    if ($ImageTar) {
        $wantVer = $image -replace '^nsu-llm-node:(.+)-(cpu|gpu)$', '$1'
        $tarVer = $(if ((Split-Path -Leaf $ImageTar) -match '^nsu-llm-node-(.+)-(cpu|gpu)\.tar') { $matches[1] } else { '' })
        if ($tarVer -and $tarVer -ne $wantVer) {
            $mismatch = "архив $ImageTar с образом версии $tarVer, а главный ($Main) работает на версии $wantVer ($image)"
            if ($ImageSource -eq 'tar' -or $script:ImageTarExplicit) {
                Fail "Архив образа не подходит: $mismatch" "Возьмите архив nsu-llm-node-$wantVer-$(if ($acc -eq 'nvidia') { 'gpu' } else { 'cpu' }).tar.gz или уберите -ImageTar / -ImageSource tar: образ скачается с главного."
            }
            Warn "${mismatch}: архив не используется, образ $image будет скачан с главного (https://$Main/v2/)."
            $script:ImageTar = ''
        }
    }
    $loadImage = $false
    if ($ImageTar) { $loadImage = $DryRun -or -not $haveImage -or ((Get-Prop $state 'ImageTarSig') -ne (Get-FileSig $ImageTar)) }
    elseif ($ImageSource -eq 'main') { $pullImage = $true }
    elseif ($script:UseTask -and -not $haveImage) { Warn "образ $image в этом сеансе не найден (WSL без учётных данных может его не видеть): образ проверит задача установки." }
    elseif ((($desk -and $ddUp) -or (-not $desk -and $wslVer)) -and -not $haveImage) {
        if ($ImageSource -eq 'tar') {
            Fail "Образ $image не найден ни в $(if ($desk) { 'Docker Desktop' } else { 'WSL' }), ни рядом со скриптом" ("Положите nsu-llm-node-$(Get-NodeVersion)-" + $(if ($acc -eq 'nvidia') { 'gpu' } else { 'cpu' }) + '.tar.gz рядом с install-node.ps1 или укажите -ImageTar <путь> (сборка и docker save: docs\deploy-class.md, «Распространение образов»).')
        }
        $pullImage = $true   # умолчание: образ с главного сервера по локальной сети
    }

    # --- задача планировщика: пароль нужен, только если задачу надо (пере)создать
    $user = Get-CurrentUser
    $task = Get-BootTask
    $taskOk = $desk -or ($task -and ($task.Actions[0].Execute -eq 'powershell.exe') -and ($task.Actions[0].Arguments -eq (Get-BootArgs)) -and ($user -like "*\$($task.Principal.UserId)" -or $user -eq $task.Principal.UserId))
    if ($script:TaskWhy -eq 'credentials') { Warn "сеанс без учётных данных пользователя (вход по ssh-ключу или WinRM): WSL и задачу автозапуска установит одноразовая задача планировщика $InstallTaskName со входом по паролю $user" }
    elseif ($script:TaskWhy -eq 'remote') { Warn "сеанс ssh или WinRM: установка идёт разовой задачей планировщика $InstallTaskName (вход по паролю $user), чтобы обрыв связи её не прервал" }
    if ((-not $taskOk -or $script:UseTask) -and -not $TaskPassword) {
        if ($ask) { $script:TaskPassword = Read-Answer "Пароль учётной записи $user (задача автозапуска работает без входа; ввод скрыт)" -Secret }
        if (-not $TaskPassword) { Fail "Нужен пароль учётной записи $user для задачи автозапуска$(if ($script:UseTask) { ' и одноразовой задачи установки' })" 'Передайте -TaskPassword <пароль> или переменную NSU_LLM_TASK_PASSWORD (пароль не сохраняется).' }
    }

    # --- Wake-on-LAN: физическая карта с адресом ноды (не vEthernet WSL/Hyper-V, не VPN), быстрый запуск, кнопка питания
    $wol = Find-WakeNic $NodeIp
    $nic = $wol.Nic
    $wst = $(if ($nic) { Get-NicWakeState $nic } else { $null })
    if (-not $nic) { Warn "Wake-on-LAN не настраивается: $($wol.Why). Портал не сможет включить этот ПК по сети; укажите -NodeIp адресом проводной сетевой карты, если она есть." }
    elseif ($nic.Media -match '802\.11') { Warn "карта $($nic.Name) — Wi-Fi: из выключенного состояния ПК по Wi-Fi обычно не просыпается, для Wake-on-LAN подключите его кабелем." }
    if ($wst -and $wst.PmWake -eq 'Unsupported') { Warn "драйвер карты $($nic.Name) не поддерживает Wake on Magic Packet: обновите драйвер сетевой карты." }
    if (-not $PowerButton) {
        $script:PowerButton = 'nothing'
        if ($ask -and (Read-Answer 'Кнопка питания ничего не делает (ПК класса не выключат случайно)? (д/н)' 'д') -notmatch '^(д|да|y|yes)$') { $script:PowerButton = 'keep' }
    }
    $hb = Get-Hiberboot
    $wantPm = $(if ($wst -and $wst.PmWake -ne 'Enabled' -and $wst.PmWake -ne 'Unsupported') { 'Enabled' } else { '' })
    $wantAdv = $(if ($wst -and $null -ne $wst.MagicAdv -and "$($wst.MagicAdv)" -ne '1') { '1' } else { $null })
    $wantS5 = $(if ($wst -and $null -ne $wst.S5 -and "$($wst.S5)" -ne '1') { '1' } else { $null })
    $wolChange = $nic -and ($wantPm -or $wantAdv -or $wantS5 -or -not $wst.Armed)
    $pba = Get-PowerIndex 'PBUTTONACTION' 'SUB_BUTTONS'
    $pbChange = $PowerButton -eq 'nothing' -and ((-not $pba) -or $pba.Ac -ne 0 -or $pba.Dc -ne 0)

    # --- .wslconfig и электропитание
    $cfgOld = Read-TextFile $WslConfigPath
    $cfgNew = Set-IniValues $cfgOld (Get-WslConfigWant $MemoryGB)
    $cfgChange = (-not $desk) -and ($cfgOld.Replace("`r", '').TrimEnd() -ne $cfgNew.Replace("`r", '').TrimEnd())
    $stb = Get-PowerIndex 'STANDBYIDLE'; $hib = Get-PowerIndex 'HIBERNATEIDLE'; $hibOn = Get-HibernateEnabled
    $powerChange = (-not $stb) -or $stb.Ac -ne 0 -or $stb.Dc -ne 0 -or (-not $hib) -or $hib.Ac -ne 0 -or $hib.Dc -ne 0 -or $hibOn -ne 0
    $ports = Get-FwPorts $Mode

    # --- план
    $steps = New-Object System.Collections.ArrayList
    function Add-Step([string]$t, [scriptblock]$b) { [void]$steps.Add([pscustomobject]@{ Title = $t; Do = $b }) }
    $reboot = $false
    if (-not $desk -and -not $wslVer) {
        Add-Step 'установить WSL (wsl --install --no-distribution), затем перезагрузка' { Invoke-Change @('wsl', '--install', '--no-distribution') 'Не удалось установить WSL' 'Включите виртуализацию в BIOS/UEFI и компонент «Платформа виртуальной машины», затем повторите.' | Out-Null }
        $reboot = $true
    }
    else {
        if (-not $desk -and $wslVer -lt $MinWsl) { Add-Step "обновить WSL $wslVer до последней версии (wsl --update)" { Invoke-Change @('wsl', '--update', '--web-download') 'Не удалось обновить WSL' 'Проверьте доступ в интернет (github.com) и повторите; вручную: wsl --update.' | Out-Null } }
        Add-Step "скопировать скрипты в $ScriptsDir" { Copy-ScriptFiles }
        if (-not $desk -and -not $hasDistro) {
            $loc = Join-Path $InstallDir 'wsl'
            # wsl\ создаётся до wsl --install сразу с DACL «только SYSTEM и Administrators»: в ext4.vhdx лежат токен кластера и
            # пароль прокси, а без этого каталог унаследовал бы чтение для Users. Права ВМ WSL добавит сама.
            Add-Step "создать $loc (только SYSTEM и Administrators, без Users)" {
                if ($DryRun) { Show-Dry "New-SecureDir $loc -NoUsers"; return }
                Initialize-InstallDir; New-SecureDir $loc -NoUsers
            }
            if ($DistroSource) { Add-Step "установить дистрибутив $Distro из $DistroSource в $loc" { Invoke-Change @('wsl', '--install', '--from-file', $DistroSource, '--name', $Distro, '--location', $loc, '--no-launch') "Не удалось установить дистрибутив $Distro" "Проверьте файл $DistroSource (.wsl или .tar.gz с rootfs Ubuntu 24.04)." | Out-Null; Set-State 'CreatedDistro' $true; Save-State } }
            else { Add-Step "установить дистрибутив $UbuntuDistro под именем $Distro в $loc (из интернета)" { Invoke-Change @('wsl', '--install', $UbuntuDistro, '--name', $Distro, '--location', $loc, '--no-launch', '--web-download') "Не удалось установить дистрибутив $Distro" 'Нет интернета: скачайте образ Ubuntu 24.04 для WSL (.wsl) на другой машине и укажите -DistroSource <путь>.' | Out-Null; Set-State 'CreatedDistro' $true; Save-State } }
            Add-Step "проверить запуск $Distro" { Invoke-Change (Get-WslRoot @('true')) "Дистрибутив $Distro не запускается" "wsl -d $Distro -u root  (посмотрите сообщение об ошибке)" | Out-Null }
            if ($script:AccelFinal -eq 'nvidia') {
                Add-Step 'проверить GPU в WSL' {
                    if ($DryRun) { Show-Dry (Format-Cmd (Get-WslRoot @('/usr/lib/wsl/lib/nvidia-smi', '-L'))); return }
                    if (-not (Test-WslGpu)) {
                        if ($Accel -eq 'nvidia') { Fail 'GPU не виден в WSL' 'Выполните wsl --update и wsl --shutdown; проверьте драйвер NVIDIA 570+.' }
                        Warn 'GPU не виден в WSL — нода будет cpu'; $script:AccelFinal = 'cpu'
                    }
                }
            }
        }
        if ($cfgChange) {
            Add-Step "записать $WslConfigPath (memory=${MemoryGB}GB, networkingMode=mirrored, без автоостановки) и перезапустить WSL (wsl --shutdown: остановит все дистрибутивы этого пользователя)" {
                if ($null -eq (Get-Prop $state 'WslConfigExisted')) { Set-State 'WslConfigExisted' ([bool]$cfgOld); Set-State 'WslConfigOriginal' $cfgOld; Save-State }
                if ($DryRun) { Show-Dry "записать $WslConfigPath`:"; $cfgNew -split "`r`n" | Where-Object { $_ } | ForEach-Object { Write-Host "    $_" } }
                else { [IO.File]::WriteAllText($WslConfigPath, $cfgNew) }   # без BOM: WSL читает файл как есть
                Invoke-Change @('wsl', '--shutdown') 'wsl --shutdown завершился ошибкой' | Out-Null
            }
        }
        if (-not $desk) { Add-Step 'настроить дистрибутив: systemd, Docker CE, NVIDIA Container Toolkit для GPU (wsl-setup.sh)' {
            $setup = ConvertTo-WslPath (Join-Path $ScriptsDir 'wsl-setup.sh')
            # Прокси (может быть с паролем) идёт в WSL через WSLENV, а не в командной строке: её видят все пользователи.
            $prevEnv = @{ PROXY = $env:PROXY; NO_PROXY_LIST = $env:NO_PROXY_LIST; WSLENV = $env:WSLENV }
            if ($Proxy) {
                $env:PROXY = $Proxy; $env:NO_PROXY_LIST = $script:NoProxyList
                $env:WSLENV = ((@($env:WSLENV -split ':' | Where-Object { $_ }) + @('PROXY/u', 'NO_PROXY_LIST/u')) -join ':')
                if ($DryRun) { Show-Dry "окружение для wsl-setup.sh через WSLENV: PROXY=$(Get-MaskedUrl $Proxy), NO_PROXY_LIST=$($script:NoProxyList)" }
            }
            try { $r = Invoke-Change (Get-WslRoot @('env', "ACCEL=$($script:AccelFinal)", 'bash', $setup)) 'Настройка дистрибутива не удалась' "Смотрите вывод выше; повторить вручную: wsl -d $Distro -u root -e bash $setup" }
            finally { $env:PROXY = $prevEnv.PROXY; $env:NO_PROXY_LIST = $prevEnv.NO_PROXY_LIST; $env:WSLENV = $prevEnv.WSLENV }
            if ($DryRun -or $r.Out -match 'NSU_LLM_RESTART=1') {
                Invoke-Change @('wsl', '--terminate', $Distro) "wsl --terminate $Distro завершился ошибкой" | Out-Null
                Invoke-Change (Get-WslRoot @('true')) "Дистрибутив $Distro не запустился после включения systemd" | Out-Null
            }
            if ($DryRun) { Show-Dry (Format-Cmd (Get-WslRoot @('systemctl', 'is-active', 'docker'))); return }
            for ($i = 0; $i -lt 30; $i++) { if ((Invoke-Read (Get-WslRoot @('systemctl', 'is-active', 'docker'))).Out -match '^active') { return }; Start-Sleep -Seconds 2 }
            Fail 'Docker в WSL не запустился' "wsl -d $Distro -u root -e journalctl -u docker -n 50"
        } }
        if ($pullImage) {
            Add-Step "скачать образ ноды $image с главного: https://$Main/v2/ (по локальной сети; корень CA главного только для этого реестра)" {
                Invoke-ImagePull (Get-ImageName $script:AccelFinal) ([bool]$desk)
            }
        }
        if ($loadImage -and $desk) {
            Add-Step "загрузить образ ноды в Docker Desktop из $ImageTar" {
                Invoke-Change @('docker', 'load', '-i', $ImageTar) 'docker load не удался' "Проверьте файл $ImageTar (docker save | gzip) и что Docker Desktop запущен." | Out-Null
                if (-not $DryRun) {
                    Set-State 'ImageTarSig' (Get-FileSig $ImageTar); Save-State
                    $img = Get-ImageName $script:AccelFinal
                    if (-not (Test-WinImage $img)) { Fail "После загрузки $ImageTar образа $img нет" "Нужен архив nsu-llm-node-$(Get-NodeVersion)-$(if ($script:AccelFinal -eq 'nvidia') { 'gpu' } else { 'cpu' }).tar.gz; укажите его через -ImageTar." }
                }
            }
        }
        elseif ($loadImage) {
            Add-Step "загрузить образ ноды из $ImageTar" {
                $src = $ImageTar
                if ($src.StartsWith('\\')) {
                    $dst = Join-Path $InstallDir ('images\' + (Split-Path -Leaf $src))
                    if ($DryRun) { Show-Dry "Copy-Item $src $dst" } else { New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null; Copy-Item -Force -Path $src -Destination $dst }
                    $src = $dst
                }
                Invoke-Change (Get-WslRoot @('docker', 'load', '-i', (ConvertTo-WslPath $src))) 'docker load не удался' "Проверьте файл $ImageTar (docker save | gzip)." | Out-Null
                if (-not $DryRun) {
                    Set-State 'ImageTarSig' (Get-FileSig $ImageTar); Save-State
                    $img = Get-ImageName $script:AccelFinal
                    if (-not (Test-WslImage $img)) { Fail "После загрузки $ImageTar образа $img нет" "Нужен архив nsu-llm-node-$(Get-NodeVersion)-$(if ($script:AccelFinal -eq 'nvidia') { 'gpu' } else { 'cpu' }).tar.gz; укажите его через -ImageTar." }
                }
            }
        }
        if ($powerChange) {
            Add-Step 'выключить сон и гибернацию (powercfg)' {
                if ($null -eq (Get-Prop $state 'Power')) {
                    Set-State 'Power' ([pscustomobject]@{ StandbyAc = $(if ($stb) { $stb.Ac } else { 0 }); StandbyDc = $(if ($stb) { $stb.Dc } else { 0 }); HibernateAc = $(if ($hib) { $hib.Ac } else { 0 }); HibernateDc = $(if ($hib) { $hib.Dc } else { 0 }); HibernateEnabled = $hibOn })
                    Save-State
                }
                foreach ($s in @('STANDBYIDLE', 'HIBERNATEIDLE')) {
                    Invoke-Change @('powercfg', '/setacvalueindex', 'SCHEME_CURRENT', 'SUB_SLEEP', $s, '0') 'powercfg не выполнился' | Out-Null
                    Invoke-Change @('powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', 'SUB_SLEEP', $s, '0') 'powercfg не выполнился' | Out-Null
                }
                Invoke-Change @('powercfg', '/setactive', 'SCHEME_CURRENT') 'powercfg не выполнился' | Out-Null
                Invoke-Change @('powercfg', '/hibernate', 'off') 'powercfg /hibernate off не выполнился' | Out-Null
            }
        }
        if ($wolChange) {
            Add-Step "Wake-on-LAN на карте $($nic.Name): Wake on Magic Packet, право будить ПК (действует после перезагрузки)" {
                if ($null -eq (Get-Prop $state 'Wake')) {
                    Set-State 'Wake' ([pscustomobject]@{ Nic = $nic.Name; Description = $nic.Description; PmWake = $wst.PmWake; MagicAdv = $wst.MagicAdv; S5 = $wst.S5; Armed = $wst.Armed })
                    Save-State
                }
                Set-NicWake $nic.Name $wantPm $wantAdv $wantS5
                if (-not $wst.Armed) {
                    $r = Invoke-Change @('powercfg', '/deviceenablewake', $nic.Description) 'powercfg /deviceenablewake' -AllowFail
                    if ($r.Code -ne 0) { Warn "Windows не разрешил карте $($nic.Description) будить ПК (powercfg /deviceenablewake): включите это в диспетчере устройств, вкладка «Управление электропитанием»." }
                }
            }
        }
        if ($hb -ne 0) {
            Add-Step 'выключить быстрый запуск Windows (HiberbootEnabled=0): с ним «Завершение работы» — гибернация, и ПК не просыпается по сети' {
                if ($null -eq (Get-Prop $state 'Hiberboot')) { Set-State 'Hiberboot' $hb; Save-State }
                Invoke-CmdletChange "Set-ItemProperty '$HiberbootKey' -Name HiberbootEnabled -Value 0" { Set-ItemProperty -Path $HiberbootKey -Name HiberbootEnabled -Value 0 -Type DWord -ErrorAction Stop }
            }
        }
        if ($pbChange) {
            Add-Step 'кнопка питания ничего не делает (powercfg PBUTTONACTION 0): ПК класса не выключат случайно' {
                if ($null -eq (Get-Prop $state 'PowerButton')) { Set-State 'PowerButton' ([pscustomobject]@{ Ac = $(if ($pba) { $pba.Ac } else { $null }); Dc = $(if ($pba) { $pba.Dc } else { $null }) }); Save-State }
                Invoke-Change @('powercfg', '/setacvalueindex', 'SCHEME_CURRENT', 'SUB_BUTTONS', 'PBUTTONACTION', '0') 'powercfg не выполнился' | Out-Null
                Invoke-Change @('powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', 'SUB_BUTTONS', 'PBUTTONACTION', '0') 'powercfg не выполнился' | Out-Null
                Invoke-Change @('powercfg', '/setactive', 'SCHEME_CURRENT') 'powercfg не выполнился' | Out-Null
            }
        }
        # Both firewalls (Windows Firewall and, on Windows 11 22H2+, Hyper-V firewall for WSL) are set by run-node.ps1 at the end of the
        # install and kept in step with the split group afterwards (node-boot.ps1 for WSL; for Docker Desktop, which has no boot task,
        # a SYSTEM task NSU-LLM-Node-FwSync once a minute). Windows 10 has no Hyper-V firewall: only the Windows rule guards the ports.
        if ($desk -and $Mode -eq 'k3s') {
            Add-Step "задача планировщика $SyncTaskName`: раз в минуту run-node.ps1 -SyncFirewall от SYSTEM (правила для 18050 следуют за группой split)" {
                $rn = Join-Path $ScriptsDir 'run-node.ps1'
                $syncArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$rn`" -SyncFirewall"
                if ($DryRun) {
                    Show-Dry "Register-ScheduledTask -TaskName $SyncTaskName -TaskPath $TaskPath -Action (powershell.exe $syncArgs) -Trigger (каждую минуту) -User SYSTEM -RunLevel Highest"
                    return
                }
                $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $syncArgs
                $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
                $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
                if (Get-SyncTask) { Unregister-ScheduledTask -TaskName $SyncTaskName -TaskPath $TaskPath -Confirm:$false }
                Register-ScheduledTask -TaskName $SyncTaskName -TaskPath $TaskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
                    -Description 'NSU LLM: правила брандмауэра для RPC split следуют за группой' | Out-Null
            }
        }
        if (-not $taskOk) {
            Add-Step "задача планировщика $TaskName`: при загрузке Windows, без входа пользователя, от $user" {
                $boot = Join-Path $ScriptsDir 'node-boot.ps1'
                if ($DryRun) {
                    Show-Dry ("Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Trigger (New-ScheduledTaskTrigger -AtStartup) " +
                        "-Action (powershell.exe $(Get-BootArgs)) -User $user -Password *** -RunLevel Highest " +
                        '-Settings (ExecutionTimeLimit 0, RestartCount 999, RestartInterval 1 мин, AllowStartIfOnBatteries)')
                    return
                }
                $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (Get-BootArgs)
                $trigger = New-ScheduledTaskTrigger -AtStartup
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
                if (Get-BootTask) { Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false }
                try {
                    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action -Trigger $trigger -Settings $settings `
                        -User $user -Password $TaskPassword -RunLevel Highest -Description 'NSU LLM: WSL с нодой при загрузке Windows' | Out-Null
                }
                catch { Fail "Не удалось создать задачу $TaskName`: $($_.Exception.Message)" "Проверьте пароль учётной записи $user (у неё должен быть непустой пароль) и повторите." }
            }
        }
        if (-not $desk) {
            Add-Step "запустить задачу $TaskName (держит $Distro запущенным)" {
                if ($DryRun) { Show-Dry "Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName"; return }
                $t = Get-BootTask
                if ($t -and "$($t.State)" -ne 'Running') { Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName }
            }
        }
        $where = $(if ($desk) { 'в Docker Desktop (bridge)' } else { "-Wsl $Distro" })
        Add-Step "запустить ноду: run-node.ps1 $where (режим $Mode, образ $image, адрес $NodeIp)" {
            $p = @{ Main = $Main; Token = $Token; Mode = $Mode; Accel = $script:AccelFinal; Name = $Name; NodeIp = $NodeIp; AllowFrom = $AllowFrom; Image = $image }
            if ($nic) { $p.WolMac = $nic.Mac; $p.WolIp = $nic.Ip; $p.WolPrefix = $nic.Prefix }
            if (-not $desk) { $p.Wsl = $Distro }
            if ($InsecureTls) { $p.InsecureTls = $true }
            if ($CaSha256) { $p.CaSha256 = $CaSha256 }
            if ($desk) { $p.DockerUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }   # Docker Desktop читает каталог данных от этого пользователя
            if ($AcceptCa) { $p.AcceptCa = $true }
            if ($DryRun) { $p.DryRun = $true }
            $logs = $(if ($desk) { 'docker logs --tail 50 llm-node' } else { "wsl -d $Distro -u root -e docker logs --tail 50 llm-node" })
            try { & (Join-Path $PSScriptRoot 'run-node.ps1') @p | Out-Host }
            catch { Fail "run-node.ps1: $($_.Exception.Message)" "Состояние: .\install-node.ps1 -Status; журнал ноды: $logs" }
        }
    }

    # --- сводка и подтверждение
    Say ''
    Say "Нода $Name -> главный $Main ($mainIp), режим $Mode, ускоритель $acc$(if ($accNote) { " ($accNote)" }), адрес $NodeIp"
    Say "Брандмауэр: TCP 18080 только с $AllowFrom$(if ($Mode -ne 'standalone') { '; TCP 18050 (RPC split) только с главного и нод текущей группы split' })"
    if ($nic) { Say "Wake-on-LAN: сетевая карта $($nic.Name) ($($nic.Description)), MAC $($nic.Mac), $($nic.Ip)/$($nic.Prefix)" }
    else { Say "Wake-on-LAN: не настроен ($($wol.Why))" }
    Say "Кнопка питания: $(if ($PowerButton -eq 'nothing') { 'ничего не делает' } else { 'как настроено в Windows' })"
    if ($Proxy) {
        Say "Прокси: $(Get-MaskedUrl $Proxy) (wsl --install/--update, apt и Docker CE в WSL, демон Docker, контейнер ноды); NO_PROXY=$($script:NoProxyList)"
        if ($desk) { Warn 'Docker Desktop: прокси для загрузки образов задаётся в его настройках (Settings, Resources, Proxies); контейнер ноды получает HTTP_PROXY, HTTPS_PROXY и NO_PROXY.' }
    }
    if ($script:UseTask) { Say "Выполнение: одноразовая задача $InstallTaskName (вход по паролю $user, исполнитель class-run.ps1), журнал $InstallLog. Обрыв ssh или WinRM её не остановит: ход покажет повторный запуск и -Status." }
    if ($desk) { Say 'Docker: Docker Desktop (запасной путь). Автозапуск: нода вернётся только после входа пользователя в Windows (Docker Desktop стартует при входе, контейнер — по --restart unless-stopped). На Windows 10 ПК класса должен входить в Windows сам: автовход (netplwiz, Winlogon AutoAdminLogon) решает администратор. Без входа: -Docker wsl (Windows 11 22H2+).' }
    else { Say "Docker: Docker CE в дистрибутиве $Distro, автозапуск без входа пользователя (задача $TaskName)." }
    Say 'Будет сделано:'
    $n = 0; foreach ($s in $steps) { $n++; Say "  $n. $($s.Title)" }
    if (-not $NonInteractive -and -not $Yes) {
        if ((Read-Answer 'Продолжить? (д/н)' 'д') -notmatch '^(д|да|y|yes)$') { Say 'Отменено, ничего не изменено.'; return 1 }
    }

    # --- ответы сохраняются до действий: после перезагрузки повторный запуск идёт без вопросов
    $tokenEnc = ''; $proxyEnc = ''
    $hasPw = $Proxy -and ((Get-MaskedUrl $Proxy) -ne $Proxy)
    if (-not $DryRun) {
        try { $tokenEnc = Protect-Secret $Token; if ($hasPw) { $proxyEnc = Protect-Secret $Proxy } }
        catch { Warn "секрет не сохранён (DPAPI: $($_.Exception.Message)); при повторном запуске передайте -Token или CLUSTER_TOKEN" }
    }
    $save = [ordered]@{ Main = $Main; Mode = $Mode; Accel = $Accel; Docker = $script:DockerFinal; Name = $Name; Distro = $Distro; AllowFrom = $AllowFrom; CaSha256 = $CaSha256; MemoryGB = $MemoryGB
        NodeIp = $(if ($script:NodeIpExplicit -or (Get-Prop $answers 'NodeIp')) { $NodeIp } else { '' })
        DistroSource = $DistroSource; ImageTar = $ImageTar; ImageSource = $ImageSource; InsecureTls = [bool]$InsecureTls; Proxy = $(if ($Proxy) { Get-MaskedUrl $Proxy } else { '' }); PowerButton = $PowerButton
        ProxyMachine = $proxyEnc; TokenMachine = $tokenEnc
    }
    Initialize-InstallDir
    if ($DryRun) {
        $shown = ($save.Keys | Where-Object { $_ -notin @('TokenMachine', 'ProxyMachine') -and "$($save[$_])" -ne '' } | ForEach-Object { "$_=$($save[$_])" }) -join '; '
        Show-Dry "сохранить ответы в $AnswersPath (только Administrators и SYSTEM): $shown"
    }
    else { Write-SecureFile -Path $AnswersPath -Text ([pscustomobject]$save | ConvertTo-Json) -AdminOnly }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent()
    Set-State 'InstallUser' $me.Name; Set-State 'InstallUserSid' $me.User.Value; Save-State

    # Прокси для дочерних процессов: wsl --install/--update --web-download, curl и контейнер ноды (run-node передаёт их в -e).
    if ($Proxy) {
        foreach ($v in @('HTTP_PROXY', 'HTTPS_PROXY')) { [Environment]::SetEnvironmentVariable($v, $Proxy, 'Process') }
        [Environment]::SetEnvironmentVariable('NO_PROXY', $script:NoProxyList, 'Process')
    }
    if ($script:UseTask) {
        $ta = [ordered]@{ NonInteractive = $true; Yes = $true; FromTask = $true; InstallDir = $InstallDir; NodeIp = $NodeIp; TaskPassword = $TaskPassword }
        if ($WslConfigPath -ne (Join-Path $env:USERPROFILE '.wslconfig')) { $ta.WslConfigPath = $WslConfigPath }
        return (Invoke-InstallTask $ta)
    }

    $n = 0
    foreach ($s in $steps) { $n++; Say "[$n/$($steps.Count)] $($s.Title)"; & $s.Do | Out-Host }
    if ($reboot) {
        Say "Перезагрузите ПК и запустите установку снова: powershell -ExecutionPolicy Bypass -File $Self  (ответы сохранены в $AnswersPath)"
        return 3010
    }
    Say ''
    if ($DryRun) { Say 'Пробный прогон (-DryRun): ничего не изменено.'; return 0 }
    # Успешная установка в этом сеансе: след прошлого запуска в разовой задаче (run.done, action.txt) устарел, -Status не должен его показывать.
    if (-not $FromTask) { foreach ($f in @((Join-Path $RunDir 'run.done'), $InstallActionFile)) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue } }
    Say "Готово. Состояние: powershell -ExecutionPolicy Bypass -File $(Join-Path $ScriptsDir 'install-node.ps1') -Status"
    Show-Status
    return 0
}

# ================================================================== -ViaTask: одноразовая задача (установка или удаление)
# Задача входит по паролю (пакетный вход) и получает полные учётные данные: WSL, DPAPI и Register-ScheduledTask работают
# как при обычном входе. Исполнитель — class-run.ps1 (общий с class-deploy.ps1): аргументы в файле под DPAPI LocalMachine
# (только Administrators и SYSTEM, удаляется при чтении), вывод построчно в run\install-task.log, код в run\run.done.
function Invoke-InstallTask($taskArgs, [switch]$Removing) {
    $user = Get-CurrentUser
    $child = Join-Path $ScriptsDir 'install-node.ps1'
    $runner = Join-Path $ScriptsDir 'class-run.ps1'
    $pf = Join-Path $RunDir 'run-params.bin'; $done = Join-Path $RunDir 'run.done'
    $arg = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Params `"$pf`" -Log `"$InstallLog`" -Done `"$done`""
    $shownArgs = ($taskArgs.Keys | ForEach-Object { if ($_ -in @('TaskPassword', 'Token')) { "$_=***" } else { "$_=$($taskArgs[$_])" } }) -join ', '
    if ($DryRun) {
        Copy-ScriptFiles
        Show-Dry "параметры задачи в $pf (DPAPI LocalMachine, только Administrators и SYSTEM): $shownArgs"
        Show-Dry "Register-ScheduledTask -TaskName $InstallTaskName -TaskPath $TaskPath -Action (powershell.exe $arg) -User $user -Password *** -RunLevel Highest"
        Show-Dry "Start-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName; ждать $done, показывать $InstallLog; Unregister-ScheduledTask"
        Say ''; Say 'Пробный прогон (-DryRun): ничего не изменено.'
        return 0
    }
    Initialize-InstallDir
    Copy-ScriptFiles
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    # Прошлая задача ещё идёт (например, связь оборвалась, а установка продолжается): ничего не запускать заново, показать её ход.
    if (Get-InstallTaskRunning) {
        Warn "прошлый запуск задачи $InstallTaskName ещё идёт: показываю его журнал, новые параметры не применялись"
        return (Wait-InstallTask -Removing:$Removing -Attached)
    }
    Remove-Item -LiteralPath $InstallLog, $done -Force -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText($InstallActionFile, $(if ($Removing) { 'uninstall' } else { 'install' }))
    $old = $null; try { $old = Get-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName -ErrorAction Stop } catch { }
    if ($old) { Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName -Confirm:$false }
    $json = [pscustomobject]@{ Script = $child; Args = [pscustomobject]$taskArgs; EnvProxy = '' } | ConvertTo-Json -Depth 4
    Add-Type -AssemblyName System.Security
    Write-SecureFile -Path $pf -AdminOnly -Bytes ([Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($json), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine))
    $json = $null
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    try {
        Register-ScheduledTask -TaskName $InstallTaskName -TaskPath $TaskPath -Action $action -Settings $settings -User $user -Password $TaskPassword `
            -RunLevel Highest -Description 'NSU LLM: разовый запуск install-node.ps1 (удаляется после выполнения)' -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName -ErrorAction Stop
    }
    catch {
        Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue
        Fail "Не удалось создать или запустить задачу $InstallTaskName от $user`: $($_.Exception.Message)" "Проверьте пароль учётной записи $user и право «Вход в качестве пакетного задания» (у Администраторов оно есть, если GPO его не забирает)."
    }
    return (Wait-InstallTask -Removing:$Removing)
}

# Ждёт конца разовой задачи и показывает её журнал по ходу. -Attached: задача уже шла (прошлый сеанс оборвался) и показывается с начала;
# параметры файла запуска к ней не относятся. Возвращает код установщика.
function Wait-InstallTask([switch]$Removing, [switch]$Attached) {
    $user = Get-CurrentUser
    $pf = Join-Path $RunDir 'run-params.bin'; $done = Join-Path $RunDir 'run.done'
    Say "$(if ($Attached) { 'Подключился к идущей задаче: ' })$(if ($Removing) { 'Удаление' } else { 'Установка' }) идёт в задаче $InstallTaskName, журнал ${InstallLog}:"
    $enc = New-Object Text.UTF8Encoding $false
    $pos = [long]0; $buf = ''
    $pull = {
        param([switch]$Flush)
        if (Test-Path -LiteralPath $InstallLog) {
            $fs = [IO.File]::Open($InstallLog, 'Open', 'Read', 'ReadWrite')
            try { [void]$fs.Seek($script:pos, 'Begin'); $bytes = New-Object byte[] ([int][math]::Max(0, $fs.Length - $script:pos)); $n = $fs.Read($bytes, 0, $bytes.Length); $script:pos += $n }
            finally { $fs.Close() }
            if ($n -gt 0) { $script:buf += $enc.GetString($bytes, 0, $n) }
        }
        $parts = $script:buf -split "`n"; $script:buf = $parts[-1]
        for ($i = 0; $i -lt $parts.Count - 1; $i++) { Write-Host $parts[$i].TrimEnd("`r") }
        if ($Flush -and $script:buf) { Write-Host $script:buf.TrimEnd("`r"); $script:buf = '' }
    }
    $script:pos = $pos; $script:buf = $buf
    $t0 = Get-Date; $deadline = $t0.AddHours(2); $why = ''
    while ($true) {
        & $pull
        if (Test-Path -LiteralPath $done) { break }
        if ((Get-Date) -gt $deadline) { $why = 'timeout'; break }
        $ts = ''; try { $ts = "$((Get-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName -ErrorAction Stop).State)" } catch { }
        # Не запустилась за минуту (нет права пакетного входа, политика выполнения) или завершилась без run.done: исполнитель не дошёл до конца.
        $started = (Test-Path -LiteralPath $InstallLog) -or $ts -eq 'Running'
        if (((Get-Date) - $t0).TotalSeconds -gt 60 -and -not $started) { $why = 'nostart'; break }
        if (((Get-Date) - $t0).TotalSeconds -gt 10 -and $ts -notin @('Running', 'Queued') -and -not (Test-Path -LiteralPath $done)) { $why = 'dead'; break }
        Start-Sleep -Seconds 2
    }
    & $pull -Flush
    $res = ''; try { $res = '0x{0:X}' -f [int](Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $InstallTaskName -ErrorAction Stop).LastTaskResult } catch { }
    if ($why -eq 'timeout') { Fail "Задача $InstallTaskName не завершилась за 2 ч" "Она продолжает работу; журнал $InstallLog, состояние: Get-ScheduledTaskInfo -TaskPath '$TaskPath' -TaskName $InstallTaskName" }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $InstallTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue
    if ($why) {
        Fail "Задача $InstallTaskName $(if ($why -eq 'nostart') { 'не запустилась за 60 с' } else { 'завершилась без результата' }) (LastTaskResult $res)" "Проверьте право «Вход в качестве пакетного задания» для $user (secpol.msc, локальные политики, назначение прав) и журнал планировщика; запуск без задачи: локальный вход или ssh с паролем."
    }
    $code = 1; [void][int]::TryParse(([IO.File]::ReadAllText($done)).Trim(), [ref]$code)
    if ($code -ne 0 -and $code -ne 3010) { Write-Host "Задача завершилась с кодом $code (LastTaskResult $res); полный журнал: $InstallLog" -ForegroundColor Red }
    # Удаление: задача оставила run\ для журнала и кода, остальное убрано; теперь можно удалить и его.
    if ($Removing -and $code -eq 0) {
        if (Test-Path -LiteralPath (Join-Path $InstallDir 'wsl')) { Remove-Item -LiteralPath $RunDir -Recurse -Force -ErrorAction SilentlyContinue }
        else { Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
    return $code
}

try {
    Initialize-Answers
    Resolve-Answers
    if ($Status) { Show-Status; exit $(if ($script:bad -gt 0) { 1 } else { 0 }) }
    # Прошлый запуск (установка или удаление в разовой задаче) ещё идёт, а сеанс, который его начал, оборвался: подключиться к нему,
    # а не начинать заново (задача держится планировщиком и сеанс ssh/WinRM ей не нужен).
    if (-not $FromTask -and -not $DryRun) {
        $busy = Get-InstallTaskRunning
        if ($busy) {
            $want = $(if ($Uninstall) { 'uninstall' } else { 'install' })
            $has = ''; try { $has = ([IO.File]::ReadAllText($InstallActionFile)).Trim() } catch { }
            if ($has -ne $want) {
                Fail "В задаче $InstallTaskName сейчас идёт $(if ($has -eq 'uninstall') { 'удаление' } elseif ($has -eq 'install') { 'установка' } else { 'прошлый запуск' }), а запрошено: $(if ($Uninstall) { 'удаление' } else { 'установка' })" "Дождитесь его конца (-Status показывает ход, журнал $InstallLog) или остановите: Stop-ScheduledTask -TaskPath '$TaskPath' -TaskName $InstallTaskName"
            }
            Say "Идёт $(if ($want -eq 'uninstall') { 'удаление' } else { 'установка' }) в разовой задаче $InstallTaskName (начата прошлым запуском, возможно оборванным сеансом): показываю её журнал, этот запуск ничего не меняет."
            exit (Wait-InstallTask -Removing:($want -eq 'uninstall') -Attached)
        }
    }
    if ($Uninstall) {
        if (-not $DryRun -and -not (Test-Admin)) { Fail 'Нужны права администратора' 'Запустите PowerShell от имени администратора.' }
        exit (Invoke-Uninstall)
    }
    exit (Invoke-Install)
}
catch {
    Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    $fix = $_.Exception.Data['fix']
    if ($fix) { Write-Host "Как исправить: $fix" }
    if ($env:NSU_LLM_DEBUG) { Write-Host $_.ScriptStackTrace }
    exit 1
}
