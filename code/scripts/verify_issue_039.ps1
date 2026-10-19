$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$codeRoot = Join-Path $repoRoot "code"
$frontendRoot = Join-Path $codeRoot "frontend"
$oldPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")

try {
    $env:PYTHONPATH = "$(Join-Path $codeRoot 'backend');$(Join-Path $codeRoot '')"
    Push-Location $repoRoot
    try {
        uv run --frozen --project backend --directory code ruff check backend/app/evaluations/routes.py backend/app/main.py backend/app/config.py evals/backend_executor.py evals/service.py evals/runner.py tests/issues/test_issue_039.py
        if ($LASTEXITCODE -ne 0) { throw "Issue #39 Ruff 检查失败，退出码 $LASTEXITCODE。" }
        uv run --frozen --project backend --directory code ruff format --check backend/app/evaluations/routes.py backend/app/main.py backend/app/config.py evals/backend_executor.py evals/service.py evals/runner.py tests/issues/test_issue_039.py
        if ($LASTEXITCODE -ne 0) { throw "Issue #39 格式检查失败，退出码 $LASTEXITCODE。" }
        uv run --frozen --project backend --directory code python -m pytest -q tests/issues/test_issue_039.py tests/issues/test_issue_038.py
        if ($LASTEXITCODE -ne 0) { throw "Issue #39 后端回归失败，退出码 $LASTEXITCODE。" }
        npm run type-check --prefix $frontendRoot
        if ($LASTEXITCODE -ne 0) { throw "前端类型检查失败，退出码 $LASTEXITCODE。" }
        npm run build --prefix $frontendRoot
        if ($LASTEXITCODE -ne 0) { throw "前端生产构建失败，退出码 $LASTEXITCODE。" }
        git diff --check
        if ($LASTEXITCODE -ne 0) { throw "Git 差异空白检查失败，退出码 $LASTEXITCODE。" }
    }
    finally { Pop-Location }
}
finally {
    if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { Set-Item Env:PYTHONPATH $oldPythonPath }
}
