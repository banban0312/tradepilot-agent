param(
    [string[]]$CaseId = @(),
    [string[]]$Category = @(),
    [ValidateSet("offline", "online")]
    [string]$Mode = "offline"
)

$ErrorActionPreference = "Stop"
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = Split-Path -Parent $codeRoot
$envFile = Join-Path $codeRoot "backend/.env"
$projectName = "tp-issue037-" + [Guid]::NewGuid().ToString("N").ToLowerInvariant()
$backendImageName = "tradepilot/backend:verify-$projectName"
$overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-$projectName.yaml"
$reportRoot = Join-Path $codeRoot "artifacts/evals"
$composeArgs = @(
    "compose", "--project-name", $projectName,
    "--env-file", "backend/.env",
    "--file", "compose.yaml",
    "--file", $overrideFile
)
$environmentNames = @(
    "POSTGRES_PORT", "API_PORT", "MCP_HOST_PORT", "APP_ORIGIN",
    "DEMO_MODE", "DEMO_PASSWORD", "DEMO_ALLOW_SELF_APPROVAL",
    "SESSION_COOKIE_SECURE", "MCP_CONTEXT_SIGNING_KEY", "EVAL_PROJECT_NAME",
    "PYTHONPATH"
)
$oldEnvironment = @{}
foreach ($name in $environmentNames) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "隔离 Compose 命令失败，退出码 $LASTEXITCODE：$($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "未找到 $envFile；需要本机 Compose 数据库配置。"
}

try {
    Push-Location $codeRoot
    $ports = @()
    while ($ports.Count -lt 3) {
        $candidate = Get-FreeLocalPort
        if ($candidate -notin $ports) { $ports += $candidate }
    }
    $env:POSTGRES_PORT = $ports[0]
    $env:API_PORT = $ports[1]
    $env:MCP_HOST_PORT = $ports[2]
    $env:APP_ORIGIN = "http://localhost"
    $env:DEMO_MODE = "true"
    $env:DEMO_ALLOW_SELF_APPROVAL = "false"
    $env:DEMO_PASSWORD = "TP-Eval-" + [Guid]::NewGuid().ToString("N")
    $env:SESSION_COOKIE_SECURE = "false"
    $env:EVAL_PROJECT_NAME = $projectName
    $env:PYTHONPATH = "backend;."
    $key = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($key)
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($key)
    [Array]::Clear($key, 0, $key.Length)

    @"
services:
  api:
    image: $backendImageName
    environment:
      MCP_URL: http://mcp:8000/mcp
  mcp:
    image: $backendImageName
    environment:
      MCP_URL: http://127.0.0.1:8000/mcp
  verify:
    image: $backendImageName
    environment:
      PYTHONPATH: /workspace:/app
      EVAL_API_URL: http://api:8000
      EVAL_PROJECT_NAME: $projectName
      TRADEPILOT_EVAL_SANDBOX: "1"
      APP_ORIGIN: http://localhost
      SESSION_COOKIE_SECURE: "false"
      DEMO_MODE: "true"
      DEMO_PASSWORD: $($env:DEMO_PASSWORD)
      MCP_CONTEXT_SIGNING_KEY: $($env:MCP_CONTEXT_SIGNING_KEY)
      MCP_URL: http://mcp:8000/mcp
    volumes:
      - ./artifacts/evals:/workspace/artifacts/evals
"@ | Set-Content -LiteralPath $overrideFile -Encoding utf8

    New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
    Push-Location $repoRoot
    try {
        Write-Host "运行 Issue #37 服务测试，并创建独立 Compose 项目 $projectName。"
        uv run --frozen --project backend --directory code python -m pytest -q tests/issues/test_issue_037.py tests/issues/test_issue_036.py
        if ($LASTEXITCODE -ne 0) { throw "评测服务单测失败，退出码 $LASTEXITCODE。" }
    }
    finally { Pop-Location }

    Push-Location $codeRoot
    try {
        # Compose 中 api、mcp、verify 共用同一镜像标签；先单独构建一次，避免并行导出竞争标签。
        Invoke-Compose @("build", "api")
        Invoke-Compose @("up", "--no-build", "--detach", "--wait", "db", "api", "mcp")
        Invoke-Compose @("run", "--rm", "--no-deps", "api", "uv", "run", "--frozen", "alembic", "upgrade", "head")
        Invoke-Compose @("run", "--rm", "--no-deps", "api", "uv", "run", "--frozen", "alembic", "check")
        Invoke-Compose @("run", "--rm", "--no-deps", "api", "uv", "run", "--frozen", "python", "-m", "app.seed")
        Invoke-Compose @("run", "--rm", "--no-deps", "verify", "uv", "run", "--frozen", "python", "-m", "pytest", "-q", "/workspace/tests/issues/test_issue_037.py", "/workspace/tests/issues/test_issue_036.py", "/workspace/tests/issues/test_issue_005.py", "/workspace/tests/issues/test_issue_004.py")

        if ($Mode -eq "offline") {
            Write-Host "离线模式使用独立模型响应夹具；API、Worker、MCP 与 PostgreSQL 仍为隔离真实服务。"
        }
        else {
            Write-Host "在线模式会调用本机配置的真实模型；API、Worker、MCP 与 PostgreSQL 使用隔离服务。"
        }
            $reportDir = "/workspace/artifacts/evals/$projectName"
            $codeVersion = (& git -C $repoRoot rev-parse HEAD).Trim()
            if ($LASTEXITCODE -ne 0) { throw "无法读取当前代码版本。" }
            $command = @(
                "run", "--rm", "--no-deps", "--env", "TRADEPILOT_CODE_VERSION=$codeVersion", "verify", "uv", "run", "--frozen",
                "python", "-m", "evals.runner", "--mode", $Mode, "--dataset", "v1",
                "--report-dir", $reportDir
            )
            foreach ($id in $CaseId) { $command += @("--case", $id) }
            foreach ($name in $Category) { $command += @("--category", $name) }
            Invoke-Compose $command
            Write-Host "$Mode 评测报告已保存到 $reportRoot\$projectName。"
    }
    finally { Pop-Location }
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color --tail 200 api mcp db 2>$null | Select-String -Pattern "Traceback|Exception|Error|MCP|httpx|HTTP" -Context 1, 2
    throw
}
finally {
    if (Test-Path -LiteralPath (Join-Path $codeRoot "compose.yaml")) {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "隔离项目清理失败；仅清理本轮项目：$projectName。"
        }
    }
    & docker image rm $backendImageName 2>$null | Out-Null
    Remove-Item -LiteralPath $overrideFile -ErrorAction SilentlyContinue
    Pop-Location
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
