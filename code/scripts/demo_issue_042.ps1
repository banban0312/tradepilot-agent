param(
    [ValidateSet('TextToOrder', 'OcrAndQuota', 'ExcelPartial', 'DualModeTraceEvaluation', 'All')]
    [string]$Scenario = 'TextToOrder'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$scriptsRoot = Join-Path $PSScriptRoot ''

function Invoke-IssueDemoCheck {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [hashtable]$Parameters = @{}
    )

    $path = Join-Path $scriptsRoot "$Name.ps1"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "未找到演示验证入口：$path"
    }
    Write-Host "`n开始：$Name" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    & $path @Parameters
    if ($LASTEXITCODE -ne 0) {
        throw "$Name 失败，退出码 $LASTEXITCODE。请检查该隔离验收脚本输出。"
    }
    Write-Host "完成：$Name" -ForegroundColor Green
}

function Invoke-Scenario {
    param([Parameter(Mandatory)] [string]$Name)

    switch ($Name) {
        'TextToOrder' {
            Write-Host '文本流程案例：缺少规格时等待补充；正常文本生成草稿；独立批准案例核对正式订单和额度事实。案例使用彼此隔离的评测任务。'
            Invoke-IssueDemoCheck -Name 'verify_issue_037' -Parameters @{
                CaseId = @('ambiguity-missing-spec', 'normal-complete-sales', 'normal-approved-order')
            }
        }
        'OcrAndQuota' {
            Write-Host '依次验收真实本地 OCR/文件证据与独立超额规则案例；二者使用不同隔离任务，不伪称同一图片的在线端到端流程。'
            Invoke-IssueDemoCheck -Name 'verify_issue_028'
            Invoke-IssueDemoCheck -Name 'verify_issue_037' -Parameters @{
                CaseId = @('quota-over', 'quota-over-by-cent')
            }
        }
        'ExcelPartial' {
            Write-Host '混合 Excel 分组：正常组、需修正组和超额组分别核对源行、任务创建与无副作用边界。'
            Invoke-IssueDemoCheck -Name 'verify_issue_029' -Parameters @{ IssueOnly = $true }
        }
        'DualModeTraceEvaluation' {
            Write-Host '依次验证悬浮窗/全屏共享状态、历史轨迹和评测中心页面/API。'
            Invoke-IssueDemoCheck -Name 'verify_issue_032'
            Invoke-IssueDemoCheck -Name 'verify_issue_029' -Parameters @{ Issue033 = $true; IssueOnly = $true }
            Invoke-IssueDemoCheck -Name 'verify_issue_039'
        }
        default { throw "不支持的演示场景：$Name" }
    }
}

Push-Location $repoRoot
try {
    Write-Host "TradePilot M6-07 场景：$Scenario。验收服务使用随机项目与数据卷，结果写入 code/artifacts/。" -ForegroundColor Yellow
    if ($Scenario -eq 'All') {
        foreach ($name in @('TextToOrder', 'OcrAndQuota', 'ExcelPartial', 'DualModeTraceEvaluation')) {
            Invoke-Scenario -Name $name
        }
    }
    else {
        Invoke-Scenario -Name $Scenario
    }
    Write-Host "`n演示场景 $Scenario 全部通过。请按各验证脚本输出查看隔离报告目录。" -ForegroundColor Green
}
finally {
    Pop-Location
}
