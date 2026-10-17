$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue022-artifact-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) { throw '未找到 code/backend/.env；请先按 backend/.env.example 配置隔离验收所需数据库。' }

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'MCP_CONTEXT_SIGNING_KEY')
$oldEnvironment = @{}
foreach ($name in $environmentNames) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "隔离 Compose 命令失败，退出码 $LASTEXITCODE：$($Arguments -join ' ')" }
}

function Set-IsolatedEnvironment {
    $ports = @()
    while ($ports.Count -lt 4) {
        $candidate = Get-FreeLocalPort
        if ($candidate -notin $ports) { $ports += $candidate }
    }
    $env:POSTGRES_PORT = $ports[0]
    $env:API_PORT = $ports[1]
    $env:MCP_HOST_PORT = $ports[2]
    $env:WEB_PORT = $ports[3]
    $env:APP_ORIGIN = 'http://localhost:5173'
    $env:DEMO_MODE = 'true'
    $env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:TRADEPILOT_TEST_MODE = '1'
    $env:TRADEPILOT_MCP_INTEGRATION = '1'
    $key = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($key)
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($key)
    [Array]::Clear($key, 0, $key.Length)
}

try {
    Push-Location $codeRoot
    try {
        Set-IsolatedEnvironment
        Write-Host "使用独立 Compose 项目 $projectName 验收附件上传和权限。"
        Invoke-Compose @('build', 'api', 'verify')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'mcp', 'api')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'check', '--ignore', 'EXE002', '--config', 'lint.isort.known-first-party=["app"]', 'app', 'alembic/versions/c3218be6a410_input_artifact_storage.py', '/workspace/tests/issues/test_issue_022.py')
        $changedPython = @('app/main.py', 'app/config.py', 'app/models.py', 'app/artifacts/routes.py', 'app/storage/files.py', 'app/storage/cleanup.py', 'alembic/versions/c3218be6a410_input_artifact_storage.py', '/workspace/tests/issues/test_issue_022.py')
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'format', '--check') + $changedPython)
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'compileall', '-q', 'app', 'alembic/versions/c3218be6a410_input_artifact_storage.py', '/workspace/tests/issues/test_issue_022.py')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')
        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-q', '/workspace/tests/issues/test_issue_019.py', '/workspace/tests/issues/test_issue_022.py')
        Write-Host '附件验收通过：上传/稳定哈希/原文下载/元数据脱敏/跨部门拒绝/损坏、加密及超页 PDF 拒绝。'
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --no-color db api mcp
        throw
    }
    finally {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) { Write-Warning "随机验收项目 $projectName 清理失败；仅清理该项目的容器和卷。" }
        Pop-Location
    }
}
finally {
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
