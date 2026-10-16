param(
    [switch]$KeepStack
)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue001-verify-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose',
    '--project-name', $projectName,
    '--env-file', 'backend/.env',
    '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并按需设置本机数据库密码。'
}

$localConfig = @{}
foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
    if ($line -match '^\s*([^#=\s]+)\s*=\s*(.*?)\s*$') {
        $localConfig[$Matches[1]] = $Matches[2]
    }
}
$webPort = if ($localConfig.ContainsKey('WEB_PORT')) { [int]$localConfig['WEB_PORT'] } else { 5173 }
$apiPort = if ($localConfig.ContainsKey('API_PORT')) { [int]$localConfig['API_PORT'] } else { 8010 }

Push-Location $codeRoot
try {
    Write-Host "使用隔离的 Compose 项目启动工程基线：$projectName"
    & docker @composeArgs up --build --detach --wait
    if ($LASTEXITCODE -ne 0) {
        throw "Compose 启动或健康检查失败，退出码 $LASTEXITCODE。"
    }

    $web = Invoke-WebRequest -Uri "http://127.0.0.1:$webPort/" -TimeoutSec 10
    if ($web.StatusCode -ne 200 -or $web.Content -notmatch 'TradePilot') {
        throw '前端未返回预期的 TradePilot 工程页面。'
    }

    $directHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/health/ready" -TimeoutSec 10
    if ($directHealth.status -ne 'ready' -or $directHealth.database -ne 'connected') {
        throw 'API 就绪检查未确认 PostgreSQL 连接。'
    }

    $proxiedHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$webPort/api/health/ready" -TimeoutSec 10
    if ($proxiedHealth.status -ne 'ready' -or $proxiedHealth.database -ne 'connected') {
        throw '前端 API 代理未返回 PostgreSQL 就绪状态。'
    }

    & docker @composeArgs run --rm --no-deps verify
    if ($LASTEXITCODE -ne 0) {
        throw "Issue #1 真实集成验证失败，退出码 $LASTEXITCODE。"
    }

    Write-Host 'Issue #1 验证通过：前端、API、PostgreSQL、MCP Streamable HTTP 与 LangGraph 检查点。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    if (-not $KeepStack) {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "未能清理隔离项目 $projectName；可使用 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
        }
    }
    else {
        Write-Host "已保留隔离项目 $projectName。清理时请只对这个项目名称执行 docker compose down --volumes。"
    }
    Pop-Location
}
