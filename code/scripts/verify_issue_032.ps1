$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$codeRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $codeRoot
$backendRoot = Join-Path $codeRoot 'backend'
$frontendRoot = Join-Path $codeRoot 'frontend'
$testPath = Join-Path $codeRoot 'tests/issues/test_issue_032.py'

Push-Location $repoRoot
try {
    uv run --project $backendRoot pytest $testPath
    if ($LASTEXITCODE -ne 0) { throw 'Issue #32 自动化行为回归失败。' }

    npm --prefix $frontendRoot run build
    if ($LASTEXITCODE -ne 0) { throw 'Issue #32 前端类型检查或生产构建失败。' }

    Write-Host 'M5-04 悬浮窗与全屏共享状态验收通过。'
} finally {
    Pop-Location
}
