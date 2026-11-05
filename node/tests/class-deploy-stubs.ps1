# Тестовая обвязка class-deploy.ps1: подменяет всё, что ходит в сеть (Invoke-Command, New-PSSession, Test-WSMan, Copy-Item -ToSession,
# New-PSDrive для SMB, задания и порты), и запускает скрипт. Поведение «ПК класса» задаёт файл сценария (JSON), «удалённые» диски
# лежат в каталоге $env:STUB_ROOT\<адрес>\. Журнал вызовов — $env:STUB_LOG. Запуск: powershell -File class-deploy-stubs.ps1 -Scenario s.json -Script class-deploy.ps1 -ArgLine "-Action status".
param([Parameter(Mandatory = $true)][string]$Scenario, [Parameter(Mandatory = $true)][string]$Script, [string]$ArgLine = '')
$ErrorActionPreference = 'Stop'
# Сценарий — в $global:Scn: имя $S в функциях перекрыли бы переменные $s проверяемого кода (имена без учёта регистра).
$global:Scn = Get-Content -Raw -Encoding UTF8 -Path $Scenario | ConvertFrom-Json
$global:StubLog = $env:STUB_LOG
$global:StubRoot = $env:STUB_ROOT
$global:JobSeq = 0; $global:SessSeq = 0; $global:Running = 0
$global:Tasks = @{}

# SecureString в журнале: тип и начало SHA-256 открытого текста (значение не печатается, но его можно сверить)
function SecureTag($v) {
    $pt = (New-Object Net.NetworkCredential('', $v)).Password
    $h = (([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($pt)) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 8)
    return "<SecureString:$h>"
}
function Log([string]$t) { Add-Content -Path $global:StubLog -Value $t -Encoding UTF8 }
# Свойство сценария: узел -> поле, со значением по умолчанию.
function HostCfg([string]$addr) { $p = $global:Scn.hosts.PSObject.Properties[$addr]; if ($p) { return $p.Value }; return $null }
function Cfg($obj, [string]$name, $def) {
    if ($obj -is [System.Collections.IDictionary]) { if ($obj.Contains($name)) { return $obj[$name] }; return $def }
    if ($obj -and $obj.PSObject.Properties[$name]) { return $obj.$name }; return $def
}
function RemotePath([string]$addr, [string]$path) { return (Join-Path (Join-Path $global:StubRoot $addr) ($path -replace '^[A-Za-z]:\\', '')) }

# --- то, что читает окружение администратора
function Test-Ports([string[]]$addrs, [int]$port) {
    Log "ports $port $($addrs -join ',')"
    $r = @{}; foreach ($a in $addrs) { $r[$a] = [bool](Cfg (HostCfg $a) 'tcp' $true) }; return $r
}
$global:Trusted = [string](Cfg $global:Scn 'trusted' '*')
function Get-TrustedHosts { return $global:Trusted }
function Set-TrustedHosts([string]$value) { Log "Set-TrustedHosts $value"; $global:Trusted = $value }
function Test-DomainJoined { return [bool](Cfg $global:Scn 'domain' $false) }

# --- WinRM
function Test-WSMan { [CmdletBinding()] param($ComputerName, $Credential, $Authentication, [switch]$UseSSL)
    Log "Test-WSMan $ComputerName"
    if (-not (Cfg (HostCfg $ComputerName) 'wsman' $true)) { throw 'WinRM client cannot process the request' }
}
function New-PSSession { [CmdletBinding()] param($ComputerName, $Credential, $SessionOption, [switch]$UseSSL)
    Log "New-PSSession $ComputerName user=$($Credential.UserName) noprofile=$($SessionOption.NoMachineProfile)"
    if (-not (Cfg (HostCfg $ComputerName) 'session' $true)) { throw 'Access is denied' }
    # ПК в рабочей группе при домене у администратора: вход только после TrustedHosts.
    if ((Cfg (HostCfg $ComputerName) 'needtrust' $false) -and (@($global:Trusted -split ',') -notcontains $ComputerName)) { throw 'Kerberos: the computer is not joined to the domain' }
    $global:SessSeq++
    return [pscustomobject]@{ ComputerName = $ComputerName; Id = $global:SessSeq }
}
# Windows LAPS: у каждого ПК свой пароль локального администратора (сценарий: "laps": false — нет пароля)
function Get-LapsADPassword { [CmdletBinding()] param($Identity, [switch]$AsPlainText)
    Log "LAPS $Identity"
    if (-not (Cfg $global:Scn 'laps' $true)) { throw 'Windows LAPS: no password for this computer' }
    return [pscustomobject]@{ Account = 'lapsadmin'; Password = "LapsPw-$Identity-9z" }
}
function Remove-PSSession { [CmdletBinding()] param($Session) Log "Remove-PSSession $($Session.ComputerName)" }

function Invoke-Command { [CmdletBinding()] param($Session, [scriptblock]$ScriptBlock, $ArgumentList, [switch]$AsJob)
    $addr = $Session.ComputerName; $h = HostCfg $addr
    $global:CurAddr = $addr
    if (-not $AsJob) {
        $a0 = @($ArgumentList)[0]
        if ($a0 -is [System.Collections.IDictionary] -and $a0['Op'] -eq 'place') {
            Log "PLACE $addr $(@($a0.Items | ForEach-Object { Split-Path -Leaf $_.Path }) -join ',')"
            $q = @{}; foreach ($k in $a0.Keys) { $q[$k] = $a0[$k] }
            $q.Items = @($a0.Items | ForEach-Object { @{ Tmp = (RemotePath $addr $_.Tmp); Path = (RemotePath $addr $_.Path) } })
            return (& $ScriptBlock $q)
        }
        if ($a0 -is [System.Collections.IDictionary] -and $a0['Op'] -eq 'secure') {
            # Настоящий блок Secure-Dir на «диске ПК»; SD каталога и перенос в сторону — через подмены ниже.
            Log "SECURE $addr $($a0.Path)"
            $q = @{}; foreach ($k in $a0.Keys) { $q[$k] = $a0[$k] }
            $q.Path = RemotePath $addr $a0.Path
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $q.Path) | Out-Null
            return (& $ScriptBlock $q)
        }
        $d = RemotePath $addr ([string]$a0); New-Item -ItemType Directory -Force -Path $d | Out-Null
        Log "mkdir $addr $a0"; return
    }
    $p = $ArgumentList[0]
    $global:Running++
    Log "START $addr op=$($p.Op) running=$($global:Running)"
    $act = [string](Cfg $p 'Action' '')
    $items = @()
    if ($p.Op -eq 'probe') {
        $pr = Cfg $h 'probe' $null
        $stage = RemotePath $addr $p.StageDir
        $files = @{}
        if (Test-Path -LiteralPath $stage) { Get-ChildItem -LiteralPath $stage -File | ForEach-Object { $files[$_.Name] = [long]$_.Length } }
        $gpu = [bool](Cfg $pr 'HasGpu' $false)
        $drv = [string](Cfg $pr 'Driver' $(if ($gpu) { '580.88' } else { '' }))
        $major = 0; [void][int]::TryParse(($drv -split '\.')[0], [ref]$major)
        $mk = Join-Path $stage 'image-loaded.json'
        $mark = $(if (Test-Path -LiteralPath $mk) { [IO.File]::ReadAllText($mk) } else { [string](Cfg $pr 'ImageMark' '') })
        $items = @([pscustomobject]@{ Kind = 'probe'; ImageMark = $mark; Computer = $addr; IsAdmin = [bool](Cfg $pr 'IsAdmin' $true); HasGpu = $gpu; Driver = $drv; DriverMajor = $major
            FreeGB = [double](Cfg $pr 'FreeGB' 200); Build = [int](Cfg $pr 'Build' 26100); Installed = [bool](Cfg $pr 'Installed' $false); StageFiles = $files })
    }
    elseif ($p.Op -eq 'tail') {
        Log "TAIL $addr $($p.Path)"
        $items = @([pscustomobject]@{ Kind = 'tail'; Text = [string](Cfg $h 'tail' '') })
    }
    else {
        Log ("RUN $addr action=$act mode=$($p.RunMode) cleanup=$($p.Cleanup) proxy=$($p.Proxy) args=" + (($p.Args.Keys | Sort-Object | ForEach-Object { "$_=$(if ($p.Args[$_] -is [Security.SecureString]) { SecureTag $p.Args[$_] } else { $p.Args[$_] })" }) -join ' ') + " taskpw=$(if ($p.TaskPassword -is [Security.SecureString]) { SecureTag $p.TaskPassword } elseif ($p.TaskPassword) { 'PLAIN' } else { '' }) taskuser=$($p.TaskUser)")
        if ((Cfg $global:Scn 'exec' '') -eq 'local') {
            # Настоящий удалённый блок на «диске ПК»: своя копия каталога раздачи на каждый адрес, политика выполнения как на чистом ПК.
            $q = @{}; foreach ($k in $p.Keys) { $q[$k] = $p[$k] }
            $q.Args = @{}; foreach ($k in $p.Args.Keys) { $q.Args[$k] = $p.Args[$k] }
            $local = Join-Path $global:StubRoot "$addr-stage"
            if (-not (Test-Path -LiteralPath $local)) {
                New-Item -ItemType Directory -Force -Path $local | Out-Null
                if (Test-Path -LiteralPath $p.StageDir) { Get-ChildItem -LiteralPath $p.StageDir -File | ForEach-Object { Microsoft.PowerShell.Management\Copy-Item -LiteralPath $_.FullName -Destination $local } }
            }
            $q.StageDir = $local
            if ($q.Args['ImageTar']) { $q.Args['ImageTar'] = Join-Path $local (Split-Path -Leaf $q.Args['ImageTar']) }
            Set-ExecutionPolicy -Scope Process -ExecutionPolicy AllSigned -Force
            try { $items = @(& $ScriptBlock $q) } finally { Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force }
        }
        else {
            $r = Cfg (Cfg $h 'run' $null) $act $null
            foreach ($l in @(Cfg $r 'Lines' @())) { $items += [pscustomobject]@{ Kind = 'line'; Text = [string]$l } }
            $items += [pscustomobject]@{ Kind = 'run'; Code = [int](Cfg $r 'Code' 0); Output = [string](Cfg $r 'Output' 'ok') }
        }
    }
    $global:JobSeq++
    return [pscustomobject]@{ Id = $global:JobSeq; State = 'Running'; Result = $items; Fake = $true; Fail = ([bool](Cfg $h 'jobfail' $false) -and $p.Op -eq 'run')
        Hang = ([bool](Cfg $h 'hang' $false) -and $p.Op -eq 'run')
        ChildJobs = @([pscustomobject]@{ JobStateInfo = [pscustomobject]@{ Reason = [pscustomobject]@{ Message = 'remote session broke' } } }) }
}
# Задание завершается при первом Wait-Job -Any: так видна настоящая параллельность.
function Wait-Job { [CmdletBinding()] param($Job, [switch]$Any, $Timeout)
    $fake = @($Job | Where-Object { $_.PSObject.Properties['Fake'] })
    if ($fake.Count -eq 0) { return Microsoft.PowerShell.Core\Wait-Job @PSBoundParameters }
    $j = $fake | Where-Object { $_.State -eq 'Running' -and -not $_.Hang } | Select-Object -First 1
    if ($j) { $j.State = $(if ($j.Fail) { 'Failed' } else { 'Completed' }); $global:Running-- }
    else { Start-Sleep -Milliseconds 200 }
    return $j
}
function Receive-Job { [CmdletBinding()] param($Job)
    if (-not $Job.PSObject.Properties['Fake']) { return Microsoft.PowerShell.Core\Receive-Job @PSBoundParameters }
    if ($Job.Fail -or $Job.PSObject.Properties['Received']) { return }
    $Job | Add-Member -NotePropertyName Received -NotePropertyValue $true
    return $Job.Result
}
function Remove-Job { [CmdletBinding()] param($Job, [switch]$Force) if (-not $Job.PSObject.Properties['Fake']) { Microsoft.PowerShell.Core\Remove-Job @PSBoundParameters } }
function Stop-Job { [CmdletBinding()] param($Job) if (-not $Job.PSObject.Properties['Fake']) { Microsoft.PowerShell.Core\Stop-Job @PSBoundParameters } else { Log "Stop-Job $($Job.Id)" } }

# --- планировщик и ACL на «ПК класса» (exec=local): задача запускается отдельным процессом powershell, как настоящая
function New-ScheduledTaskAction { param($Execute, $Argument) return [pscustomobject]@{ Execute = $Execute; Argument = $Argument } }
function New-ScheduledTaskSettingsSet { param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit, $MultipleInstances) return [pscustomobject]@{ Limit = $ExecutionTimeLimit } }
function Register-ScheduledTask { [CmdletBinding()] param($TaskName, $TaskPath, $Action, $Settings, $User, $Password, $RunLevel, $Description, [switch]$Force)
    Log "Register-ScheduledTask $TaskPath$TaskName user=$User runlevel=$RunLevel pwset=$([bool]$Password) exe=$($Action.Execute) arg=$($Action.Argument)"
    $global:Tasks["$TaskPath$TaskName"] = @{ Action = $Action; Proc = $null }
}
function Start-ScheduledTask { [CmdletBinding()] param($TaskPath, $TaskName)
    Log "Start-ScheduledTask $TaskPath$TaskName"
    $t = $global:Tasks["$TaskPath$TaskName"]
    $t.Proc = Start-Process -FilePath $t.Action.Execute -ArgumentList $t.Action.Argument -WindowStyle Hidden -PassThru
}
function Get-ScheduledTask { [CmdletBinding()] param($TaskPath, $TaskName)
    $t = $global:Tasks["$TaskPath$TaskName"]
    # STUB_TASK_RUNNING: задача прошлого запуска (другого процесса) ещё идёт на ПК
    if (-not $t -and $env:STUB_TASK_RUNNING) { return [pscustomobject]@{ TaskName = $TaskName; State = 'Running' } }
    if (-not $t) { throw "no task $TaskName" }
    return [pscustomobject]@{ TaskName = $TaskName; State = $(if ($t.Proc -and -not $t.Proc.HasExited) { 'Running' } else { 'Ready' }) }
}
function Get-ScheduledTaskInfo { [CmdletBinding()] param($TaskPath, $TaskName)
    $t = $global:Tasks["$TaskPath$TaskName"]
    return [pscustomobject]@{ LastTaskResult = $(if ($t.Proc -and $t.Proc.HasExited) { $t.Proc.ExitCode } else { 267009 }) }
}
function Unregister-ScheduledTask { [CmdletBinding()] param($TaskPath, $TaskName, $Confirm)
    Log "Unregister-ScheduledTask $TaskPath$TaskName"
    $global:Tasks.Remove("$TaskPath$TaskName")
}
# Права на «ПК класса» (точки подмены node\secure-dir.ps1). Без прав администратора владельца Administrators не поставить и
# Users-только-чтение не дало бы тесту писать: SD каталога в журнал — как запрошен, на диск — без владельца и с этой учётной записью.
# aclfail в сценарии узла: отложить недоверенный каталог не удаётся.
function New-SdDirSecurity([switch]$NoUsers, [switch]$NoOwner, [string[]]$ExtraSids = @()) {
    Log "DirSec owner=$(if ($NoOwner) { 'keep' } else { 'S-1-5-32-544' }) protected=True ids=S-1-5-18:FullControl,S-1-5-32-544:FullControl$(if (-not $NoUsers) { ',S-1-5-32-545:ReadAndExecute' })"
    $sec = New-Object Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)
    $inh = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $ids = @('S-1-5-18', 'S-1-5-32-544', [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    foreach ($sid in $ids) { $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier $sid), 'FullControl', $inh, 'None', 'Allow'))) }
    if (-not $NoUsers) { $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-545'), 'ReadAndExecute', $inh, 'None', 'Allow'))) }
    return $sec
}
function Move-SdAside([string]$From, [string]$To) {
    Log "MoveAside $(Split-Path -Leaf $From) -> $(Split-Path -Leaf $To)"
    if ([bool](Cfg (HostCfg $global:CurAddr) 'aclfail' $false)) { throw 'Access to the path is denied.' }
    if ([IO.Directory]::Exists($From)) { [IO.Directory]::Move($From, $To) } else { [IO.File]::Move($From, $To) }
}
# Файл «только SYSTEM и Administrators»: в журнал — что запрошено; на диск — ещё и текущая учётная запись, иначе задача
# (отдельный процесс без повышения) не прочитала бы файл параметров.
function New-SdAdminOnlySecurity([string]$Path) {
    Log "AdminOnly $(Split-Path -Leaf $Path) exists=$([IO.File]::Exists($Path)) ids=S-1-5-18:FullControl,S-1-5-32-544:FullControl protected=True"
    $sec = New-Object Security.AccessControl.FileSecurity
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)) {
        $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier $sid), 'FullControl', 'Allow')))
    }
    return $sec
}

# Имя файла раздачи в журнале: файлы копируются во временное имя .<имя>.<guid>.tmp и ставятся на место блоком place.
function StageName([string]$d) { $l = Split-Path -Leaf $d; if ($l -match '^\.(.+)\.[0-9a-f]{32}\.tmp$') { return "$($matches[1]) (tmp)" }; return $l }
# --- копирование: WinRM пишет в «удалённый диск» сразу, SMB — через настоящий PSDrive на каталог
function Copy-Item { [CmdletBinding()] param([string]$Path, [string]$LiteralPath, [string]$Destination, $ToSession, [switch]$Force, [switch]$Recurse)
    if ($ToSession) {
        $dst = RemotePath $ToSession.ComputerName $Destination
        Log "Copy-Item -ToSession $($ToSession.ComputerName) $(StageName $Destination)"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
        Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $dst -Force
        return
    }
    if ($Destination -match '^NSUND\d+:') { Log "Copy-Item smb $(StageName $Destination)" }
    Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
}
function New-PSDrive { [CmdletBinding()] param($Name, $PSProvider, $Root, $Credential, $Scope, [switch]$Persist)
    if ("$Name" -notlike 'NSUND*') { return Microsoft.PowerShell.Management\New-PSDrive @PSBoundParameters }
    $addr = ($Root -replace '^\\\\([^\\]+)\\.*$', '$1')
    Log "New-PSDrive $Name $Root user=$($Credential.UserName)"
    if (-not (Cfg (HostCfg $addr) 'smb' $true)) { throw 'The network path was not found' }
    $d = Join-Path $global:StubRoot $addr; New-Item -ItemType Directory -Force -Path $d | Out-Null
    return Microsoft.PowerShell.Management\New-PSDrive -Name $Name -PSProvider FileSystem -Root $d -Scope Global
}

Invoke-Expression "& '$Script' $ArgLine"
exit $LASTEXITCODE
