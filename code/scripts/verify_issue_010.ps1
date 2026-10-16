$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue010-queries-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
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
    Write-Host "启动独立 PostgreSQL 和 MCP 服务，验证四类只读查询及历史回归：$projectName"
    & docker @composeArgs up --build --detach --wait api
    if ($LASTEXITCODE -ne 0) { throw "API/PostgreSQL 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离示例账号和主数据初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --detach --wait mcp
    if ($LASTEXITCODE -ne 0) { throw "MCP Streamable HTTP 服务启动或健康探针失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_005.py /workspace/tests/issues/test_issue_006.py /workspace/tests/issues/test_issue_008.py /workspace/tests/issues/test_issue_009.py /workspace/tests/issues/test_issue_010.py
    if ($LASTEXITCODE -ne 0) { throw "合同规则、严格 MCP 契约、身份上下文或只读查询验证失败，退出码 $LASTEXITCODE。" }

    Write-Host 'Issue #10 验证通过：真实 MCP/PostgreSQL 客户、物料、合同与余额查询，部门隔离、歧义确认、合同规则、草稿金额版本绑定、零业务写入及 #5/#6/#8/#9 回归均有断言。'
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
