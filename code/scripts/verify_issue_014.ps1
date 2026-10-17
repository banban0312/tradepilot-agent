param([switch]$LiveModel)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue014-api-sse-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example 并设置本机数据库参数。'
}

if ($LiveModel) {
    $modelSettings = @{}
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*(MODEL_PROVIDER|MODEL_NAME|MODEL_API_KEY)\s*=\s*(.*)$') {
            $modelSettings[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
        }
    }
    if ($modelSettings['MODEL_PROVIDER'] -ne 'deepseek' -or
        -not $modelSettings['MODEL_NAME'] -or
        -not $modelSettings['MODEL_API_KEY']) {
        throw '在线验证要求 backend/.env 配置 DeepSeek、模型名称和非空模型密钥；密钥不会显示。'
    }
}

$oldPostgresPort = [Environment]::GetEnvironmentVariable('POSTGRES_PORT', 'Process')
$oldApiPort = [Environment]::GetEnvironmentVariable('API_PORT', 'Process')
$oldMcpHostPort = [Environment]::GetEnvironmentVariable('MCP_HOST_PORT', 'Process')
$oldDemoMode = [Environment]::GetEnvironmentVariable('DEMO_MODE', 'Process')
$oldDemoPassword = [Environment]::GetEnvironmentVariable('DEMO_PASSWORD', 'Process')
$oldCookieSecure = [Environment]::GetEnvironmentVariable('SESSION_COOKIE_SECURE', 'Process')
$oldOnlineModel = [Environment]::GetEnvironmentVariable('TRADEPILOT_ONLINE_MODEL', 'Process')
$oldMcpSigningKey = [Environment]::GetEnvironmentVariable('MCP_CONTEXT_SIGNING_KEY', 'Process')

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return [string]$listener.LocalEndpoint.Port
    }
    finally {
        $listener.Stop()
    }
}

$env:POSTGRES_PORT = Get-FreeLocalPort
$env:API_PORT = Get-FreeLocalPort
$env:MCP_HOST_PORT = Get-FreeLocalPort
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:SESSION_COOKIE_SECURE = 'false'
$env:TRADEPILOT_ONLINE_MODEL = if ($LiveModel) { '1' } else { '0' }
if ($LiveModel) {
    $signingKeyBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($signingKeyBytes)
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($signingKeyBytes)
    [Array]::Clear($signingKeyBytes, 0, $signingKeyBytes.Length)
}

Push-Location $codeRoot
try {
    Write-Host "使用独立 Compose 项目 $projectName 验证任务 API、权限、幂等、草稿版本和 SSE。"
    & docker @composeArgs build api verify
    if ($LASTEXITCODE -ne 0) { throw "后端验证镜像构建失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs up --build --detach --wait db api
    if ($LASTEXITCODE -ne 0) { throw "隔离 PostgreSQL/API 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离示例账号初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_008.py /workspace/tests/issues/test_issue_013.py /workspace/tests/issues/test_issue_014.py
    if ($LASTEXITCODE -ne 0) { throw "任务恢复或任务 API/SSE 验收失败，退出码 $LASTEXITCODE。" }

    if ($LiveModel) {
        & docker @composeArgs up --build --detach --wait mcp worker
        if ($LASTEXITCODE -ne 0) { throw "隔离 MCP/Worker 启动失败，退出码 $LASTEXITCODE。" }
        & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_014_live.py
        if ($LASTEXITCODE -ne 0) { throw "DeepSeek 与 MCP 真实任务冒烟验收失败，退出码 $LASTEXITCODE。" }
    }
}
catch {
    Write-Host $_ -ForegroundColor Red
    if ($LiveModel) {
        & docker @composeArgs logs --no-color db api mcp worker
    }
    else {
        & docker @composeArgs logs --no-color db api
    }
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
    if ($null -eq $oldApiPort) { Remove-Item Env:API_PORT -ErrorAction SilentlyContinue }
    else { Set-Item Env:API_PORT $oldApiPort }
    if ($null -eq $oldMcpHostPort) { Remove-Item Env:MCP_HOST_PORT -ErrorAction SilentlyContinue }
    else { Set-Item Env:MCP_HOST_PORT $oldMcpHostPort }
    if ($null -eq $oldDemoMode) { Remove-Item Env:DEMO_MODE -ErrorAction SilentlyContinue }
    else { Set-Item Env:DEMO_MODE $oldDemoMode }
    if ($null -eq $oldDemoPassword) { Remove-Item Env:DEMO_PASSWORD -ErrorAction SilentlyContinue }
    else { Set-Item Env:DEMO_PASSWORD $oldDemoPassword }
    if ($null -eq $oldCookieSecure) { Remove-Item Env:SESSION_COOKIE_SECURE -ErrorAction SilentlyContinue }
    else { Set-Item Env:SESSION_COOKIE_SECURE $oldCookieSecure }
    if ($null -eq $oldOnlineModel) { Remove-Item Env:TRADEPILOT_ONLINE_MODEL -ErrorAction SilentlyContinue }
    else { Set-Item Env:TRADEPILOT_ONLINE_MODEL $oldOnlineModel }
    if ($null -eq $oldMcpSigningKey) { Remove-Item Env:MCP_CONTEXT_SIGNING_KEY -ErrorAction SilentlyContinue }
    else { Set-Item Env:MCP_CONTEXT_SIGNING_KEY $oldMcpSigningKey }
}
