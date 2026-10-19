param([switch]$OfflineOnly)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = Split-Path -Parent $codeRoot
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue035-acceptance-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$imageName = "tradepilot/backend:verify-$projectName"
$webImageName = "tradepilot/web:verify-$projectName"
$overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-$projectName.yaml"
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml', '--file', $overrideFile)
$environmentNames = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_PASSWORD', 'DEMO_ALLOW_SELF_APPROVAL', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'TRADEPILOT_ONLINE_MODEL', 'MCP_CONTEXT_SIGNING_KEY', 'RUNTIME_CONFIG_ENCRYPTION_KEY')
$oldEnvironment = @{}
foreach ($name in $environmentNames) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }

if (-not (Test-Path -LiteralPath $envFile)) { throw '未找到 code/backend/.env；请先配置隔离验收所需的数据库参数。' }

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "隔离 Compose 命令失败，退出码 $LASTEXITCODE：$($Arguments -join ' ')" }
}

function Invoke-VerifyPython {
    param([string[]]$Arguments)
    & docker @composeArgs run --rm --no-deps verify uv run --frozen python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "隔离 Python 验收失败，退出码 $LASTEXITCODE：$($Arguments -join ' ')" }
}

function Get-VerifyOutput {
    param([string[]]$Arguments)
    $output = @(& docker @composeArgs run --rm --no-deps verify uv run --frozen python @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "隔离验收阶段失败：$($output -join [Environment]::NewLine)" }
    return $output -join [Environment]::NewLine
}

function Set-IsolatedEnvironment {
    $ports = @()
    while ($ports.Count -lt 4) {
        $candidate = Get-FreeLocalPort
        if ($candidate -notin $ports) { $ports += $candidate }
    }
    $env:POSTGRES_PORT = $ports[0]
    $env:API_PORT = $ports[1]
    $env:MCP_HOST_PORT = $ports[2]
    $env:WEB_PORT = $ports[3]
    $env:APP_ORIGIN = 'http://localhost:5173'
    $env:DEMO_MODE = 'true'
    $env:DEMO_ALLOW_SELF_APPROVAL = 'false'
    $env:DEMO_PASSWORD = 'TP-Verify-' + [Guid]::NewGuid().ToString('N')
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:TRADEPILOT_TEST_MODE = '1'
    $env:TRADEPILOT_MCP_INTEGRATION = '1'
    $env:TRADEPILOT_ONLINE_MODEL = if ($OfflineOnly) { '0' } else { '1' }
    $key = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($key)
    $encoded = [Convert]::ToBase64String($key).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($key)
    $env:RUNTIME_CONFIG_ENCRYPTION_KEY = $encoded + '='
    [Array]::Clear($key, 0, $key.Length)

    $modelValues = @{}
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*(MODEL_PROVIDER|MODEL_NAME|MODEL_API_KEY)\s*=\s*(.*)$') {
            $modelValues[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
        }
    }
    if (-not $OfflineOnly -and ($modelValues['MODEL_PROVIDER'] -ne 'deepseek' -or -not $modelValues['MODEL_NAME'] -or -not $modelValues['MODEL_API_KEY'])) {
        throw '在线验收需要 code/backend/.env 中已配置 DeepSeek provider、模型名和密钥；密钥值不会显示。'
    }

    @(
        "services:`n  api:`n    image: $imageName`n    environment:`n      MCP_URL: http://mcp:8000/mcp`n      RUNTIME_CONFIG_ENCRYPTION_KEY: $env:RUNTIME_CONFIG_ENCRYPTION_KEY`n  worker:`n    image: $imageName`n    environment:`n      MCP_URL: http://mcp:8000/mcp`n      RUNTIME_CONFIG_ENCRYPTION_KEY: $env:RUNTIME_CONFIG_ENCRYPTION_KEY`n  mcp:`n    image: $imageName`n  verify:`n    image: $imageName`n    environment:`n      MCP_URL: http://mcp:8000/mcp`n      RUNTIME_CONFIG_ENCRYPTION_KEY: $env:RUNTIME_CONFIG_ENCRYPTION_KEY`n  web:`n    image: $webImageName"
    ) -join "`n" | Set-Content -LiteralPath $overrideFile -Encoding utf8
}

function Restart-WorkerAndAssert {
    param([string]$AcceptanceScript, [string[]]$Arguments, [string]$ExpectedState)
    $workerId = (& docker @composeArgs ps -q worker).Trim()
    if (-not $workerId) { throw '隔离验收找不到本轮 Worker。' }
    $before = (& docker inspect --format '{{.State.StartedAt}}' $workerId).Trim()
    Invoke-Compose @('restart', 'worker')
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $state = (& docker inspect --format '{{.State.Status}}|{{.State.StartedAt}}' $workerId).Trim().Split('|')
        if ($state.Count -ge 2 -and $state[0] -eq 'running' -and $state[1] -ne $before) {
            $null = & docker exec $workerId uv run --frozen python -m app.worker.healthcheck 2>&1
            if ($LASTEXITCODE -eq 0) { $healthy = $true; break }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { throw '隔离 Worker 重启后未恢复健康。' }
    Invoke-VerifyPython (@("/workspace/tests/issues/$AcceptanceScript") + $Arguments)
    Write-Host "等待态 $ExpectedState 经 Worker 重启后仍与原 task/checkpoint 一致。"
}

try {
    Push-Location $codeRoot
    try {
        Set-IsolatedEnvironment
        Push-Location $repoRoot
        try {
            Write-Host '运行悬浮窗/全屏共享任务行为回归与前端生产构建。'
            & npm --prefix code/frontend run test:unit -- --reporter=dot
            if ($LASTEXITCODE -ne 0) { throw '悬浮窗/全屏共享任务行为测试失败。' }
            & npm --prefix code/frontend run type-check
            if ($LASTEXITCODE -ne 0) { throw '前端 TypeScript 类型检查失败。' }
            & npm --prefix code/frontend run build
            if ($LASTEXITCODE -ne 0) { throw '前端生产构建失败。' }
        } finally { Pop-Location }

        Write-Host "创建隔离 Compose 项目 $projectName；只会清理本项目资源，不操作默认 Docker 服务。"
        Invoke-Compose @('build', 'api', 'web')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'api', 'mcp', 'web')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        $pythonFiles = @('app', '/workspace/tests/issues/test_issue_015.py', '/workspace/tests/issues/test_issue_016.py', '/workspace/tests/issues/test_issue_017.py', '/workspace/tests/issues/test_issue_018.py', '/workspace/tests/issues/test_issue_020.py', '/workspace/tests/issues/test_issue_024.py', '/workspace/tests/issues/test_issue_026.py', '/workspace/tests/issues/test_issue_028.py', '/workspace/tests/issues/test_issue_029.py', '/workspace/tests/issues/test_issue_030.py', '/workspace/tests/issues/test_issue_031.py', '/workspace/tests/issues/test_issue_033.py', '/workspace/tests/issues/test_issue_034.py', '/workspace/tests/issues/test_issue_035.py', '/workspace/tests/issues/issue_021_live_acceptance.py', '/workspace/tests/issues/issue_035_live_acceptance.py')
        $newPythonFiles = @('/workspace/tests/issues/test_issue_035.py', '/workspace/tests/issues/issue_035_live_acceptance.py')
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'check', '--ignore', 'EXE002', '--config', 'lint.isort.known-first-party=["app"]') + $pythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'ruff', 'format', '--check') + $newPythonFiles)
        Invoke-Compose (@('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'compileall', '-q') + $pythonFiles)
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')
        $testFiles = @('/workspace/tests/issues/test_issue_015.py', '/workspace/tests/issues/test_issue_016.py', '/workspace/tests/issues/test_issue_017.py', '/workspace/tests/issues/test_issue_018.py', '/workspace/tests/issues/test_issue_020.py', '/workspace/tests/issues/test_issue_024.py', '/workspace/tests/issues/test_issue_026.py', '/workspace/tests/issues/test_issue_028.py', '/workspace/tests/issues/test_issue_029.py', '/workspace/tests/issues/test_issue_030.py', '/workspace/tests/issues/test_issue_031.py', '/workspace/tests/issues/test_issue_033.py', '/workspace/tests/issues/test_issue_034.py', '/workspace/tests/issues/test_issue_035.py')
        Invoke-Compose (@('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-x', '-q') + $testFiles)

        if (-not $OfflineOnly) {
            $mcpOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_035_live_acceptance.py', 'mcp-smoke')
            if ($mcpOutput -notmatch 'ISSUE035_MCP_LIST_TOOLS=verified') { throw "隔离 MCP 工具注册表检查失败：$mcpOutput" }
            $queueOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_035_live_acceptance.py', 'prepare-live')
            Write-Host "在线联调前隔离本轮离线回归遗留作业：$queueOutput"
            Invoke-Compose @('up', '--detach', '--wait', 'worker')
            $inputOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_035_live_acceptance.py', 'create-input')
            $inputTaskId = [regex]::Match($inputOutput, 'ISSUE035_INPUT_TASK_ID=([0-9a-fA-F-]{36})').Groups[1].Value
            if (-not $inputTaskId) { throw '无法记录等待补充的真实 task_id。' }
            Restart-WorkerAndAssert 'issue_035_live_acceptance.py' @('assert-input-wait', '--task-id', $inputTaskId) 'WAITING_INPUT'

            $batchOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_035_live_acceptance.py', 'create-batch')
            $batchId = [regex]::Match($batchOutput, 'ISSUE035_BATCH_ID=([0-9a-fA-F-]{36})').Groups[1].Value
            $batchTaskId = [regex]::Match($batchOutput, 'ISSUE035_TASK_ID=([0-9a-fA-F-]{36})').Groups[1].Value
            if (-not $batchId -or -not $batchTaskId -or $batchOutput -notmatch 'ISSUE035_LIVE_MODEL_MCP=verified') { throw "批次在线模型/MCP 阶段未通过：$batchOutput" }
            Restart-WorkerAndAssert 'issue_035_live_acceptance.py' @('assert-approval-wait', '--task-id', $batchTaskId) 'WAITING_APPROVAL'
            $approvalOutput = Get-VerifyOutput @('/workspace/tests/issues/issue_035_live_acceptance.py', 'approve-batch', '--batch-id', $batchId, '--task-id', $batchTaskId)
            $orderId = [regex]::Match($approvalOutput, 'ISSUE035_ORDER_ID=([0-9a-fA-F-]{36})').Groups[1].Value
            if (-not $orderId -or $approvalOutput -notmatch 'ISSUE035_BATCH_ORDER_APPROVAL_QUOTA_AUDIT=verified') { throw "批次审批、订单、额度或审计事实核验失败：$approvalOutput" }
            Write-Host "在线证据：等待输入 task_id=$inputTaskId；混合批次 batch_id=$batchId；订单 task_id=$batchTaskId；order_id=$orderId。"
        } else {
            Write-Host '离线/真实 API-数据库回归通过；未执行真实模型联调，不能据此关闭 Issue #35。'
        }
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --no-color --tail 250 worker | Select-String -Pattern 'Traceback|Exception|Error|MCP|httpx|HTTP' -Context 1, 3
        throw
    }
    finally {
        & docker @composeArgs down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) { Write-Warning "隔离项目 $projectName 清理失败；只清理由本脚本创建的项目资源。" }
        & docker image rm $imageName 2>$null | Out-Null
        & docker image rm $webImageName 2>$null | Out-Null
        Pop-Location
    }
}
finally {
    Remove-Item -LiteralPath $overrideFile -ErrorAction SilentlyContinue
    foreach ($name in $environmentNames) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
