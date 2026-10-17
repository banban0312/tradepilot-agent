$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue020-master-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose', '--project-name', $projectName,
    '--env-file', 'backend/.env', '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请复制 backend/.env.example，并设置本机数据库密码。'
}

$oldEnvironment = @{}
foreach ($name in @('DEMO_MODE', 'DEMO_PASSWORD', 'POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'WEB_PORT', 'MCP_CONTEXT_SIGNING_KEY', 'APP_ORIGIN', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION')) {
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
$env:APP_ORIGIN = 'http://localhost:5173'
$env:SESSION_COOKIE_SECURE = 'false'
$env:TRADEPILOT_TEST_MODE = '1'
$env:TRADEPILOT_MCP_INTEGRATION = '1'
$env:POSTGRES_PORT = Get-FreeLoopbackPort
do { $env:API_PORT = Get-FreeLoopbackPort } while ($env:API_PORT -eq $env:POSTGRES_PORT)
do { $env:MCP_HOST_PORT = Get-FreeLoopbackPort } while ($env:MCP_HOST_PORT -in @($env:POSTGRES_PORT, $env:API_PORT))
do { $env:WEB_PORT = Get-FreeLoopbackPort } while ($env:WEB_PORT -in @($env:POSTGRES_PORT, $env:API_PORT, $env:MCP_HOST_PORT))

Push-Location $codeRoot
try {
    Write-Host "启动本轮专用 PostgreSQL/API，验证主数据权限、合同额度、预检、别名与审计：$projectName"
    & docker @composeArgs up --build --detach --wait api
    if ($LASTEXITCODE -ne 0) { throw "API/PostgreSQL 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离示例账号初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --build --detach --wait mcp web
    if ($LASTEXITCODE -ne 0) { throw "MCP 或前端代理服务启动/健康探针失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_003.py /workspace/tests/issues/test_issue_005.py /workspace/tests/issues/test_issue_010.py /workspace/tests/issues/test_issue_020.py
    if ($LASTEXITCODE -ne 0) { throw "主数据及合同规则集成验证失败，退出码 $LASTEXITCODE。" }

    Write-Host 'Issue #20 已验证：管理员权限、CSRF、余额只读与总额下限、停用合同最终事务阻断、正式订单快照不受物料更新影响、别名歧义、版本冲突和审计均有真实数据库/API 断言。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；可仅对该项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location
    foreach ($name in $oldEnvironment.Keys) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
