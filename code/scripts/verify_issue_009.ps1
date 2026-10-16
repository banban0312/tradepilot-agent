$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue009-mcp-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose', '--project-name', $projectName,
    '--env-file', 'backend/.env', '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并设置本机数据库密码。'
}

$oldEnvironment = @{}
foreach ($name in @('DEMO_MODE', 'DEMO_PASSWORD', 'POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'MCP_CONTEXT_SIGNING_KEY')) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:POSTGRES_PORT = Get-FreeLoopbackPort
do { $env:API_PORT = Get-FreeLoopbackPort } while ($env:API_PORT -eq $env:POSTGRES_PORT)
do { $env:MCP_HOST_PORT = Get-FreeLoopbackPort } while ($env:MCP_HOST_PORT -in @($env:POSTGRES_PORT, $env:API_PORT))

Push-Location $codeRoot
try {
    Write-Host "启动独立 PostgreSQL 和 MCP 服务，验证严格 Schema、签名身份及真实 HTTP 轨迹：$projectName"
    & docker @composeArgs up --build --detach --wait api
    if ($LASTEXITCODE -ne 0) { throw "API/PostgreSQL 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离示例账号初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --detach --wait mcp
    if ($LASTEXITCODE -ne 0) { throw "MCP Streamable HTTP 服务启动或健康探针失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_006.py /workspace/tests/issues/test_issue_008.py /workspace/tests/issues/test_issue_009.py
    if ($LASTEXITCODE -ne 0) { throw "MCP 合约、数据库轨迹或依赖回归失败，退出码 $LASTEXITCODE。" }

    Write-Host 'Issue #9 集成验证通过：官方 Streamable HTTP 发现/健康探针、Schema 拒绝、短期签名、身份与 epoch 复核、未实现工具显式失败、脱敏轨迹、零业务副作用及断连错误均有实际断言。'
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
