$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue007-frontend-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose',
    '--project-name', $projectName,
    '--env-file', 'backend/.env',
    '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并设置本机数据库密码。'
}

function Get-FreeLoopbackPort {
    $portProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $portProbe.Start()
        return [string]$portProbe.LocalEndpoint.Port
    }
    finally {
        $portProbe.Stop()
    }
}

$oldEnvironment = @{}
$environmentNames = @(
    'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'APP_ORIGIN',
    'POSTGRES_PORT', 'API_PORT', 'WEB_PORT'
)
foreach ($name in $environmentNames) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

$usedPorts = [System.Collections.Generic.List[string]]::new()
foreach ($name in @('POSTGRES_PORT', 'API_PORT', 'WEB_PORT')) {
    do {
        $port = Get-FreeLoopbackPort
    } while ($usedPorts.Contains($port))
    $usedPorts.Add($port)
    Set-Item "Env:$name" $port
}

$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:SESSION_COOKIE_SECURE = 'false'
$env:APP_ORIGIN = "http://localhost:$($env:WEB_PORT)"

Push-Location $codeRoot
try {
    Write-Host "使用独立 Compose 项目验证简体中文前端、真实登录会话和同源代理：$projectName"
    & docker @composeArgs up --build --detach --wait api web
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL、API 或前端启动失败，退出码 $LASTEXITCODE。检查 Docker 和 backend/.env。"
    }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "数据库迁移失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) {
        throw "SQLAlchemy 模型与迁移不一致，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) {
        throw "写入隔离演示账号失败，退出码 $LASTEXITCODE。"
    }

    $webPort = $env:WEB_PORT
    $page = Invoke-WebRequest -Uri "http://127.0.0.1:$webPort/" -TimeoutSec 10
    if ($page.StatusCode -ne 200 -or $page.Content -notmatch '<html lang="zh-CN">') {
        throw '前端未返回简体中文 TradePilot 应用入口。'
    }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_007.py
    if ($LASTEXITCODE -ne 0) {
        throw "Issue #7 前端认证与同源代理集成验证失败，退出码 $LASTEXITCODE。"
    }

    Write-Host 'Issue #7 验证通过：简体中文路由入口、同源登录/身份恢复/CSRF 登出、失败凭据与跨源写入均有实际断言。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；仅对该项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location

    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$name" $oldEnvironment[$name]
        }
    }
}
