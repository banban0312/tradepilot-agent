param(
    [string]$CaseId,
    [string]$Category,
    [ValidateSet("offline", "online")]
    [string]$Mode = "offline",
    [string]$ReportDir = "artifacts/evals/issue-037"
)

$ErrorActionPreference = "Stop"
$repoCode = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoCode
try {
    uv run --frozen --project backend python -m pytest -q tests/issues/test_issue_037.py
    if ($LASTEXITCODE -ne 0) { throw "Issue 37 自动化测试失败，退出码 $LASTEXITCODE" }

    $arguments = @(
        "run", "--frozen", "--project", "backend", "python", "-m", "evals.runner",
        "--mode", $Mode, "--dataset", "v1", "--report-dir", $ReportDir
    )
    if ($CaseId) { $arguments += @("--case", $CaseId) }
    if ($Category) { $arguments += @("--category", $Category) }
    & uv @arguments
    if ($LASTEXITCODE -ne 0) { throw "评测执行失败，退出码 $LASTEXITCODE；请检查报告中的 FAIL、SKIPPED 或 ERROR" }
}
finally {
    Pop-Location
}
