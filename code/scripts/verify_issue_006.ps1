$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue006-observability-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose',
    '--project-name', $projectName,
    '--env-file', 'backend/.env',
    '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并设置本机 PostgreSQL 端口及密码。'
}

$oldEnvironment = @{
    DEMO_MODE = $env:DEMO_MODE
    DEMO_PASSWORD = $env:DEMO_PASSWORD
    POSTGRES_PORT = $env:POSTGRES_PORT
    API_PORT = $env:API_PORT
}
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
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
$env:POSTGRES_PORT = Get-FreeLoopbackPort
do {
    $env:API_PORT = Get-FreeLoopbackPort
} while ($env:API_PORT -eq $env:POSTGRES_PORT)

Push-Location $codeRoot
try {
    Write-Host "使用独立 PostgreSQL 验证任务事件、Trace/Span 与追加审计：$projectName"
    & docker @composeArgs up --build --detach --wait api
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL 或 API 启动失败，退出码 $LASTEXITCODE。检查 Docker 和 backend/.env。"
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
        throw "写入隔离示例用户失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_006.py
    if ($LASTEXITCODE -ne 0) {
        throw "Issue #6 可观测性集成验证失败，退出码 $LASTEXITCODE。"
    }

    Write-Host 'Issue #6 验证通过：span 状态与脱敏、并发事件顺序/回滚、审计不可改写、中断恢复及留存保护均有数据库断言。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；可仅对此项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location

    foreach ($name in $oldEnvironment.Keys) {
        if ($null -eq $oldEnvironment[$name]) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$name" $oldEnvironment[$name]
        }
    }
}
