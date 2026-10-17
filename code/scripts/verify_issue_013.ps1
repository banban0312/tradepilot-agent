$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue013-graph-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example 并设置本机数据库参数。'
}

$oldPostgresPort = [Environment]::GetEnvironmentVariable('POSTGRES_PORT', 'Process')
$oldDemoMode = [Environment]::GetEnvironmentVariable('DEMO_MODE', 'Process')
$oldDemoPassword = [Environment]::GetEnvironmentVariable('DEMO_PASSWORD', 'Process')
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
try { $listener.Start(); $env:POSTGRES_PORT = [string]$listener.LocalEndpoint.Port }
finally { $listener.Stop() }
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')

Push-Location $codeRoot
try {
    Write-Host "使用独立 Compose 项目 $projectName 验证 PostgreSQL checkpoint 与图恢复。"
    & docker @composeArgs build api verify
    if ($LASTEXITCODE -ne 0) { throw "后端验证镜像构建失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs up --build --detach --wait db
    if ($LASTEXITCODE -ne 0) { throw "隔离 PostgreSQL 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离示例账号初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_008.py /workspace/tests/issues/test_issue_013.py
    if ($LASTEXITCODE -ne 0) { throw "任务恢复或 LangGraph PostgreSQL 验收失败，退出码 $LASTEXITCODE。" }
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color db
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；只对此项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location
    if ($null -eq $oldPostgresPort) { Remove-Item Env:POSTGRES_PORT -ErrorAction SilentlyContinue }
    else { Set-Item Env:POSTGRES_PORT $oldPostgresPort }
    if ($null -eq $oldDemoMode) { Remove-Item Env:DEMO_MODE -ErrorAction SilentlyContinue }
    else { Set-Item Env:DEMO_MODE $oldDemoMode }
    if ($null -eq $oldDemoPassword) { Remove-Item Env:DEMO_PASSWORD -ErrorAction SilentlyContinue }
    else { Set-Item Env:DEMO_PASSWORD $oldDemoPassword }
}
