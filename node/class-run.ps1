param([string]$Params, [string]$Log, [string]$Done)
# Единый исполнитель разовой задачи планировщика (install-node.ps1 -ViaTask, class-deploy.ps1 -RunMode task).
# Задача входит по паролю (пакетный вход): у неё есть профиль и учётные данные, поэтому WSL, DPAPI и Register-ScheduledTask
# работают, как при обычном входе. -Params — файл JSON {Script, Args, EnvProxy} под DPAPI LocalMachine с доступом только
# Administrators и SYSTEM; он удаляется сразу после чтения. Вывод скрипта построчно дописывается в -Log (без переноса длинных
# строк), код завершения — в -Done; -Done пишется всегда, по нему вызывающий отличает «упало» от «не запустилось».
$ErrorActionPreference = 'Stop'
$enc = New-Object Text.UTF8Encoding $false
$code = 1
try {
    Add-Type -AssemblyName System.Security
    $raw = [IO.File]::ReadAllBytes($Params)
    Remove-Item -LiteralPath $Params -Force
    $cfg = $enc.GetString([Security.Cryptography.ProtectedData]::Unprotect($raw, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)) | ConvertFrom-Json
    $a = @{}
    foreach ($pr in $cfg.Args.PSObject.Properties) { $a[$pr.Name] = $pr.Value }
    if ($cfg.EnvProxy) { $env:HTTP_PROXY = $cfg.EnvProxy; $env:HTTPS_PROXY = $cfg.EnvProxy }
    $global:LASTEXITCODE = $null
    & $cfg.Script @a *>&1 | ForEach-Object { [IO.File]::AppendAllText($Log, "$_`r`n", $enc) }
    $code = $(if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 })
}
catch { [IO.File]::AppendAllText($Log, "ОШИБКА: $($_.Exception.Message)`r`n", $enc); $code = 1 }
finally { [IO.File]::WriteAllText($Done, "$code") }
exit $code
