param()

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue015-approval-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example 并配置本机数据库参数。'
}

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'DEMO_ALLOW_SELF_APPROVAL')
$oldEnvironment = @{}
foreach ($name in $environmentNames) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

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
$env:DEMO_MODE = 'true'
$env:DEMO_ALLOW_SELF_APPROVAL = 'false'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:SESSION_COOKIE_SECURE = 'false'

Push-Location $codeRoot
try {
    Write-Host '使用独立 Compose 项目运行审批 API、迁移与状态图验收。'
    & docker @composeArgs build api verify
    if ($LASTEXITCODE -ne 0) { throw "验证镜像构建失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --detach --wait db api
    if ($LASTEXITCODE -ne 0) { throw "隔离 PostgreSQL/API 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离演示账号及主数据初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_008.py /workspace/tests/issues/test_issue_013.py /workspace/tests/issues/test_issue_014.py /workspace/tests/issues/test_issue_015.py
    if ($LASTEXITCODE -ne 0) { throw "审批、持久恢复或任务 API 验收失败，退出码 $LASTEXITCODE。" }
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未清理隔离项目 $projectName。只对此项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
