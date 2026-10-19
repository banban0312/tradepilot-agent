param([switch]$IssueOnly, [switch]$Issue030)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue029-batch-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
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

        Write-Host "使用隔离项目 $projectName、独立数据库和附件卷；不启动 Worker、不调用在线模型，也不会重启或清理主开发服务。"
        & npm --prefix frontend run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码 $LASTEXITCODE。" }
        Invoke-Compose @('build', 'api')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'mcp', 'api')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        $pythonFiles = @('app/imports/batches.py', 'app/imports/routes.py', 'app/models.py', 'alembic/versions/f4c91b62a7d3_import_batches.py', '/workspace/tests/issues/test_issue_029.py')
        if ($Issue030) { $pythonFiles += @('alembic/versions/f5b0374c9a1d_import_batch_item_editing.py', '/workspace/tests/issues/test_issue_030.py') }
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'check', '--ignore', 'EXE002', '--config', 'lint.isort.known-first-party=["app"]') + $pythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'format', '--check') + $pythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'compileall', '-q') + $pythonFiles)
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')
        $testFiles = @('/workspace/tests/issues/test_issue_029.py')
        if ($Issue030) { $testFiles += '/workspace/tests/issues/test_issue_030.py' }
        if (-not $IssueOnly) {
            $testFiles = @('/workspace/tests/issues/test_issue_010.py', '/workspace/tests/issues/test_issue_017.py', '/workspace/tests/issues/test_issue_018.py', '/workspace/tests/issues/test_issue_022.py', '/workspace/tests/issues/test_issue_023.py', '/workspace/tests/issues/test_issue_024.py', '/workspace/tests/issues/test_issue_025.py', '/workspace/tests/issues/test_issue_026.py', '/workspace/tests/issues/test_issue_027.py', '/workspace/tests/issues/test_issue_028.py', '/workspace/tests/issues/test_issue_029.py')
        }
        Invoke-Compose (@('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-x', '-vv', '-s') + $testFiles)
        if ($IssueOnly) {
            if ($Issue030) { Write-Host 'M5-01/M5-02 单项验收通过：批次持久化、组内修正、乐观版本、5 路并发和停止边界均完成。' }
            else { Write-Host 'M5-01 单项验收通过：批次父子持久化、Excel 源行关联、幂等、5 路并发和停止边界均完成。' }
        }
        else {
            Write-Host 'M5-01 验收通过：批次父子持久化、Excel 源行关联、重复请求幂等、5 路并发、停止边界及既有回归均完成。'
        }
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --no-color db api mcp
        throw
    }
    finally {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) { Write-Warning "隔离项目 $projectName 清理失败；仅清理该项目关联资源即可。" }
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
