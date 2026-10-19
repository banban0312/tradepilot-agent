$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue026-excel-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
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

        Write-Host "使用独立项目 $projectName、隔离数据库和附件卷验收 Excel 模板与分组解析；不会重启或清理主开发服务。"
        Invoke-Compose @('build', 'api')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'mcp', 'api')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        $pythonFiles = @('app/imports/routes.py', 'app/imports/service.py', 'app/main.py', '/workspace/tests/issues/test_issue_026.py')
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'check', '--ignore', 'EXE002', '--config', 'lint.isort.known-first-party=["app"]') + $pythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'format', '--check') + $pythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'compileall', '-q') + $pythonFiles)
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')
        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-x', '-vv', '/workspace/tests/issues/test_issue_010.py', '/workspace/tests/issues/test_issue_018.py', '/workspace/tests/issues/test_issue_022.py', '/workspace/tests/issues/test_issue_023.py', '/workspace/tests/issues/test_issue_024.py', '/workspace/tests/issues/test_issue_025.py', '/workspace/tests/issues/test_issue_026.py')
        Write-Host 'Excel 导入验收通过：模板下载、同组多行汇总、原单元格证据、主档冲突、数字/公式校验、额度超额状态、部门鉴权及无订单副作用。'
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
