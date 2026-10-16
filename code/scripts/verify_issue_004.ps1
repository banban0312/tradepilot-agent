$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue004-draft-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
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
}
$env:DEMO_MODE = 'true'
$env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')

Push-Location $codeRoot
try {
    Write-Host "使用隔离的 Compose 项目验证草稿金额、来源和版本：$projectName"
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
        throw "首次写入隔离示例账号与主数据失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs exec -T api uv run --frozen python -m app.seed
    if ($LASTEXITCODE -ne 0) {
        throw "重复写入隔离示例账号与主数据失败，退出码 $LASTEXITCODE。"
    }

    & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q /workspace/tests/issues/test_issue_004.py
    if ($LASTEXITCODE -ne 0) {
        throw "Issue #4 草稿领域与数据库集成验证失败，退出码 $LASTEXITCODE。"
    }

    Write-Host 'Issue #4 验证通过：金额舍入、候选/有效类型、来源证据、内容哈希、版本冲突与数据库快照均有断言。'
}
catch {
    Write-Host $_ -ForegroundColor Red
    & docker @composeArgs logs --no-color
    throw
}
finally {
    & docker @composeArgs down --volumes --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "未能清理隔离项目 $projectName；可只对该项目执行 docker compose --project-name $projectName --env-file backend/.env --file compose.yaml down --volumes --remove-orphans。"
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
