param()

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue019-orders-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先按项目说明配置本机数据库参数。'
}

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'DEMO_ALLOW_SELF_APPROVAL')
$oldEnvironment = @{}
foreach ($name in $environmentNames) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

$env:POSTGRES_PORT = Get-FreeLocalPort
$env:API_PORT = Get-FreeLocalPort
$env:WEB_PORT = Get-FreeLocalPort
$env:APP_ORIGIN = "http://localhost:$env:WEB_PORT"
$env:DEMO_MODE = 'true'
$env:DEMO_ALLOW_SELF_APPROVAL = 'false'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:SESSION_COOKIE_SECURE = 'false'

Push-Location $codeRoot
try {
    Write-Host "使用独立 Compose 项目 $projectName 验收订单列表、批准快照和部门隔离。"
    & docker @composeArgs build api web verify
    if ($LASTEXITCODE -ne 0) { throw "API、前端或验证镜像构建失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --detach --wait db api web
    if ($LASTEXITCODE -ne 0) { throw "隔离 PostgreSQL/API/Web 启动失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen ruff check --ignore EXE002 --config 'lint.isort.known-first-party=["app"]' app/orders/routes.py /workspace/tests/issues/test_issue_019.py
    if ($LASTEXITCODE -ne 0) { throw "Python 静态检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen ruff format --check app/orders/routes.py /workspace/tests/issues/test_issue_019.py
    if ($LASTEXITCODE -ne 0) { throw "Python 格式检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m compileall -q app/orders/routes.py /workspace/tests/issues/test_issue_019.py
    if ($LASTEXITCODE -ne 0) { throw "Python 编译检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离演示账号及主数据初始化失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_016.py /workspace/tests/issues/test_issue_019.py
    if ($LASTEXITCODE -ne 0) { throw "订单原子提交回归、列表筛选、详情快照或部门隔离验收失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps web npm run type-check
    if ($LASTEXITCODE -ne 0) { throw "前端 TypeScript 检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps web npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端生产构建失败，退出码 $LASTEXITCODE。" }

    Write-Host 'Issue #19 验收通过：隔离数据库中的订单筛选、批准快照/明细一致性、跨部门拒绝、重复提交幂等、API/前端构建均通过。'
    Write-Host '在线模型评测未执行；订单接口验证由真实隔离 PostgreSQL/API 提供，订单数据由已验证的提单服务和固定测试夹具创建。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color db api web
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) { Write-Warning "未能清理隔离项目 $projectName；仅对此随机 Compose 项目执行对应的 down 清理。" }
    Pop-Location
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
