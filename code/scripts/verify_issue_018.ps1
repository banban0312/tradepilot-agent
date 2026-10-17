param()

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue018-workbench-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先按项目说明配置本机数据库参数。'
}

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'DEMO_ALLOW_SELF_APPROVAL')
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
$env:WEB_PORT = Get-FreeLocalPort
$env:APP_ORIGIN = "http://localhost:$env:WEB_PORT"
$env:DEMO_MODE = 'true'
$env:DEMO_ALLOW_SELF_APPROVAL = 'false'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:SESSION_COOKIE_SECURE = 'false'

Push-Location $codeRoot
try {
    Write-Host "使用独立 Compose 项目 $projectName 验收工作台、草稿版本、额度摘要与部门隔离。"
    & docker @composeArgs build api web verify
    if ($LASTEXITCODE -ne 0) { throw "后端/前端验证镜像构建失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs up --detach --wait db api web
    if ($LASTEXITCODE -ne 0) { throw "隔离 PostgreSQL/API/Web 启动失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) { throw "ORM 与迁移一致性检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen ruff check --ignore EXE002 --config 'lint.isort.known-first-party=["app"]' app/tasks/routes.py /workspace/tests/issues/test_issue_018.py
    if ($LASTEXITCODE -ne 0) { throw "Python 静态检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen ruff format --check app/tasks/routes.py /workspace/tests/issues/test_issue_018.py
    if ($LASTEXITCODE -ne 0) { throw "Python 格式检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m compileall -q app/tasks/routes.py /workspace/tests/issues/test_issue_018.py
    if ($LASTEXITCODE -ne 0) { throw "Python 编译检查失败，退出码 $LASTEXITCODE。" }
    & docker @composeArgs run --rm --no-deps api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) { throw "隔离演示账号及主数据初始化失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_014.py /workspace/tests/issues/test_issue_015.py /workspace/tests/issues/test_issue_017.py /workspace/tests/issues/test_issue_018.py
    if ($LASTEXITCODE -ne 0) { throw "任务事件、审批、安全恢复或工作台额度验收失败，退出码 $LASTEXITCODE。" }

    & docker @composeArgs run --rm --no-deps web npm run type-check
    if ($LASTEXITCODE -ne 0) { throw "前端 TypeScript 检查失败，退出码 $LASTEXITCODE。" }
    Write-Host 'Issue #18 验收通过：真实任务快照、输入等待状态、乐观锁草稿编辑、金额与额度重算、跨部门额度保密及审批/恢复回归均通过。'
    Write-Host '在线模型评测未执行；本验收的任务由隔离数据库夹具建立，不代表模型在线调用证据。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color db api web
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；只对此项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
    }
    Pop-Location
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
