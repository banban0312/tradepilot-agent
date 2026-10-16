$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue003-auth-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose',
    '--project-name', $projectName,
    '--env-file', 'backend/.env',
    '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并按需设置本机数据库密码。'
}

$oldEnvironment = @{
    DEMO_MODE = $env:DEMO_MODE
    DEMO_PASSWORD = $env:DEMO_PASSWORD
    SESSION_COOKIE_SECURE = $env:SESSION_COOKIE_SECURE
    APP_ORIGIN = $env:APP_ORIGIN
}
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
$env:SESSION_COOKIE_SECURE = 'false'
$env:APP_ORIGIN = 'http://localhost:5173'

Push-Location $codeRoot
try {
    Write-Host "使用隔离的 Compose 项目验证登录、会话、CSRF 和部门权限：$projectName"
    & docker @composeArgs up --build --detach --wait api web
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL 或 API 启动失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "首次数据库迁移失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "重复执行数据库迁移失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen alembic check
    if ($LASTEXITCODE -ne 0) {
        throw "SQLAlchemy 模型与已应用迁移不一致，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) {
        throw "首次写入隔离示例账号失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) {
        throw "重复写入隔离示例账号失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_003.py
    if ($LASTEXITCODE -ne 0) {
        throw "Issue #3 身份与权限集成验证失败，退出码 $LASTEXITCODE。"
    }

    Write-Host 'Issue #3 验证通过：会话哈希、登录失效、CSRF、同源写入与部门/角色边界均有实际断言。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；可使用 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
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
