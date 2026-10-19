$ErrorActionPreference = 'Stop'
$verifyScript = Join-Path $PSScriptRoot 'verify_issue_029.ps1'
& $verifyScript -IssueOnly -Issue030
if ($LASTEXITCODE -ne 0) { throw "M5-02 隔离验收失败，退出码 $LASTEXITCODE。" }
