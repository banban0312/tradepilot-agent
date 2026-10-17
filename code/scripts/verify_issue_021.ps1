param([switch]$LiveModel)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue021-core-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml')

if (-not (Test-Path -LiteralPath $envFile)) { throw '未找到 code/backend/.env；请先按 backend/.env.example 配置数据库。' }

$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'DEMO_ALLOW_SELF_APPROVAL', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'TRADEPILOT_ONLINE_MODEL', 'MCP_CONTEXT_SIGNING_KEY')
$oldEnvironment = @{}
foreach ($name in $environmentNames) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$overrideFile = $null

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose 命令失败，退出码 $LASTEXITCODE：$($Arguments -join ' ')" }
}

function Invoke-VerifyPython {
    param([string[]]$Arguments)
    & docker @composeArgs run --rm --no-deps verify uv run --frozen python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "隔离 Python 验收命令失败，退出码 $LASTEXITCODE。" }
}

function Get-VerifyOutput {
    param([string[]]$Arguments)
    $output = @(& docker @composeArgs run --rm --no-deps verify uv run --frozen python @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "隔离验收阶段失败：$($output -join [Environment]::NewLine)" }
    return $output -join [Environment]::NewLine
}

function Restart-WorkerAndAssert {
    param([string]$TaskId, [string]$ExpectedState, [int]$Count = 10)
    $workerId = (& docker @composeArgs ps -q worker).Trim()
    if (-not $workerId) { throw '找不到本次隔离 Compose 的 Worker 容器。' }
    for ($iteration = 1; $iteration -le $Count; $iteration++) {
        $before = (& docker inspect --format '{{.State.StartedAt}}' $workerId).Trim()
        Invoke-Compose @('restart', 'worker')
        $healthy = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            $state = (& docker inspect --format '{{.State.Status}}|{{.State.StartedAt}}' $workerId).Trim()
            $parts = $state.Split('|')
            if ($parts.Count -ge 2 -and $parts[0] -eq 'running' -and $parts[1] -ne $before) {
                $null = & docker exec $workerId uv run --frozen python -m app.worker.healthcheck 2>&1
                if ($LASTEXITCODE -eq 0) { $healthy = $true; break }
            }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) { throw "第 $iteration 次 Worker 重启后未恢复健康。" }
        Invoke-VerifyPython @('/workspace/tests/issues/issue_021_live_acceptance.py', 'assert-wait', '--task-id', $TaskId, '--expected', $ExpectedState)
        Write-Host "$ExpectedState 等待态真实 Worker 重启 $iteration/$Count 次，原任务状态和检查点回查通过。"
    }
}

function Set-IsolatedEnvironment {
    $env:POSTGRES_PORT = Get-FreeLocalPort
    do { $env:API_PORT = Get-FreeLocalPort } while ($env:API_PORT -eq $env:POSTGRES_PORT)
    do { $env:MCP_HOST_PORT = Get-FreeLocalPort } while ($env:MCP_HOST_PORT -in @($env:POSTGRES_PORT, $env:API_PORT))
    do { $env:WEB_PORT = Get-FreeLocalPort } while ($env:WEB_PORT -in @($env:POSTGRES_PORT, $env:API_PORT, $env:MCP_HOST_PORT))
    # 既有 #3/#20 集成用例使用固定本机开发 Origin。
    $env:APP_ORIGIN = 'http://localhost:5173'
    $env:DEMO_MODE = 'true'
    $env:DEMO_ALLOW_SELF_APPROVAL = 'false'
    $env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:TRADEPILOT_TEST_MODE = '1'
    $env:TRADEPILOT_MCP_INTEGRATION = '1'
    $env:TRADEPILOT_ONLINE_MODEL = if ($LiveModel) { '1' } else { '0' }
    if (-not $LiveModel) {
        $script:overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) ($projectName + '-offline-model.yaml')
        @'
services:
  api:
    environment:
      MODEL_PROVIDER: ""
      MODEL_NAME: ""
      MODEL_API_KEY: ""
      MODEL_BASE_URL: ""
      OCR_PROVIDER: ""
      OCR_API_KEY: ""
  verify:
    environment:
      MODEL_PROVIDER: ""
      MODEL_NAME: ""
      MODEL_API_KEY: ""
      MODEL_BASE_URL: ""
      OCR_PROVIDER: ""
      OCR_API_KEY: ""
'@ | Set-Content -LiteralPath $script:overrideFile -Encoding utf8
        $script:composeArgs += @('--file', $script:overrideFile)
    }
    $signingKey = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($signingKey)
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($signingKey)
    [Array]::Clear($signingKey, 0, $signingKey.Length)
}

function Invoke-IssueAcceptance {
    Push-Location $codeRoot
    try {
        Set-IsolatedEnvironment
        Write-Host "使用独立 Compose 项目 $projectName 执行 M3-07 核心验收。"
        Invoke-Compose @('build', 'api', 'verify', 'web')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'api', 'mcp', 'web')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'check', '--ignore', 'EXE002', '--config', 'lint.isort.known-first-party=["app"]', 'app', '/workspace/tests/issues/test_issue_021.py', '/workspace/tests/issues/issue_021_live_acceptance.py')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'format', '--check', '/workspace/tests/issues/test_issue_021.py', '/workspace/tests/issues/issue_021_live_acceptance.py')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'compileall', '-q', 'app', '/workspace/tests/issues/test_issue_021.py', '/workspace/tests/issues/issue_021_live_acceptance.py')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')

        $regressionFiles = 1..20 | ForEach-Object { '/workspace/tests/issues/test_issue_{0:D3}.py' -f $_ }
        $regressionFiles += '/workspace/tests/issues/test_issue_021.py'
        $pytestSelection = @()
        if ($LiveModel) {
            # #1 的该断言要求模型配置为空；在线模式下由默认离线命令独立验证。
            $pytestSelection = @('-k', 'not test_api_reports_database_readiness_and_unconfigured_integrations')
        }
        & docker @composeArgs run --rm --no-deps verify uv run --frozen python -m pytest -q @pytestSelection @regressionFiles
        if ($LASTEXITCODE -ne 0) { throw "AC-01～AC-20 对应离线/服务集成回归失败，退出码 $LASTEXITCODE。" }
        Invoke-Compose @('run', '--rm', '--no-deps', 'web', 'npm', 'run', 'type-check')
        Invoke-Compose @('run', '--rm', '--no-deps', 'web', 'npm', 'run', 'build')

        if ($LiveModel) {
            $modelValues = @{}
            foreach ($line in Get-Content -LiteralPath $envFile) {
                if ($line -match '^\s*(MODEL_PROVIDER|MODEL_NAME|MODEL_API_KEY)\s*=\s*(.*)$') { $modelValues[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'") }
            }
            if ($modelValues['MODEL_PROVIDER'] -ne 'deepseek' -or -not $modelValues['MODEL_NAME'] -or -not $modelValues['MODEL_API_KEY']) { throw '在线验收要求配置 DeepSeek provider、model name 和密钥；密钥值不会显示。' }
            Invoke-Compose @('up', '--detach', '--wait', 'worker')

            $startOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_021_live_acceptance.py', 'create-input')
            if ($startOutput -match 'ISSUE021_TASK_ID=([0-9a-fA-F-]{36})') { $taskId = $Matches[1] }
            else { throw "无法解析真实 WAITING_INPUT 任务 ID：$startOutput" }
            Restart-WorkerAndAssert -TaskId $taskId -ExpectedState 'WAITING_INPUT' -Count 10

            $continueOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_021_live_acceptance.py', 'continue', '--task-id', $taskId)
            if ($continueOutput -notmatch 'ISSUE021_WAIT_STATE=WAITING_APPROVAL') { throw "在线模型/MCP 未将任务推进到审批等待：$continueOutput" }
            Restart-WorkerAndAssert -TaskId $taskId -ExpectedState 'WAITING_APPROVAL' -Count 10

            $approvalOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_021_live_acceptance.py', 'approve', '--task-id', $taskId)
            if ($approvalOutput -notmatch 'ISSUE021_ORDER_COMMIT=verified') { throw "真实批准、订单列表/详情或数据库事实核验失败：$approvalOutput" }
            $taskReference = [regex]::Match($approvalOutput, 'ISSUE021_TASK_ID=([0-9a-fA-F-]{36})').Groups[1].Value
            $orderReference = [regex]::Match($approvalOutput, 'ISSUE021_ORDER_ID=([0-9a-fA-F-]{36})').Groups[1].Value
            if (-not $taskReference -or -not $orderReference) { throw '在线验收通过但未能解析任务/订单证据编号。' }
            Write-Host '在线验收通过：DeepSeek 真实工具选择与 MCP、20 次等待态 Worker 重启、人工批准、正式订单/额度/审计回查。'
            Write-Host "隔离在线事实引用：task_id=$taskReference；order_id=$orderReference。"
        }

        Write-Host 'M3-07 离线/服务集成验收通过；AC-20 文件来源下载权限保留为 M4 补验。'
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --no-color db api mcp worker web
        throw
    }
    finally {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) { Write-Warning "随机隔离项目 $projectName 清理失败；仅针对该项目执行 down --volumes --remove-orphans。" }
        if ($script:overrideFile -and (Test-Path -LiteralPath $script:overrideFile)) { Remove-Item -LiteralPath $script:overrideFile }
        Pop-Location
    }
}

try { Invoke-IssueAcceptance }
finally {
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
