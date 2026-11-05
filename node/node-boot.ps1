# Задача планировщика NSU-LLM-Node (её создаёт install-node.ps1): при загрузке Windows, без входа пользователя,
# поднимает дистрибутив WSL с нодой и держит его запущенным. Docker стартует через systemd, контейнер llm-node —
# по своей политике --restart unless-stopped. Пока жив процесс wsl.exe с `sleep infinity`, WSL не останавливает
# дистрибутив по простою; если его остановили (wsl --shutdown), цикл поднимает его снова.
# Пока дистрибутив работает, раз в -UpdateEverySec секунд (по умолчанию 60) вызывается run-node.ps1 -Update: он спрашивает главного,
# не пора ли заменить контейнер llm-node новым образом (кнопка «Обновить» на странице «Ноды», NODE_AUTO_UPDATE на главном), и если
# пора, скачивает образ с главного и создаёт контейнер заново теми же параметрами. Обновлять нечего: ничего не происходит и ничего
# не пишется в журнал. 0 отключает проверку.
# Там же раз в -UpdateEverySec секунд вызывается run-node.ps1 -SyncFirewall: главный называет ноды текущей группы split (rpc_peers), и
# правила брандмауэра Windows и Hyper-V для 18050 (RPC без аутентификации) приводятся к «главный и эти ноды». В журнал пишется
# только изменение.
param(
    [string]$Distro = 'NSU-LLM',
    [string]$Log = '',
    [int]$MaxRuns = 0,      # 0 — бесконечно (для тестов: 1)
    [int]$PauseSec = 15,
    [int]$UpdateEverySec = 60
)
$ErrorActionPreference = 'Continue'
$env:WSL_UTF8 = '1'
if (-not $Log) { $Log = Join-Path (Split-Path -Parent $PSScriptRoot) 'boot.log' }

function Write-Log([string]$m) {
    try {
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 1MB) { Move-Item -Force $Log "$Log.1" }
        Add-Content -Path $Log -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $m) -Encoding UTF8
    }
    catch { }
}

# Один круг проверки обновления. Повтор одного и того же ответа («обновление не требуется», «главный не ответил») в журнал
# не пишется: в журнале видно, что изменилось, а не одна и та же строка каждую минуту.
$script:LastUpdateOut = ''
function Invoke-NodeUpdate {
    $runNode = Join-Path $PSScriptRoot 'run-node.ps1'
    if (-not (Test-Path -LiteralPath $runNode)) { return }
    $out = ''
    try { $out = (@(& $runNode -Update -Wsl $Distro *>&1 | ForEach-Object { ("$_" -replace "`0", '').Trim() } | Where-Object { $_ }) -join ' | ') }
    catch { $out = "обновление не удалось: $($_.Exception.Message)" }
    if ($out -and $out -ne $script:LastUpdateOut) { Write-Log "обновление: $out" }
    $script:LastUpdateOut = $out
}

# Один круг синхронизации брандмауэра (RPC split только для нод текущей группы): повтор того же ответа в журнал не пишется.
$script:LastFwOut = ''
function Invoke-FirewallSync {
    $runNode = Join-Path $PSScriptRoot 'run-node.ps1'
    if (-not (Test-Path -LiteralPath $runNode)) { return }
    $out = ''
    try { $out = (@(& $runNode -SyncFirewall -Wsl $Distro *>&1 | ForEach-Object { ("$_" -replace "`0", '').Trim() } | Where-Object { $_ }) -join ' | ') }
    catch { $out = "синхронизация брандмауэра не удалась: $($_.Exception.Message)" }
    if ($out -and $out -ne $script:LastFwOut) { Write-Log "брандмауэр: $out" }
    $script:LastFwOut = $out
}

$run = 0
while ($MaxRuns -eq 0 -or $run -lt $MaxRuns) {
    $run++
    Write-Log "запуск дистрибутива $Distro (попытка $run, пользователь $env:USERNAME)"
    $out = (& wsl -d $Distro -u root -e systemctl start docker 2>&1 | ForEach-Object { ("$_" -replace "`0", '') }) -join ' '
    if ($LASTEXITCODE -eq 0) { Write-Log 'docker запущен' } else { Write-Log "docker не запустился (код $LASTEXITCODE): $out" }
    # Процесс, который держит дистрибутив, живёт отдельно: пока он идёт, этот цикл проверяет обновления.
    $keep = $null
    try {
        $wslExe = (Get-Command wsl -ErrorAction Stop).Source
        $keep = Start-Process -FilePath $wslExe -ArgumentList @('-d', $Distro, '-u', 'root', '-e', 'sleep', 'infinity') -WindowStyle Hidden -PassThru
    }
    catch { Write-Log "не удалось запустить удержание дистрибутива отдельным процессом: $($_.Exception.Message)" }
    if ($keep) {
        while (-not $keep.WaitForExit($(if ($UpdateEverySec -gt 0) { $UpdateEverySec * 1000 } else { 60000 }))) {
            if ($UpdateEverySec -gt 0) { Invoke-NodeUpdate; Invoke-FirewallSync }
        }
        $code = $keep.ExitCode
    }
    else {
        & wsl -d $Distro -u root -e sleep infinity 2>&1 | Out-Null
        $code = $LASTEXITCODE
    }
    Write-Log "дистрибутив $Distro остановлен (код $code), повтор через $PauseSec с"
    if ($MaxRuns -eq 0 -or $run -lt $MaxRuns) { Start-Sleep -Seconds $PauseSec }
}
