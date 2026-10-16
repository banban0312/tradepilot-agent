$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue008-queue-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose', '--project-name', $projectName,
    '--env-file', 'backend/.env', '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并设置本机数据库密码。'
}

$oldEnvironment = @{}
foreach ($name in @('DEMO_MODE', 'DEMO_PASSWORD', 'POSTGRES_PORT', 'API_PORT')) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:POSTGRES_PORT = Get-FreeLoopbackPort
do { $env:API_PORT = Get-FreeLoopbackPort } while ($env:API_PORT -eq $env:POSTGRES_PORT)

Push-Location $codeRoot
try {
    Write-Host "启动隔离 PostgreSQL，验证持久命令、任务锁、epoch 与检查点恢复：$projectName"
    & docker @composeArgs up --build --detach --wait api
    if ($LASTEXITCODE -ne 0) { throw "API/PostgreSQL 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移不一致，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离示例账号初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --detach --wait worker
    if ($LASTEXITCODE -ne 0) { throw "持久 Worker 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_006.py /workspace/tests/issues/test_issue_008.py
    if ($LASTEXITCODE -ne 0) { throw "任务队列或可观测性回归失败，退出码 $LASTEXITCODE。" }

    Write-Host 'Issue #8 集成验证通过：采销任务隔离、request_id 重放、FOR UPDATE SKIP LOCKED 领取、心跳候选与 advisory lock 接管、execution_epoch 防旧写入及真实 PostgreSQL LangGraph 检查点均有断言。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；仅对此项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location
    foreach ($name in $oldEnvironment.Keys) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
