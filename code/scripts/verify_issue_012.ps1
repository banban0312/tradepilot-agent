param(
    [switch]$OfflineOnly
)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue012-fc-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose', '--project-name', $projectName,
    '--env-file', 'backend/.env', '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并设置本机数据库密码。'
}

$oldEnvironment = @{}
foreach ($name in @('DEMO_MODE', 'DEMO_PASSWORD', 'POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'MCP_CONTEXT_SIGNING_KEY', 'TRADEPILOT_MCP_INTEGRATION', 'TRADEPILOT_ONLINE_MODEL')) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

$modelValues = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^\s*(MODEL_PROVIDER|MODEL_NAME|MODEL_API_KEY)\s*=\s*(.*?)\s*$') {
        $modelValues[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
$onlineModelConfigured = (
    $modelValues['MODEL_PROVIDER'] -in @('openai', 'deepseek') -and
    -not [string]::IsNullOrWhiteSpace($modelValues['MODEL_NAME']) -and
    -not [string]::IsNullOrWhiteSpace($modelValues['MODEL_API_KEY'])
)
$runOnlineModel = $onlineModelConfigured -and -not $OfflineOnly
$modelValues.Clear()

$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:TRADEPILOT_MCP_INTEGRATION = '1'
$env:TRADEPILOT_ONLINE_MODEL = if ($runOnlineModel) { '1' } else { '0' }
$env:POSTGRES_PORT = Get-FreeLoopbackPort
do { $env:API_PORT = Get-FreeLoopbackPort } while ($env:API_PORT -eq $env:POSTGRES_PORT)
do { $env:MCP_HOST_PORT = Get-FreeLoopbackPort } while ($env:MCP_HOST_PORT -in @($env:POSTGRES_PORT, $env:API_PORT))

Push-Location $codeRoot
try {
    Write-Host "使用独立 Compose 项目 $projectName 验证真实 MCP 链路与预算控制。"
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

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_008.py /workspace/tests/issues/test_issue_009.py /workspace/tests/issues/test_issue_010.py /workspace/tests/issues/test_issue_011.py /workspace/tests/issues/test_issue_012.py
    if ($LASTEXITCODE -ne 0) { throw "工具契约、真实 MCP、草稿幂等或 Function Calling 验证失败，退出码 $LASTEXITCODE。" }

    if ($runOnlineModel) {
        Write-Host '在线模型验收已执行：请确认 pytest 输出中的在线测试通过；此调用使用隔离示例数据，并按模型供应商计费规则产生用量。'
    }
    elseif ($OfflineOnly -and $onlineModelConfigured) {
        Write-Warning '已通过 -OfflineOnly 禁用在线模型验收；真实模型工具选择仍未验证。'
    }
    else {
        Write-Warning '在线模型验收未执行：MODEL_PROVIDER、MODEL_NAME、MODEL_API_KEY 尚未完整配置。离线测试和真实 MCP 联调不代表在线模型工具选择通过。'
    }
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；只对此项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location
    foreach ($name in $oldEnvironment.Keys) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
