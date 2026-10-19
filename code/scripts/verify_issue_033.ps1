$ErrorActionPreference = 'Stop'
$verifyScript = Join-Path $PSScriptRoot 'verify_issue_029.ps1'
& $verifyScript -IssueOnly -Issue033
if ($LASTEXITCODE -ne 0) { throw "M5-05 隔离验收失败，退出码 $LASTEXITCODE。" }
