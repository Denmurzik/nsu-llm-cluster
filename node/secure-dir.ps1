# Каталоги NSU LLM в C:\ProgramData защищаются до любой записи (подключается через dot-source: install-node.ps1, class-deploy.ps1).
# Права C:\ProgramData по умолчанию разрешают Users создавать каталоги и становиться их владельцем (CREATOR OWNER). Ученик,
# заранее создавший C:\ProgramData\NSU-LLM, мог бы подменить скрипты, которые потом запускаются от администратора при каждой
# загрузке, или выдать себе чтение файла с секретами. Права такого объекта нельзя чинить на месте: дескриптор, открытый раньше
# (например, с WRITE_DAC), переживает смену DACL, и ученик снова выдаст себе доступ. Поэтому:
#   Secure-Dir       — (-NoUsers: Users без доступа, только SYSTEM и Administrators) недоверенный каталог (или недоверенный объект внутри) откладывается в сторону: переименовывается в
#                      <каталог>.untrusted-<время> рядом; каталог создаётся заново сразу с нужным SD
#                      ([IO.Directory]::CreateDirectory(path, DirectorySecurity)): владелец Administrators, SYSTEM и Administrators
#                      полный доступ, Users чтение, без наследования от ProgramData. Не удалось отложить — ошибка.
#                      Ссылка (junction, symlink) на месте самого каталога — отказ.
#   -Exclude <имена> — дочерние каталоги верхнего уровня, содержимое которых не трогается и не проверяется (у install-node.ps1 —
#                      wsl\: диск дистрибутива с правом ВМ Hyper-V, без которого ВМ WSL может не запуститься). Сам такой каталог
#                      проверяется (не ссылка, владелец SYSTEM, Administrators или администратор, который ставит ноду), и из его
#                      DACL убираются Users, Everyone, Authenticated Users и CREATOR OWNER (запись ВМ остаётся).
#   Test-TrustedPath — можно ли доверять каталогу или файлу (те же правила; только чтение).
#   New-SecureDir    — новый каталог сразу с нужным SD (-NoUsers: только SYSTEM и Administrators), например wsl\ до wsl --install.
#   Write-SecureFile — запись нового файла вместо старого (старый переименовывается и удаляется: открытый раньше дескриптор
#                      остаётся у старого); -AdminOnly создаёт файл сразу с DACL «только SYSTEM и Administrators».
# Самодостаточен: class-deploy.ps1 передаёт этот текст на ПК класса и выполняет его через . ([scriptblock]::Create(...)), поэтому
# без $script:-переменных. Права читаются и пишутся через .NET (Get-Acl не грузится, если PSModulePath унаследован от pwsh 7).
# Точки подмены для тестов (определяются, только если таких функций ещё нет): New-SdDirSecurity, New-SdAdminOnlySecurity,
# Move-SdAside.

$SdTrustedSids = @('S-1-5-18', 'S-1-5-32-544')
# Права, которые позволяют менять содержимое, удалять или переписывать DACL/владельца.
$SdWriteMask = 0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
# Кого нет в DACL исключённого каталога (wsl\): Users, Everyone, Authenticated Users, CREATOR OWNER.
$SdStripSids = @('S-1-5-32-545', 'S-1-1-0', 'S-1-5-11', 'S-1-3-0')

# Текст ошибки сразу с подсказкой: class-deploy.ps1 показывает только Message.
function New-SdError([string]$msg, [string]$fix) { return (New-Object System.Exception("$msg`nКак исправить: $fix")) }

# Текущая учётная запись — член Administrators (и без повышения: тогда группа в токене только «для запрета»).
function Test-SdCurrentIsAdmin {
    $i = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (@($i.Groups | ForEach-Object { $_.Value }) -contains 'S-1-5-32-544') { return $true }
    return [bool](@($i.Claims | Where-Object { $_.Type -like '*denyonlysid' -and $_.Value -eq 'S-1-5-32-544' }).Count)
}
# Доверенные: SYSTEM, Administrators, службы (S-1-5-80-*, в том числе TrustedInstaller) и администратор, который запускает
# скрипт (его учётная запись — владелец того, что он создал). Обычный пользователь доверенным не бывает.
function Test-SdTrustedSid([string]$sid) {
    if ($SdTrustedSids -contains $sid) { return $true }
    if ($sid -like 'S-1-5-80-*') { return $true }
    if (@($global:SdExtraSids) -contains $sid) { return $true }   # -ExtraSids: пользователь, под которым работает Docker Desktop (его назвал установщик)
    return ($sid -eq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -and (Test-SdCurrentIsAdmin))
}
function Get-SdAcl([string]$p) { if ([IO.Directory]::Exists($p)) { return [IO.Directory]::GetAccessControl($p) }; return [IO.File]::GetAccessControl($p) }
function Test-SdReparse([string]$p) { return [bool]([IO.File]::GetAttributes($p) -band [IO.FileAttributes]::ReparsePoint) }
function Get-SdOwner([string]$Path) {
    try { $a = Get-SdAcl $Path; try { return $a.GetOwner([Security.Principal.NTAccount]).Value } catch { return $a.GetOwner([Security.Principal.SecurityIdentifier]).Value } }
    catch { return 'неизвестен' }
}
# Один объект: не ссылка, владелец доверенный, писать могут только доверенные.
function Test-SdItem([string]$p) {
    if (Test-SdReparse $p) { return $false }
    $acl = Get-SdAcl $p
    if (-not (Test-SdTrustedSid $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value)) { return $false }
    foreach ($r in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ("$($r.AccessControlType)" -ne 'Allow') { continue }
        if (Test-SdTrustedSid $r.IdentityReference.Value) { continue }
        if (([int64]$r.FileSystemRights -band $SdWriteMask) -ne 0) { return $false }
    }
    return $true
}
# Объект и всё под ним (ссылки не раскрываются).
function Test-SdTree([string]$p) {
    if (-not (Test-SdItem $p)) { return $false }
    if ([IO.Directory]::Exists($p)) { foreach ($c in [IO.Directory]::GetFileSystemEntries($p, '*', 'AllDirectories')) { if (-not (Test-SdItem $c)) { return $false } } }
    return $true
}
# Исключённый каталог: не ссылка и владелец доверенный; права и содержимое не проверяются.
function Test-SdExcluded([string]$p) {
    if (Test-SdReparse $p) { return $false }
    return (Test-SdTrustedSid (Get-SdAcl $p).GetOwner([Security.Principal.SecurityIdentifier]).Value)
}
# Каталог (или файл) и всё под ним, кроме содержимого исключённых каталогов. Нет пути — доверять нечему, $true.
function Test-TrustedPath([string]$Path, [string[]]$Exclude = @()) {
    if (-not ([IO.Directory]::Exists($Path) -or [IO.File]::Exists($Path))) { return $true }
    try {
        if (-not (Test-SdItem $Path)) { return $false }
        if ([IO.Directory]::Exists($Path)) {
            foreach ($c in [IO.Directory]::GetFileSystemEntries($Path)) {
                if ($Exclude -contains [IO.Path]::GetFileName($c)) { if (-not (Test-SdExcluded $c)) { return $false }; continue }
                if (-not (Test-SdTree $c)) { return $false }
            }
        }
        return $true
    }
    catch { return $false }
}

if (-not (Test-Path Function:\New-SdDirSecurity)) {
    # SD каталога: владелец Administrators; SYSTEM и Administrators полный доступ, Users (кроме -NoUsers) чтение; без наследования.
    function New-SdDirSecurity([switch]$NoUsers, [switch]$NoOwner, [string[]]$ExtraSids = @()) {
        $sec = New-Object Security.AccessControl.DirectorySecurity
        # NSU_LLM_TEST_NO_SD_OWNER: только для тестов (без повышения владельцем Administrators назначить нельзя)
        if (-not $NoOwner -and -not $env:NSU_LLM_TEST_NO_SD_OWNER) { $sec.SetOwner((New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544')) }
        $sec.SetAccessRuleProtection($true, $false)
        $inh = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        $rules = New-Object System.Collections.ArrayList
        [void]$rules.Add(@('S-1-5-18', 'FullControl')); [void]$rules.Add(@('S-1-5-32-544', 'FullControl'))
        if (-not $NoUsers) { [void]$rules.Add(@('S-1-5-32-545', 'ReadAndExecute')) }
        foreach ($x in @($ExtraSids | Where-Object { $_ })) { [void]$rules.Add(@($x, 'FullControl')) }
        foreach ($r in $rules) {
            $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier $r[0]), $r[1], $inh, 'None', 'Allow')))
        }
        return $sec
    }
}
if (-not (Test-Path Function:\New-SdAdminOnlySecurity)) {
    # DACL файла с секретом: SYSTEM и Administrators, полный доступ, без наследования.
    function New-SdAdminOnlySecurity([string]$Path) {
        $sec = New-Object Security.AccessControl.FileSecurity
        $sec.SetAccessRuleProtection($true, $false)
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
            $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier $sid), 'FullControl', 'Allow')))
        }
        return $sec
    }
}
if (-not (Test-Path Function:\Move-SdAside)) {
    # Переименование (на том же томе — без копирования): старый объект и все дескрипторы к нему уходят вместе с ним.
    function Move-SdAside([string]$From, [string]$To) {
        if ([IO.Directory]::Exists($From)) { [IO.Directory]::Move($From, $To) } else { [IO.File]::Move($From, $To) }
    }
}

# Новый каталог сразу с SD. Уже есть: должен быть доверенным (иначе ошибка); его права не меняются.
function New-SecureDir([string]$Path, [switch]$NoUsers) {
    $Path = [IO.Path]::GetFullPath($Path)
    if (-not [IO.Directory]::Exists($Path)) { [void][IO.Directory]::CreateDirectory($Path, (New-SdDirSecurity -NoUsers:$NoUsers)) }
    if (-not (Test-SdItem $Path)) {
        throw (New-SdError "Каталог $Path создан не администратором (владелец $(Get-SdOwner $Path)) или с чужими правами на запись" "Удалите его от администратора (Remove-Item -Recurse -Force '$Path') и повторите.")
    }
}
# Убирает из DACL исключённого каталога Users, Everyone, Authenticated Users и CREATOR OWNER (собственные и унаследованные записи;
# каталог становится защищённым от наследования). Остальные записи, в том числе ВМ Hyper-V (S-1-5-83-*), остаются.
function Clear-SdExcluded([string]$p, [switch]$DryRun) {
    $acl = [IO.Directory]::GetAccessControl($p, 'Access')
    $strip = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $SdStripSids -contains $_.IdentityReference.Value })
    if (-not $strip.Count -and $acl.AreAccessRulesProtected) { return }
    if ($DryRun) { Write-Host "[dry-run] $p`: без наследования, убрать $(@($strip | ForEach-Object { $_.IdentityReference.Value } | Select-Object -Unique) -join ', ') (запись ВМ остаётся)"; return }
    $acl.SetAccessRuleProtection($true, $true)                      # унаследованное становится собственным
    [IO.Directory]::SetAccessControl($p, $acl)
    $acl = [IO.Directory]::GetAccessControl($p, 'Access')
    foreach ($r in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { if ($SdStripSids -contains $r.IdentityReference.Value) { [void]$acl.RemoveAccessRuleSpecific($r) } }
    [IO.Directory]::SetAccessControl($p, $acl)
}
function Get-SdStamp { return (Get-Date).ToString('yyyyMMdd-HHmmss') }

# Защищает каталог до записи в него. -DryRun печатает план и ничего не меняет.
function Secure-Dir([string]$Path, [string[]]$Exclude = @(), [switch]$DryRun, [switch]$NoUsers, [string[]]$ExtraSids = @()) {
    # -ExtraSids: SID пользователя, под которым Docker Desktop читает каталог для bind-mount (администратор без повышения: группа
    # Administrators в его токене «только для запрета» и записи на группу не действуют). Им выдаётся полный доступ и им доверяется.
    $global:SdExtraSids = @($ExtraSids | Where-Object { $_ })
    $Path = [IO.Path]::GetFullPath($Path)
    $exists = [IO.Directory]::Exists($Path) -or [IO.File]::Exists($Path)
    if ($exists -and (Test-SdReparse $Path)) {
        throw (New-SdError "$Path — ссылка (junction или symlink), а не каталог: её мог создать не администратор" "Проверьте, куда она ведёт (Get-Item '$Path' | Format-List *), удалите ссылку от администратора (cmd /c rmdir `"$Path`") и повторите.")
    }
    $excl = @($(if ([IO.Directory]::Exists($Path)) { $Exclude | ForEach-Object { Join-Path $Path $_ } | Where-Object { [IO.Directory]::Exists($_) -or [IO.File]::Exists($_) } }))
    foreach ($x in $excl) {
        if (Test-SdReparse $x) { throw (New-SdError "$x — ссылка (junction или symlink), а не каталог: её мог создать не администратор" "Удалите ссылку от администратора (cmd /c rmdir `"$x`") и повторите.") }
        if (-not (Test-SdExcluded $x)) { throw (New-SdError "Каталог $x принадлежит $(Get-SdOwner $x), а не администратору: его содержимое скрипт не трогает и доверять ему не может" "Проверьте его от администратора; если он не нужен, удалите (Remove-Item -Recurse -Force '$x') и повторите.") }
    }
    $aside = "$Path.untrusted-$(Get-SdStamp)"
    $plan = "владелец Administrators, SYSTEM и Administrators полный доступ, $(if ($NoUsers) { 'Users без доступа' } else { 'Users чтение' })$(if ($ExtraSids) { ', полный доступ ' + ($ExtraSids -join ',') }), без наследования от ProgramData$(if ($excl) { "; содержимое $(($excl | ForEach-Object { Split-Path -Leaf $_ }) -join ', ') не трогается" })"
    # Сам каталог недоверенный: откладывается целиком. С исключённым каталогом внутри (диск дистрибутива) переносить нельзя:
    # тогда допустим только каталог администратора, у которого чужие лишь права (наследование от ProgramData); они заменяются целиком.
    if ($exists -and -not (Test-SdItem $Path)) {
        $foreign = -not (Test-SdTrustedSid (Get-SdAcl $Path).GetOwner([Security.Principal.SecurityIdentifier]).Value)
        if ($excl -and $foreign) {
            throw (New-SdError "Каталог $Path принадлежит $(Get-SdOwner $Path), а внутри лежит $(($excl | ForEach-Object { Split-Path -Leaf $_ }) -join ', '): отложить его нельзя, чинить на месте небезопасно" "Остановите WSL (wsl --shutdown), перенесите данные от администратора и удалите каталог (Remove-Item -Recurse -Force '$Path'), затем повторите.")
        }
        if (-not $excl) {
            if ($DryRun) { Write-Host "[dry-run] каталог $Path создан или изменён не администратором: отложить его в $aside и создать заново ($plan)"; return }
            try { Move-SdAside $Path $aside }
            catch { throw (New-SdError "Каталог $Path создан или изменён не администратором, и отложить его в сторону не удалось: $($_.Exception.Message)" "Закройте программы, которые его держат, удалите его от администратора (Remove-Item -Recurse -Force '$Path') и повторите.") }
            Write-Host "Каталог $Path создан или изменён не администратором: отложен в $aside (его можно удалить), создан заново."
            $exists = $false
        }
    }
    if (-not $exists) {
        if ($DryRun) { Write-Host "[dry-run] создать каталог $Path сразу с правами: $plan"; return }
        [void][IO.Directory]::CreateDirectory($Path, (New-SdDirSecurity -NoUsers:$NoUsers -ExtraSids $ExtraSids))
        if (-not (Test-TrustedPath $Path $Exclude)) { throw (New-SdError "Каталог $Path появился раньше, чем установщик его создал (владелец $(Get-SdOwner $Path))" "Удалите его от администратора и повторите.") }
        return
    }
    # Каталог остаётся: недоверенное внутри (без исключённых) откладывается, оценка — до смены прав корня.
    $bad = @([IO.Directory]::GetFileSystemEntries($Path) | Where-Object { $Exclude -notcontains [IO.Path]::GetFileName($_) } | Where-Object { -not (Test-SdTree $_) })
    $quar = "$Path.untrusted-$(Get-SdStamp)"
    if ($DryRun) {
        Write-Host "[dry-run] защитить каталог $Path`: $plan (DACL заменяется целиком)"
        foreach ($b in $bad) { Write-Host "[dry-run] отложить $b в $quar (чужой владелец, чужие права на запись или ссылка)" }
        foreach ($x in $excl) { Clear-SdExcluded $x -DryRun }
        return
    }
    foreach ($x in $excl) { Clear-SdExcluded $x }                       # до смены прав корня: защищённый каталог их не наследует
    [IO.Directory]::SetAccessControl($Path, (New-SdDirSecurity -NoOwner -NoUsers:$NoUsers -ExtraSids $ExtraSids))   # DACL целиком, собственные записи ученика уходят
    if ($bad) {
        New-SecureDir $quar -NoUsers
        foreach ($b in $bad) {
            try { Move-SdAside $b (Join-Path $quar ([IO.Path]::GetFileName($b))) }
            catch { throw (New-SdError "$b создан или изменён не администратором, и отложить его в сторону не удалось: $($_.Exception.Message)" "Закройте программы, которые его держат, удалите его от администратора и повторите.") }
        }
        Write-Host "В $Path были объекты не администратора: отложены в $quar (его можно удалить)."
    }
    if (-not (Test-TrustedPath $Path $Exclude)) {
        throw (New-SdError "Каталог $Path не удалось защитить (владелец $(Get-SdOwner $Path); чужие права или объекты остались)" "Удалите его от администратора (Remove-Item -Recurse -Force '$Path') и повторите.")
    }
}
# Новый файл вместо старого: старый переименовывается и удаляется (дескриптор, открытый раньше, остаётся у старого файла),
# новый создаётся заново. -AdminOnly: DACL «SYSTEM и Administrators, полный доступ, без наследования» задаётся при создании.
function Write-SecureFile([string]$Path, [string]$Text = $null, [byte[]]$Bytes = $null, [switch]$AdminOnly) {
    if ($null -eq $Bytes) { $Bytes = (New-Object Text.UTF8Encoding $false).GetBytes([string]$Text) }
    if ([IO.File]::Exists($Path) -or [IO.Directory]::Exists($Path)) {
        if (Test-SdReparse $Path) { throw (New-SdError "$Path — ссылка, а не файл" "Удалите её от администратора и повторите.") }
        $old = "$Path.old-$([guid]::NewGuid().ToString('N'))"
        [IO.File]::Move($Path, $old)
        try { [IO.File]::Delete($old) } catch { }
    }
    $sec = $(if ($AdminOnly) { New-SdAdminOnlySecurity $Path } else { $null })
    $fs = New-Object IO.FileStream($Path, [IO.FileMode]::CreateNew, [Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $sec)
    try { $fs.Write($Bytes, 0, $Bytes.Length) } finally { $fs.Close() }
}
# Файл, уже скопированный во временное имя рядом, ставится на место (старый удаляется, как в Write-SecureFile).
function Move-SecureFileInto([string]$Temp, [string]$Path) {
    if ([IO.File]::Exists($Path) -or [IO.Directory]::Exists($Path)) {
        if (Test-SdReparse $Path) { throw (New-SdError "$Path — ссылка, а не файл" "Удалите её от администратора и повторите.") }
        $old = "$Path.old-$([guid]::NewGuid().ToString('N'))"
        [IO.File]::Move($Path, $old)
        try { [IO.File]::Delete($old) } catch { }
    }
    [IO.File]::Move($Temp, $Path)
}

# ------------------------------------------------------------------ корень CA главного по токену подключения (общая для install-node.ps1 и run-node.ps1)
# Токен K10<H>::node:<секрет> несёт H = sha256 ответа https://<главный>:6443/cacerts (CA сервера k3s, его же проверяет k3s agent).
# Главный подписывает точные байты своего /ca.crt этим CA (CMS DER, https://<главный>/ca.crt.p7s). Корень принимается, если H сошёлся,
# подпись верна и подписавший сертификат РАВЕН первому сертификату /cacerts (цепочки к CA k3s мало: он выпускает и сертификаты нод, их
# ключи лежат на самих нодах). Контракт с main/sign-ca.sh и node/rootfs/opt/llm/bin/fetch-main-ca.sh.
# Результат: Status = verified (Pem проверен) | mismatch (подмена, Why) | unverifiable (токен без K10 или главный не подписывает: решает
# отпечаток/подтверждение, Pem и Fp заполнены) | public (у главного нет /ca.crt: системные корни) | unreachable (нет связи).
function Get-CaPemSha256([string]$pem) {
    if ($pem -notmatch '(?s)-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----') { return '' }
    $der = [Convert]::FromBase64String(($matches[1] -replace '\s', ''))
    return ((([Security.Cryptography.SHA256]::Create().ComputeHash($der)) | ForEach-Object { $_.ToString('x2') }) -join '')
}
function Invoke-CaFetch([string]$url, [string]$curlExe) {
    $f = [IO.Path]::GetTempFileName()
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $code = "$(& $curlExe -sS -k --ssl-no-revoke --connect-timeout 5 -m 20 -o $f -w '%{http_code}' $url 2>$null)".Trim()
        $bytes = $(if (Test-Path -LiteralPath $f) { [IO.File]::ReadAllBytes($f) } else { [byte[]]@() })
        return [pscustomobject]@{ Code = $code; Bytes = $bytes }
    }
    finally { $ErrorActionPreference = $prev; Remove-Item -Force -LiteralPath $f -ErrorAction SilentlyContinue }
}
# $Fetch: scriptblock { param($url) } -> @{ Code; Bytes }; по умолчанию curl.exe -k (подмена нужна тестам)
function Test-MainCaByToken([string]$MainAddr, [string]$Token, [string]$CurlExe = 'curl.exe', [scriptblock]$Fetch = $null) {
    $res = [pscustomobject]@{ Status = 'unreachable'; Pem = ''; Fp = ''; Why = '' }
    if (-not $Fetch) { $Fetch = { param($u) Invoke-CaFetch $u $CurlExe } }
    $hostOnly = $MainAddr -replace ':\d+$', ''
    $ca = & $Fetch "https://$MainAddr/ca.crt"
    if ($ca.Code -eq '404') { $res.Status = 'public'; return $res }
    if ($ca.Code -ne '200' -or $ca.Bytes.Length -eq 0) { $res.Why = "https://$MainAddr/ca.crt недоступен (HTTP $($ca.Code))"; return $res }
    $res.Pem = [Text.Encoding]::UTF8.GetString($ca.Bytes)
    $res.Fp = Get-CaPemSha256 $res.Pem
    if (-not $res.Fp) { $res.Status = 'mismatch'; $res.Why = "https://$MainAddr/ca.crt не сертификат PEM"; $res.Pem = ''; return $res }
    $res.Status = 'unverifiable'
    if ($Token -notmatch '^K10([0-9a-f]{64})::') { $res.Why = 'токен без хеша CA (не вида K10<sha256>::...)'; return $res }
    $want = $matches[1]
    $k3s = & $Fetch "https://${hostOnly}:6443/cacerts"
    if ($k3s.Code -ne '200') { $res.Status = 'unreachable'; $res.Why = "https://${hostOnly}:6443/cacerts недоступен (HTTP $($k3s.Code))"; return $res }
    $got = ((([Security.Cryptography.SHA256]::Create().ComputeHash($k3s.Bytes)) | ForEach-Object { $_.ToString('x2') }) -join '')
    if ($got -ne $want) { $res.Status = 'mismatch'; $res.Why = "CA k3s главного не совпал с хешем в токене (получен sha256 $got, в токене $want): подмена в сети или токен от другого кластера"; $res.Pem = ''; return $res }
    $sig = & $Fetch "https://$MainAddr/ca.crt.p7s"
    # Токен K10 + нет подписи = не «старый главный», а возможная подмена: MITM на 443 ответил бы 404 только на .p7s. Отказ, без вопроса про отпечаток.
    if ($sig.Code -eq '404') { $res.Status = 'mismatch'; $res.Pem = ''; $res.Why = 'главный не отдал подпись /ca.crt.p7s при токене K10 (подмена в сети или главный старше подписи CA; главный подписывает в течение пары секунд после запуска k3s, повторите через минуту или обновите главный)'; return $res }
    if ($sig.Code -ne '200') { $res.Status = 'unreachable'; $res.Why = "https://$MainAddr/ca.crt.p7s недоступен (HTTP $($sig.Code))"; return $res }
    try {
        Add-Type -AssemblyName System.Security
        $first = [Text.Encoding]::UTF8.GetString($k3s.Bytes)
        if ($first -notmatch '(?s)-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----') { throw 'в /cacerts нет сертификата' }
        $k3sCert = New-Object Security.Cryptography.X509Certificates.X509Certificate2(,[Convert]::FromBase64String(($matches[1] -replace '\s', '')))
        $cms = New-Object Security.Cryptography.Pkcs.SignedCms((New-Object Security.Cryptography.Pkcs.ContentInfo(,$ca.Bytes)), $true)
        $cms.Decode($sig.Bytes)
        $cms.CheckSignature($true)
        if ($cms.SignerInfos.Count -lt 1 -or $cms.SignerInfos[0].Certificate.Thumbprint -ne $k3sCert.Thumbprint) { throw 'подписавший сертификат не равен CA k3s' }
    }
    catch {
        $res.Status = 'mismatch'; $res.Pem = ''
        $res.Why = "подпись https://$MainAddr/ca.crt не сходится с CA k3s главного ($(("$($_.Exception.Message)" -split "`r?`n")[0])): подмена в сети"
        return $res
    }
    $res.Status = 'verified'
    return $res
}
