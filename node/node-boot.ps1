# Задача планировщика NSU-LLM-Node (её создаёт install-node.ps1): при загрузке Windows, без входа пользователя,
# поднимает дистрибутив WSL с нодой и держит его запущенным. Docker стартует через systemd, контейнер llm-node —
# по своей политике --restart unless-stopped. Пока жив процесс wsl.exe с `sleep infinity`, WSL не останавливает
# дистрибутив по простою; если его остановили (wsl --shutdown), цикл поднимает его снова.
param(
    [string]$Distro = 'NSU-LLM',
    [string]$Log = '',
    [int]$MaxRuns = 0,      # 0 — бесконечно (для тестов: 1)
    [int]$PauseSec = 15
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

$run = 0
while ($MaxRuns -eq 0 -or $run -lt $MaxRuns) {
    $run++
    Write-Log "запуск дистрибутива $Distro (попытка $run, пользователь $env:USERNAME)"
    $out = (& wsl -d $Distro -u root -e systemctl start docker 2>&1 | ForEach-Object { ("$_" -replace "`0", '') }) -join ' '
    if ($LASTEXITCODE -eq 0) { Write-Log 'docker запущен' } else { Write-Log "docker не запустился (код $LASTEXITCODE): $out" }
    & wsl -d $Distro -u root -e sleep infinity 2>&1 | Out-Null
    Write-Log "дистрибутив $Distro остановлен (код $LASTEXITCODE), повтор через $PauseSec с"
    if ($MaxRuns -eq 0 -or $run -lt $MaxRuns) { Start-Sleep -Seconds $PauseSec }
}
