$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue025-material-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$imageName = "tradepilot/backend:verify-$projectName"
$overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-$projectName.yaml"
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml', '--file', $overrideFile)

if (-not (Test-Path -LiteralPath $envFile)) { throw '未找到 code/backend/.env；请先按 backend/.env.example 配置本机验收环境。' }

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'MCP_CONTEXT_SIGNING_KEY')
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
    while ($ports.Count -lt 3) {
        $candidate = Get-FreeLocalPort
        if ($candidate -notin $ports) { $ports += $candidate }
    }
    $env:POSTGRES_PORT = $ports[0]
    $env:API_PORT = $ports[1]
    $env:MCP_HOST_PORT = $ports[2]
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
        @(
            'services:',
            "  api:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
            "  worker:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
            "  mcp:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
            "  verify:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''"
        ) -join "`n" | Set-Content -LiteralPath $overrideFile -Encoding utf8
        Write-Host "使用独立项目 $projectName、独立数据库与附件卷验收物料候选；不重启或清理主开发服务。"
        Invoke-Compose @('build', 'api')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'mcp', 'api')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        $pythonFiles = @('app/tasks/routes.py', 'app/mcp_server/queries.py', '/workspace/tests/issues/test_issue_025.py')
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'check', '--ignore', 'EXE002', '--config', 'lint.isort.known-first-party=["app"]') + $pythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'format', '--check') + $pythonFiles)
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'compileall', '-q', 'app/tasks/routes.py', 'app/mcp_server/queries.py', '/workspace/tests/issues/test_issue_025.py')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')
        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-x', '-vv', '/workspace/tests/issues/test_issue_010.py', '/workspace/tests/issues/test_issue_018.py', '/workspace/tests/issues/test_issue_023.py', '/workspace/tests/issues/test_issue_024.py', '/workspace/tests/issues/test_issue_025.py')
        Write-Host '物料候选验收通过：规格歧义不自动选择、跨部门拒绝、用户拒绝审计、真实候选校验、字段来源与确认人、草稿新版本、旧审批失效及正式订单零新增。'
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --no-color db api mcp
        throw
    }
    finally {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) { Write-Warning "隔离项目 $projectName 清理失败；可仅按该项目名清理其容器和卷。" }
        & docker image rm $imageName 2>$null | Out-Null
        Pop-Location
    }
}
finally {
    Remove-Item -LiteralPath $overrideFile -ErrorAction SilentlyContinue
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
