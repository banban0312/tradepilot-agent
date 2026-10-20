param(
    [int]$SoakHours = 8,
    [switch]$SkipSoak,
    [switch]$SkipRegression
)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $codeRoot 'backend/.env'
$projectName = 'tp-issue041-' + [Guid]::NewGuid().ToString('N').ToLowerInvariant()
$imageName = "tradepilot/backend:verify-$projectName"
$webImageName = "tradepilot/web:verify-$projectName"
$reportRoot = Join-Path $codeRoot 'artifacts/performance'
$reportPath = Join-Path $reportRoot $projectName
$reportContainerPath = "/workspace/artifacts/performance/$projectName"
$overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-$projectName.yaml"
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml', '--file', $overrideFile)
$names = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_ALLOW_SELF_APPROVAL', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'TRADEPILOT_ISSUE041_EVIDENCE', 'TRADEPILOT_ONLINE_MODEL', 'MCP_CONTEXT_SIGNING_KEY', 'RUNTIME_CONFIG_ENCRYPTION_KEY')
$oldEnvironment = @{}
foreach ($name in $names) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }

if (-not (Test-Path -LiteralPath $envFile)) { throw '未找到 code/backend/.env，请先按 backend/.env.example 配置本机验收环境。' }
if (-not $SkipSoak -and ($SoakHours -lt 1 -or $SoakHours -gt 24)) { throw 'SoakHours 必须在 1 到 24 小时之间。' }

function Get-EnvValue([string]$Key) {
    $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))=" } | Select-Object -Last 1
    if (-not $line) { throw "backend/.env 缺少 $Key。" }
    return ($line -replace '^\s*[^=]+=','').Trim().Trim('"').Trim("'")
}

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return [string]$listener.LocalEndpoint.Port }
    finally { $listener.Stop() }
}

function Invoke-Compose([string[]]$Arguments) {
    & docker @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "隔离 Compose 命令失败，退出码 $LASTEXITCODE：$($Arguments -join ' ')" }
}

function Assert-DatabaseBackup {
    $dbId = (& docker @composeArgs ps -q db).Trim()
    if (-not $dbId) { throw '未找到隔离 PostgreSQL 容器。' }
    $dumpPath = Join-Path $reportPath 'postgres-coordinated.dump'
    $containerDump = '/tmp/issue041-coordinated.dump'
    $cloneDb = 'tp_issue041_restore'
    Invoke-Compose @('stop', 'api', 'worker')
    $dump = Start-Process -FilePath 'docker' -ArgumentList @('exec', $dbId, 'pg_dump', '-Fc', '-U', $script:dbUser, $script:dbName) -RedirectStandardOutput $dumpPath -Wait -PassThru -NoNewWindow
    if ($dump.ExitCode -ne 0 -or (Get-Item -LiteralPath $dumpPath).Length -lt 128) { throw '隔离数据库 pg_dump 失败或输出为空。' }
    & docker cp $dumpPath "${dbId}:$containerDump" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法将数据库快照复制回隔离 PostgreSQL。' }
    & docker exec $dbId createdb -U $script:dbUser $cloneDb
    if ($LASTEXITCODE -ne 0) { throw '无法创建临时恢复数据库。' }
    & docker exec $dbId pg_restore -U $script:dbUser --no-owner --dbname=$cloneDb $containerDump
    if ($LASTEXITCODE -ne 0) { throw 'pg_restore 恢复隔离数据库失败。' }
    $query = "SELECT 'agent_tasks', count(*) FROM agent_tasks UNION ALL SELECT 'task_waits', count(*) FROM task_waits UNION ALL SELECT 'task_approvals', count(*) FROM task_approvals UNION ALL SELECT 'orders', count(*) FROM orders UNION ALL SELECT 'order_items', count(*) FROM order_items UNION ALL SELECT 'quota_occupations', count(*) FROM quota_occupations UNION ALL SELECT 'input_artifacts', count(*) FROM input_artifacts UNION ALL SELECT 'field_evidence', count(*) FROM field_evidence ORDER BY 1;"
    $sourceCounts = (& docker exec $dbId psql -X -A -t -U $script:dbUser -d $script:dbName -c $query) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw '读取备份前业务数据计数失败。' }
    $restoredCounts = (& docker exec $dbId psql -X -A -t -U $script:dbUser -d $cloneDb -c $query) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $sourceCounts -ne $restoredCounts) { throw '恢复后任务、审批、订单、占额或来源证据数量不一致。' }
    $evidenceQuery = "SELECT count(*) FROM field_evidence e JOIN input_artifacts a ON a.id=e.artifact_id WHERE e.source_type IN ('FILE_PAGE','FILE_CELL');"
    $sourceEvidenceLinks = [int]((& docker exec $dbId psql -X -A -t -U $script:dbUser -d $script:dbName -c $evidenceQuery) -join '')
    if ($LASTEXITCODE -ne 0 -or $sourceEvidenceLinks -lt 1) { throw '备份夹具没有绑定真实附件记录的文件来源证据。' }
    $restoredEvidenceLinks = [int]((& docker exec $dbId psql -X -A -t -U $script:dbUser -d $cloneDb -c $evidenceQuery) -join '')
    if ($LASTEXITCODE -ne 0 -or $restoredEvidenceLinks -ne $sourceEvidenceLinks) { throw '恢复后文件来源证据与附件记录的关联数量不一致。' }
    $checkpointSchema = if ($dotenv['CHECKPOINT_SCHEMA']) { $dotenv['CHECKPOINT_SCHEMA'] } else { 'tradepilot_checkpoints' }
    $checkpointTable = (& docker exec $dbId psql -X -A -t -U $script:dbUser -d $cloneDb -c "SELECT to_regclass('$checkpointSchema.checkpoints');") -join ''
    if (-not $checkpointTable) { throw '恢复数据库缺少 LangGraph checkpoint 表。' }
    $fixturePath = Join-Path $reportPath 'checkpoint-fixture.json'
    $fixture = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
    $checkpointQuery = "SELECT count(*) FROM $checkpointSchema.checkpoints WHERE thread_id='$($fixture.task_id)';"
    $sourceCheckpoints = (& docker exec $dbId psql -X -A -t -U $script:dbUser -d $script:dbName -c $checkpointQuery) -join ''
    $restoredCheckpoints = (& docker exec $dbId psql -X -A -t -U $script:dbUser -d $cloneDb -c $checkpointQuery) -join ''
    if ($LASTEXITCODE -ne 0 -or $sourceCheckpoints -lt 2 -or $sourceCheckpoints -ne $restoredCheckpoints) { throw 'LangGraph 等待任务 checkpoint 数量未从备份完整恢复。' }
    foreach ($expected in @('task_approvals|', 'orders|', 'quota_occupations|', 'input_artifacts|')) {
        $match = $sourceCounts -split "`n" | Where-Object { $_ -like "$expected*" } | Select-Object -First 1
        if (-not $match -or [int]($match -split '\|')[-1] -lt 1) { throw "备份夹具缺少必需业务事实：$expected" }
    }

    $archivePath = "$reportContainerPath/attachments.tar.gz"
    $manifestPath = "$reportContainerPath/attachments-manifest.json"
    Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '/workspace/tests/issues/issue_041_attachment_backup.py', 'snapshot', $archivePath, $manifestPath)
    Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '/workspace/tests/issues/issue_041_attachment_backup.py', 'restore-verify', $archivePath, $manifestPath, "$reportContainerPath/attachments-restored")
    $restoreUrl = $dotenv['DATABASE_URL'] -replace '/[^/]+$', "/$cloneDb"
    Invoke-Compose @('run', '--rm', '--no-deps', '--env', "DATABASE_URL=$restoreUrl", '--env', "TRADEPILOT_ISSUE041_RESTORED_ATTACHMENTS=$reportContainerPath/attachments-restored", 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-q', '-x', '--tb=short', '/workspace/tests/issues/test_issue_041.py::test_issue_041_resume_restored_checkpoint')
    & docker exec $dbId psql -X -q -U $script:dbUser -d postgres -c "DROP DATABASE $cloneDb;" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '清理临时恢复数据库失败。' }
    [ordered]@{ database_counts = $sourceCounts; file_source_evidence_links_before = $sourceEvidenceLinks; file_source_evidence_links_restored = $restoredEvidenceLinks; checkpoint_table = $checkpointTable; checkpoint_fixture_task_id = $fixture.task_id; checkpoint_rows_before = [int]$sourceCheckpoints; checkpoint_rows_restored = [int]$restoredCheckpoints; restored_task_resume = 'WAITING_INPUT -> WAITING_APPROVAL'; order_approval_quota_fixture = 'present'; attachment_archive = 'attachments.tar.gz'; attachment_database_hash_links = 'verified'; restore = 'passed' } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $reportPath 'backup-restore.json') -Encoding utf8
}

function Test-ResponsiveBrowser {
    $browserCandidates = @{
        Chrome = @('C:\Program Files\Google\Chrome\Application\chrome.exe', 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe', "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
        Edge = @('C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', 'C:\Program Files\Microsoft\Edge\Application\msedge.exe', "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe")
    }
    $browserResults = [ordered]@{}
    foreach ($browserName in @('Chrome', 'Edge')) {
        $browserPath = $browserCandidates[$browserName] | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $browserPath) { $browserResults[$browserName] = @{ status = 'unavailable'; detail = '本机未安装可执行浏览器，未测。' }; continue }
        $version = (Get-Item -LiteralPath $browserPath).VersionInfo.ProductVersion
        $browserResult = [ordered]@{ status = 'installed'; version = $version; executable = $browserPath; viewports = @() }
        if ($browserName -eq 'Edge') {
            foreach ($size in @(@{ width = 1366; height = 768 }, @{ width = 1920; height = 1080 })) {
                $screenshot = Join-Path $reportPath "edge-$($size.width)x$($size.height).png"
                $profile = Join-Path ([System.IO.Path]::GetTempPath()) "$projectName-edge-$($size.width)"
                $url = "http://localhost:$env:WEB_PORT/login?redirect=/"
                $arguments = @('--headless', '--disable-gpu', '--no-first-run', "--user-data-dir=$profile", "--window-size=$($size.width),$($size.height)", "--screenshot=$screenshot", $url)
                $edgeProcess = Start-Process -FilePath $browserPath -ArgumentList $arguments -PassThru -WindowStyle Hidden
                if (-not $edgeProcess.WaitForExit(30000)) { Stop-Process -Id $edgeProcess.Id -Force; throw "Edge $($size.width)x$($size.height) 页面截图超时。" }
                Start-Sleep -Milliseconds 500
                if (-not (Test-Path -LiteralPath $screenshot)) { throw "Edge $($size.width)x$($size.height) 未生成截图。" }
                $bytes = [System.IO.File]::ReadAllBytes($screenshot)
                $actualWidth = [System.Net.IPAddress]::NetworkToHostOrder([BitConverter]::ToInt32($bytes, 16))
                $actualHeight = [System.Net.IPAddress]::NetworkToHostOrder([BitConverter]::ToInt32($bytes, 20))
                if ($actualWidth -ne $size.width -or $actualHeight -ne $size.height) { throw "Edge 截图尺寸错误：$actualWidth x $actualHeight。" }
                $browserResult.viewports += @{ width = $actualWidth; height = $actualHeight; result = 'rendered'; screenshot = [System.IO.Path]::GetFileName($screenshot) }
                $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
                $profilePath = [System.IO.Path]::GetFullPath($profile)
                if ($profilePath.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) { Remove-Item -LiteralPath $profilePath -Recurse -Force -ErrorAction SilentlyContinue }
            }
            $browserResult.status = 'tested_current_only'
            $browserResult.previous_major = 'unavailable; 未安装独立旧版本'
        }
        $browserResults[$browserName] = $browserResult
    }
    [ordered]@{ checked_at_utc = [DateTimeOffset]::UtcNow.ToString('o'); url = "http://localhost:$env:WEB_PORT/login?redirect=/"; browser_matrix = $browserResults; note = '截图验证页面在目标分辨率可加载，不等同于全部业务主流程验收。' } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $reportPath 'browser-compatibility.json') -Encoding utf8
}

try {
    $dotenv = @{}
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $dotenv[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
    }
    foreach ($required in @('POSTGRES_DB','POSTGRES_USER','EVALUATION_POSTGRES_DB','EVALUATION_POSTGRES_USER','EVALUATION_POSTGRES_PASSWORD')) { if (-not $dotenv[$required]) { throw "backend/.env 缺少 $required。" } }
    $script:dbName = $dotenv['POSTGRES_DB']; $script:dbUser = $dotenv['POSTGRES_USER']
    $ports = @(); while ($ports.Count -lt 4) { $candidate = Get-FreeLocalPort; if ($candidate -notin $ports) { $ports += $candidate } }
    $env:POSTGRES_PORT = $ports[0]; $env:API_PORT = $ports[1]; $env:MCP_HOST_PORT = $ports[2]; $env:WEB_PORT = $ports[3]
    $env:APP_ORIGIN = "http://localhost:$($ports[3])"; $env:DEMO_MODE = 'true'; $env:DEMO_ALLOW_SELF_APPROVAL = 'false'
    $env:DEMO_PASSWORD = 'TP-Issue041-' + [Guid]::NewGuid().ToString('N')
    $env:SESSION_COOKIE_SECURE = 'false'; $env:TRADEPILOT_TEST_MODE = '1'; $env:TRADEPILOT_MCP_INTEGRATION = '1'; $env:TRADEPILOT_ONLINE_MODEL = '0'
    $key = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Fill($key)
    $env:MCP_CONTEXT_SIGNING_KEY = [Convert]::ToBase64String($key)
    $env:RUNTIME_CONFIG_ENCRYPTION_KEY = [Convert]::ToBase64String($key).Replace('+','-').Replace('/','_')
    [Array]::Clear($key, 0, $key.Length)

    @(
        'services:',
        "  api:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
        "  mcp:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
        "  worker:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''",
        "  web:`n    image: $webImageName",
        "  verify:`n    image: $imageName`n    environment:`n      MODEL_PROVIDER: ''`n      MODEL_NAME: ''`n      MODEL_API_KEY: ''`n      MODEL_BASE_URL: ''`n      OCR_PROVIDER: local_tesseract`n      OCR_API_KEY: ''`n      TRADEPILOT_TEST_MODE: '1'`n      TRADEPILOT_ISSUE041_EVIDENCE: $reportContainerPath`n      PYTHONPATH: /workspace:/app`n    volumes:`n      - ./artifacts:/workspace/artifacts"
    ) -join "`n" | Set-Content -LiteralPath $overrideFile -Encoding utf8

    Push-Location $codeRoot
    try {
        if (Test-Path -LiteralPath $reportPath) { throw "证据目录已存在，停止以保护文件：$reportPath" }
        New-Item -ItemType Directory -Path $reportPath | Out-Null
        Invoke-Compose @('config', '--quiet')
        & docker image tag tradepilot/web:local $webImageName
        if ($LASTEXITCODE -ne 0) { throw '无法为隔离 Compose 项目建立前端镜像标签。' }
        Invoke-Compose @('build', 'api')
        Invoke-Compose @('up', '--detach', '--wait', 'db', 'eval-db', 'mcp', 'api', 'web')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'upgrade', 'head')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'alembic', 'check')
        Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'uv', 'run', '--frozen', 'python', '-m', 'app.seed')
        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-q', '-x', '--tb=short', '/workspace/tests/issues/test_issue_041.py', '-k', 'not resume_restored_checkpoint and not seed_waiting_checkpoint_for_backup_restore')
        if (-not $SkipRegression) { Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-q', '-x', '--tb=short', '/workspace/tests/issues/test_issue_006.py::test_retention_defaults_to_30_days_and_protects_live_ordered_and_audit_data', '/workspace/tests/issues/test_issue_012.py::test_invalid_or_unavailable_model_tools_fail_without_mcp_side_effect', '/workspace/tests/issues/test_issue_022.py::test_artifact_upload_hash_storage_and_department_authorization', '/workspace/tests/issues/test_issue_029.py', '/workspace/tests/issues/test_issue_035.py', '/workspace/tests/issues/test_issue_016.py::test_twenty_replays_create_one_order_and_one_quota_occupation') }
        Invoke-Compose @('run', '--rm', '--no-deps', 'verify', 'uv', 'run', '--frozen', 'python', '-m', 'pytest', '-q', '-x', '--tb=short', '/workspace/tests/issues/test_issue_041.py::test_issue_041_seed_waiting_checkpoint_for_backup_restore')
        Test-ResponsiveBrowser
        Assert-DatabaseBackup

        if (-not $SkipSoak) {
            Invoke-Compose @('up', '--detach', '--wait', 'api', 'worker')
            Write-Host "开始 $SoakHours 小时隔离 Compose 稳定性检查。页面服务仅绑定本机端口用于尺寸截图，不纳入长稳业务操作。"
            $duration = [TimeSpan]::FromHours($SoakHours); $startedAt = [DateTimeOffset]::UtcNow; $endAt = $startedAt.Add($duration)
            $sampleFile = Join-Path $reportPath 'stability-samples.jsonl'; $unexpectedExits = 0; $sampleCount = 0; $initialRestartCounts = @{}; $containerIds = @{}
            while ([DateTimeOffset]::UtcNow -lt $endAt) {
                $now = [DateTimeOffset]::UtcNow
                $api = $null; $apiError = $null
                try { $api = Invoke-RestMethod -Uri "http://localhost:$($ports[1])/health/ready" -TimeoutSec 5; if ($api.status -ne 'ready') { throw 'API readiness 非 ready' } }
                catch { $apiError = $_.Exception.Message }
                $states = @{}
                foreach ($service in @('db','eval-db','mcp','api','worker')) {
                    $id = (& docker @composeArgs ps -q $service).Trim()
                    if (-not $id) { $states[$service] = 'missing'; $unexpectedExits++; continue }
                    $state = (& docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}' $id).Trim()
                    $parts = $state -split '\|'
                    $restartCount = [int]$parts[2]
                    if (-not $initialRestartCounts.ContainsKey($service)) {
                        $initialRestartCounts[$service] = $restartCount
                        $containerIds[$service] = $id
                    }
                    elseif ($restartCount -ne $initialRestartCounts[$service] -or $id -ne $containerIds[$service]) {
                        $unexpectedExits++
                    }
                    if ($parts[0] -ne 'running' -or $parts[1] -ne 'healthy') { $unexpectedExits++ }
                    $states[$service] = @{ status = $parts[0]; health = $parts[1]; restart_count = $restartCount }
                }
                $sample = [ordered]@{ timestamp_utc = $now.ToString('o'); api_ready = ($null -ne $api); api_error = $apiError; services = $states }
                $sample | ConvertTo-Json -Depth 6 -Compress | Add-Content -LiteralPath $sampleFile -Encoding utf8
                $sampleCount++
                if ($apiError -or $unexpectedExits -gt 0) { throw "稳定性检查出现异常；报告：$sampleFile" }
                $remaining = ($endAt - [DateTimeOffset]::UtcNow).TotalSeconds
                if ($remaining -gt 0) { Start-Sleep -Seconds ([Math]::Min(60, [Math]::Ceiling($remaining))) }
            }
            [ordered]@{ started_utc = $startedAt.ToString('o'); finished_utc = [DateTimeOffset]::UtcNow.ToString('o'); duration_hours = $SoakHours; sample_interval_seconds = 60; sample_count = $sampleCount; unexpected_process_exits = $unexpectedExits; result = 'passed' } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $reportPath 'stability-summary.json') -Encoding utf8
        }
        Write-Host "验收证据：$reportPath"
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        & docker @composeArgs logs --tail 100 --no-color db eval-db api mcp worker 2>$null
        throw
    }
    finally {
        try { Invoke-Compose @('down', '--volumes', '--remove-orphans') }
        catch { Write-Warning "只清理本次隔离项目 $projectName 失败；核查报告目录。" }
        & docker image rm $imageName 2>$null | Out-Null
        & docker image rm $webImageName 2>$null | Out-Null
        Pop-Location
    }
}
finally {
    Remove-Item -LiteralPath $overrideFile -ErrorAction SilentlyContinue
    foreach ($name in $names) {
        if ($null -eq $oldEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $oldEnvironment[$name] }
    }
}
