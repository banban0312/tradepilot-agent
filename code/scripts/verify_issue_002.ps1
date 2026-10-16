$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendRoot = Join-Path $codeRoot 'backend'
$envFile = Join-Path $backendRoot '.env'
$projectName = 'tp-issue002-verify-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @(
    'compose',
    '--project-name', $projectName,
    '--env-file', 'backend/.env',
    '--file', 'compose.yaml'
)

if (-not (Test-Path -LiteralPath $envFile)) {
    throw '未找到 code/backend/.env。请先复制 backend/.env.example，并按需设置本机数据库密码。'
}

Push-Location $codeRoot
try {
    Write-Host "使用隔离的 Compose 项目验证数据库迁移和演示数据：$projectName"
    & docker @composeArgs up --build --detach --wait api
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
        throw "首次写入演示主数据失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) {
        throw "重复写入演示主数据失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_002.py
    if ($LASTEXITCODE -ne 0) {
        throw "Issue #2 数据库集成验证失败，退出码 $LASTEXITCODE。"
    }

    Write-Host 'Issue #2 验证通过：迁移可重复执行、主数据幂等、约束有效且演示边界明确。'
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
}
