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
$edgeImageName = "tradepilot/edge-smoke:verify-$projectName"
$reportRoot = Join-Path $codeRoot 'artifacts/performance'
$reportPath = Join-Path $reportRoot $projectName
$reportContainerPath = "/workspace/artifacts/performance/$projectName"
$overrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-$projectName.yaml"
$composeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml', '--file', $overrideFile)
$names = @('POSTGRES_PORT', 'API_PORT', 'MCP_HOST_PORT', 'WEB_PORT', 'APP_ORIGIN', 'DEMO_MODE', 'DEMO_ALLOW_SELF_APPROVAL', 'DEMO_PASSWORD', 'SESSION_COOKIE_SECURE', 'TRADEPILOT_TEST_MODE', 'TRADEPILOT_MCP_INTEGRATION', 'TRADEPILOT_ISSUE041_EVIDENCE', 'TRADEPILOT_ONLINE_MODEL', 'MCP_CONTEXT_SIGNING_KEY', 'RUNTIME_CONFIG_ENCRYPTION_KEY', 'TRADEPILOT_ISSUE041_BROWSER_ORIGIN', 'TRADEPILOT_ISSUE041_BROWSER_PASSWORD')
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
    $browserRoot = Join-Path $codeRoot 'artifacts/performance/browsers'
    $portableChrome = @(Get-ChildItem -LiteralPath $browserRoot -Directory -Filter 'chrome-*' -ErrorAction SilentlyContinue | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter 'chrome.exe' -Recurse -ErrorAction SilentlyContinue } | Select-Object -ExpandProperty FullName)
    $portableEdge = @(Get-ChildItem -LiteralPath $browserRoot -Directory -Filter 'edge-*' -ErrorAction SilentlyContinue | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter 'msedge.exe' -Recurse -ErrorAction SilentlyContinue } | Select-Object -ExpandProperty FullName)
    $browserCandidates = @{
        Chrome = @('C:\Program Files\Google\Chrome\Application\chrome.exe', 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe', "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe") + $portableChrome
        Edge = @('C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', 'C:\Program Files\Microsoft\Edge\Application\msedge.exe', "${env:LOCALAPPDATA}\Microsoft\Edge\Application\msedge.exe") + $portableEdge
    }
    $browserResults = [ordered]@{}
    $browserFailures = @()
    foreach ($browserName in @('Chrome', 'Edge')) {
        $paths = @($browserCandidates[$browserName] | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)
        if (-not $paths.Count) { $browserResults[$browserName] = @{ status = 'unavailable'; detail = '未发现可执行浏览器，未测。' }; continue }
        $versionResults = @()
        foreach ($browserPath in $paths) {
            $version = (Get-Item -LiteralPath $browserPath).VersionInfo.ProductVersion
            $env:TRADEPILOT_ISSUE041_BROWSER_ORIGIN = "http://localhost:$env:WEB_PORT"
            $env:TRADEPILOT_ISSUE041_BROWSER_PASSWORD = $env:DEMO_PASSWORD
            $smokeScript = Join-Path $PSScriptRoot 'issue_041_browser_smoke.mjs'
            try {
                $browserOutput = & node $smokeScript --browser $browserPath --name $browserName --version $version --report-root $reportPath 2>&1
                if ($LASTEXITCODE -ne 0) { throw ($browserOutput -join "`n") }
                $versionResults += ($browserOutput -join '' | ConvertFrom-Json)
            }
            catch {
                $failure = "$browserName $version：$($_.Exception.Message)"
                $browserFailures += $failure
                $versionResults += @{ browser = $browserName; version = $version; result = 'failed'; error = $_.Exception.Message }
                Write-Warning $failure
            }
        }
        $majorVersions = @($versionResults | ForEach-Object { [int](($_.version -split '\.')[0]) } | Sort-Object -Unique)
        $allVersionsPassed = @($versionResults | Where-Object { $_.result -ne 'passed' }).Count -eq 0
        $previousMajorAvailable = $majorVersions.Count -ge 2 -and ($majorVersions -contains ($majorVersions[-1] - 1))
        $matrixStatus = if (-not $allVersionsPassed) { 'failed' } elseif ($previousMajorAvailable) { 'tested' } else { 'incomplete' }
        if ($matrixStatus -eq 'incomplete') { Write-Warning "$browserName 当前/前一主版本矩阵未补齐；本次实际测试版本：$($versionResults.version -join ', ')。" }
        $browserResults[$browserName] = @{ status = $matrixStatus; versions = $versionResults }
    }

    $edgePackage = Join-Path $browserRoot 'edge-linux-154.0.4258.62-amd64.deb'
    if (Test-Path -LiteralPath $edgePackage) {
        $expectedHash = 'F425DEA59F0A8BAC00201685A070E947543C57D135C130BA73E0863CF2C506B1'
        $actualHash = (Get-FileHash -LiteralPath $edgePackage -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) { throw 'Edge 154 Linux 官方安装包 SHA-256 不匹配。' }

        $edgeContext = Join-Path $reportPath 'edge-linux-build-context'
        New-Item -ItemType Directory -Path $edgeContext | Out-Null
        Copy-Item -LiteralPath $edgePackage -Destination (Join-Path $edgeContext 'edge-package.deb')
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'issue_041_edge_linux.Dockerfile') -Destination (Join-Path $edgeContext 'Dockerfile')
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'issue_041_browser_smoke.mjs') -Destination (Join-Path $edgeContext 'issue_041_browser_smoke.mjs')
        & docker build --tag $edgeImageName $edgeContext
        if ($LASTEXITCODE -ne 0) { throw '无法构建隔离 Edge 154 Linux smoke 镜像。' }
        $edgeBrowserVersion = ((& docker run --rm --entrypoint microsoft-edge-stable $edgeImageName --no-sandbox --version) -join '').Trim() -replace '^Microsoft Edge ', ''
        if ($LASTEXITCODE -ne 0 -or $edgeBrowserVersion -ne '154.0.4258.62') { throw "隔离 Edge 版本不符：$edgeBrowserVersion" }

        $edgeOverride = @(
            'services:',
            "  api:`n    environment:`n      APP_ORIGIN: http://localhost:5173",
            "  edge-smoke:`n    image: $edgeImageName`n    network_mode: service:web`n    environment:`n      TRADEPILOT_ISSUE041_BROWSER_ORIGIN: http://localhost:5173`n      TRADEPILOT_ISSUE041_BROWSER_PASSWORD: $env:DEMO_PASSWORD`n    volumes:`n      - ./artifacts:/reports"
        ) -join "`n"
        $edgeOverrideFile = Join-Path ([System.IO.Path]::GetTempPath()) "compose-edge-$projectName.yaml"
        try {
            $edgeOverride | Set-Content -LiteralPath $edgeOverrideFile -Encoding utf8
            $edgeComposeArgs = @('compose', '--project-name', $projectName, '--env-file', 'backend/.env', '--file', 'compose.yaml', '--file', $overrideFile, '--file', $edgeOverrideFile)
            & docker @edgeComposeArgs config --quiet
            if ($LASTEXITCODE -ne 0) { throw 'Edge 154 隔离 smoke Compose 配置无效。' }
            & docker @edgeComposeArgs up --detach --wait api web
            if ($LASTEXITCODE -ne 0) { throw 'Edge 154 浏览器测试所需的 API/前端未就绪。' }
            $edgeReportContainerPath = "/reports/performance/$projectName"
            & docker @edgeComposeArgs run --rm --no-deps edge-smoke --browser /usr/bin/microsoft-edge-stable --name Edge --version $edgeBrowserVersion --report-root $edgeReportContainerPath
            if ($LASTEXITCODE -ne 0) { throw 'Edge 154 Linux 浏览器 smoke 失败。' }
            $edgeResultPath = Join-Path $reportPath "edge-$edgeBrowserVersion-browser-smoke.json"
            $edgeResult = Get-Content -LiteralPath $edgeResultPath -Raw | ConvertFrom-Json
            $edgeResult | Add-Member -NotePropertyName platform -NotePropertyValue 'linux-container' -Force
            $versionResults = @($browserResults.Edge.versions) + @($edgeResult)
            $majorVersions = @($versionResults | ForEach-Object { [int](($_.version -split '\.')[0]) } | Sort-Object -Unique)
            $previousMajorAvailable = $majorVersions.Count -ge 2 -and ($majorVersions -contains ($majorVersions[-1] - 1))
            $browserResults.Edge = @{ status = if ($previousMajorAvailable) { 'tested' } else { 'incomplete' }; versions = $versionResults }
            if (-not $previousMajorAvailable) { Write-Warning 'Edge 前一主版本测试未能与当前系统版本形成相邻版本矩阵。' }
        }
        finally {
            Remove-Item -LiteralPath $edgeOverrideFile -ErrorAction SilentlyContinue
            & docker image rm $edgeImageName 2>$null | Out-Null
        }
    }
    $feedbackSamples = @()
    foreach ($browserName in @('Chrome', 'Edge')) {
        if ($browserResults[$browserName].status -ne 'tested') { $browserFailures += "$browserName 当前版/前一主版本兼容矩阵未完成。" }
        foreach ($versionResult in @($browserResults[$browserName].versions)) {
            foreach ($viewportResult in @($versionResult.viewports)) {
                $feedback = $viewportResult.interaction_feedback
                if (-not $feedback -or $feedback.result -ne 'passed') {
                    $browserFailures += "$browserName $($versionResult.version) 缺少有效的页面处理中反馈测量。"
                    continue
                }
                $feedbackSamples += [double]$feedback.latency_ms
            }
        }
    }
    if ($feedbackSamples.Count -ne 8) { $browserFailures += "页面反馈测量样本数为 $($feedbackSamples.Count)，预期 8 个浏览器/视口样本。" }
    $feedbackOrdered = @($feedbackSamples | Sort-Object)
    $feedbackP95 = if ($feedbackOrdered.Count) { $feedbackOrdered[[Math]::Min($feedbackOrdered.Count - 1, [Math]::Ceiling($feedbackOrdered.Count * 0.95) - 1)] } else { [double]::PositiveInfinity }
    if ($feedbackP95 -gt 300) { $browserFailures += "页面交互处理中反馈 P95 为 $([Math]::Round($feedbackP95, 3))ms，超过 300ms。" }
    $feedbackReport = [ordered]@{ action = '点击订单中心刷新列表并显示读取中状态'; sample_count = $feedbackOrdered.Count; threshold_ms = 300; p50_ms = if ($feedbackOrdered.Count) { $feedbackOrdered[[int][Math]::Floor(($feedbackOrdered.Count - 1) / 2)] } else { $null }; p95_ms = if ($feedbackOrdered.Count) { [Math]::Round($feedbackP95, 3) } else { $null }; max_ms = if ($feedbackOrdered.Count) { [Math]::Round($feedbackOrdered[-1], 3) } else { $null }; response_time_excluded = $true; result = if ($feedbackP95 -le 300 -and $feedbackOrdered.Count -eq 8) { 'passed' } else { 'failed' } }
    [ordered]@{ checked_at_utc = [DateTimeOffset]::UtcNow.ToString('o'); url = "http://localhost:$env:WEB_PORT/login?redirect=/orders"; flows = @('真实登录', '订单中心读取', '新任务工作台加载', '点击刷新后 300ms 内显示处理中反馈'); interaction_feedback = $feedbackReport; browser_matrix = $browserResults } | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $reportPath 'browser-compatibility.json') -Encoding utf8
    if ($browserFailures.Count) { throw "浏览器验收有失败项：$($browserFailures -join '；')" }
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
        & docker image rm $edgeImageName 2>$null | Out-Null
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
