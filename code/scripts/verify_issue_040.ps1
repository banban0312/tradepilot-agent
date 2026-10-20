param()

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue040-regression-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$imageName = "tradepilot/backend:verify-$projectName"
$reportRoot = Join-Path $codeRoot 'artifacts/evals'
$reportPath = Join-Path $reportRoot $projectName
$reportDirectory = "/workspace/artifacts/evals/$projectName"
$overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-$projectName.yaml"
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml', '--file', $overrideFile)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env；请先按 backend/.env.example 配置本机验收环境。'
}

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_ALLOW_SELF_APPROVAL', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'MCP_CONTEXT_SIGNING_KEY', 'RUNTIME_CONFIG_ENCRYPTION_KEY', 'EVAL_API_URL', 'EVAL_DATABASE_HOST', 'EVAL_PROJECT_NAME', 'TRADEPILOT_EVAL_SANDBOX')
$oldEnvironment = @{}
foreach ($name in $environmentNames) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

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

try {
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
    $env:DEMO_ALLOW_SELF_APPROVAL = 'false'
    $env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:TRADEPILOT_TEST_MODE = '1'
    $env:TRADEPILOT_MCP_INTEGRATION = '1'
    $env:EVAL_API_URL = 'http://api:8000'
    $env:EVAL_DATABASE_HOST = 'db'
    $env:EVAL_PROJECT_NAME = $projectName
    $env:TRADEPILOT_EVAL_SANDBOX = '1'
    $key = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($key)
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($key)
    $env:RUNTIME_CONFIG_ENCRYPTION_KEY = [Convert]::ToBase64String($key).Replace('+', '-').Replace('/', '_')
    [Array]::Clear($key, 0, $key.Length)

    @(
        'services:',
        "  api:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
        "  mcp:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
        "  verify:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''`n      EVAL_API_URL: $env:EVAL_API_URL`n      EVAL_DATABASE_HOST: $env:EVAL_DATABASE_HOST`n      EVAL_PROJECT_NAME: $env:EVAL_PROJECT_NAME`n      TRADEPILOT_EVAL_SANDBOX: '1'`n      PYTHONPATH: /workspace:/app`n      MCP_CONTEXT_SIGNING_KEY: $env:MCP_CONTEXT_SIGNING_KEY`n      RUNTIME_CONFIG_ENCRYPTION_KEY: $env:RUNTIME_CONFIG_ENCRYPTION_KEY`n    volumes:`n      - ./artifacts/evals:/workspace/artifacts/evals"
    ) -join "`n" | Set-Content -LiteralPath $overrideFile -Encoding utf8

    Push-Location $codeRoot
    try {
        Write-Host "使用独立 Compose 项目 $projectName；不重启、清理或写入现有开发服务。"
        if (Test-Path -LiteralPath $reportPath) {
            throw "评测证据目录已存在，停止以保护现有报告：$reportPath"
        }
        New-Item -ItemType Directory -Path $reportPath | Out-Null
        Invoke-Compose @('config', '--quiet')
        Invoke-Compose @('build', 'api')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'mcp', 'api')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')

        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'evals.runner', '--mode', 'offline', '--dataset', 'v1', '--report-dir', $reportDirectory)
        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-c', "import json; from pathlib import Path; p=Path('$reportDirectory/summary.json'); s=json.loads(p.read_text()); assert s['case_count']==41 and s['status_counts'].get('PASS')==41 and sum(s['status_counts'].values())==41; print('固定离线案例：41/41 PASS')")

        $regressionFiles = @(
            '/workspace/tests/issues/test_issue_009.py',
            '/workspace/tests/issues/test_issue_010.py',
            '/workspace/tests/issues/test_issue_008.py',
            '/workspace/tests/issues/test_issue_013.py',
            '/workspace/tests/issues/test_issue_014.py',
            '/workspace/tests/issues/test_issue_015.py',
            '/workspace/tests/issues/test_issue_016.py',
            '/workspace/tests/issues/test_issue_017.py',
            '/workspace/tests/issues/test_issue_020.py',
            '/workspace/tests/issues/test_issue_022.py',
            '/workspace/tests/issues/test_issue_023.py',
            '/workspace/tests/issues/test_issue_024.py',
            '/workspace/tests/issues/test_issue_025.py',
            '/workspace/tests/issues/test_issue_029.py',
            '/workspace/tests/issues/test_issue_037.py',
            '/workspace/tests/issues/test_issue_038.py',
            '/workspace/tests/issues/test_issue_040.py'
        )
        Invoke-Compose (@('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-q', '-x') + $regressionFiles)
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --no-color db eval-db api mcp 2>$null
        throw
    }
    finally {
        try {
            Invoke-Compose @('down', '--volumes', '--remove-orphans')
        }
        catch {
            Write-Warning "隔离项目 $projectName 清理失败；只清理此项目关联资源。"
        }
        finally {
            & docker image rm $imageName 2>$null | Out-Null
            Pop-Location
        }
    }
}
finally {
    Remove-Item -LiteralPath $overrideFile -ErrorAction SilentlyContinue
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
