param(
    [switch]$ListOnly,
    [switch]$UiSelfTest,
    [string]$RenderPath,
    [string]$BackendRequestBase64
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$WriterConfigPath = if ($env:SESSION_WRITER_CONFIG) { $env:SESSION_WRITER_CONFIG } else { Join-Path $env:USERPROFILE '.config\session-writer\config.json' }
if (-not (Test-Path -LiteralPath $WriterConfigPath)) { throw '请先将 config.example.json 复制到 ~/.config/session-writer/config.json 并填写机器配置。' }
$WriterConfig = Get-Content -LiteralPath $WriterConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$MacHost = [string]$WriterConfig.mac_ssh_alias
$Backend = [string]$WriterConfig.mac_backend_path
$MacPython = if ($WriterConfig.mac_python) { [string]$WriterConfig.mac_python } else { '/usr/bin/python3' }
if ($MacHost -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' -or (-not [string]::IsNullOrWhiteSpace($Backend) -and (-not $Backend.StartsWith('/') -or $Backend.Contains('YOUR_MAC_USER'))) -or -not $MacPython.StartsWith('/')) { throw '请填写有效的 SSH 别名和 Python 路径；可选后台路径必须是绝对路径。' }
$NL = [Environment]::NewLine
$script:CallerHostName = [System.Net.Dns]::GetHostName()
$script:LogDirectory = Join-Path $env:LOCALAPPDATA 'SessionWriter\logs'
$script:LogFile = Join-Path $script:LogDirectory 'windows-ui.jsonl'

# Keep only bounded, structured metadata. Never persist raw SSH output, command
# arguments or exception text: they may contain account credentials.
function Write-WriterLog {
    param([string]$Event, [string]$Kind, [string]$RequestId, [hashtable]$Details = @{})
    $mutex = $null
    $acquired = $false
    $stream = $null
    try {
        $mutex = New-Object System.Threading.Mutex($false, 'Local\SessionWriterLog')
        try { $acquired = $mutex.WaitOne(200) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { return }
        [void][System.IO.Directory]::CreateDirectory($script:LogDirectory)
        if ([System.IO.File]::Exists($script:LogFile) -and (Get-Item -LiteralPath $script:LogFile).Length -ge 2097152) {
            if ([System.IO.File]::Exists($script:LogFile + '.2')) { [System.IO.File]::Delete($script:LogFile + '.2') }
            if ([System.IO.File]::Exists($script:LogFile + '.1')) { [System.IO.File]::Move($script:LogFile + '.1', $script:LogFile + '.2') }
            [System.IO.File]::Move($script:LogFile, $script:LogFile + '.1')
        }
        $entry = [ordered]@{
            timestamp = [DateTime]::UtcNow.ToString('o')
            source = 'windows-ui'
            toolVersion = '4.0'
            event = $Event
            kind = $(if ($Kind -in @('snapshot', 'plan', 'claim', 'logs')) { $Kind } else { 'other' })
            requestId = $RequestId
            uiPid = $PID
        }
        foreach ($name in @('durationMs', 'exitCode', 'recordsCount', 'machinesCount', 'canApply', 'ok', 'failureCode')) {
            if ($Details.ContainsKey($name)) { $entry[$name] = $Details[$name] }
        }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($entry | ConvertTo-Json -Compress -Depth 3) + $NL)
        $stream = [System.IO.File]::Open($script:LogFile, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        $stream.Write($bytes, 0, $bytes.Length)
    } catch {
        # A log storage problem must not block discovery or handoff.
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($acquired) { try { $mutex.ReleaseMutex() } catch { } }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}
function Write-RequestLog {
    param($Request, [string]$Event, [hashtable]$Details = @{})
    $Details.durationMs = [long]$Request.Stopwatch.ElapsedMilliseconds
    Write-WriterLog -Event $Event -Kind $Request.Kind -RequestId $Request.RequestId -Details $Details
}
function Save-BackendLogExport {
    param($Value)
    $events = @($Value.events | Select-Object -Last 100)
    $export = [ordered]@{ fetchedAt = [DateTime]::UtcNow.ToString('o'); source = 'mac-backend'; events = $events }
    $json = $export | ConvertTo-Json -Depth 12
    if ([System.Text.Encoding]::UTF8.GetByteCount($json) -gt 2097152) { throw '远端日志超过导出大小限制。' }
    $mutex = New-Object System.Threading.Mutex($false, 'Local\SessionWriterLog')
    $acquired = $false
    try {
        try { $acquired = $mutex.WaitOne(200) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw '日志正由另一个窗口使用，请稍后重试。' }
        [void][System.IO.Directory]::CreateDirectory($script:LogDirectory)
        [System.IO.File]::WriteAllText((Join-Path $script:LogDirectory 'backend-last.json'), $json, (New-Object System.Text.UTF8Encoding($false)))
    } finally {
        if ($acquired) { try { $mutex.ReleaseMutex() } catch { } }
        $mutex.Dispose()
    }
}

# Quote each Windows argv entry and each remote shell argument separately.
function ConvertTo-RemoteArgument {
    param([string]$Value)
    $quote = [string][char]39
    $escapedQuote = $quote + [char]34 + $quote + [char]34 + $quote
    return $quote + $Value.Replace($quote, $escapedQuote) + $quote
}
function ConvertTo-ProcessArgument {
    param([string]$Value)
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}
function Get-LocalWriterContext {
    $identity = $null
    try {
        . (Join-Path $PSScriptRoot 'windows_controller.ps1')
        $processes = @(Get-CimInstance Win32_Process -OperationTimeoutSec 5)
        $identity = Get-WriterIdentity -Processes $processes -MacAlias $MacHost
    } catch {
        # A local observation failure is not evidence that this computer is offline.
    }
    $epoch = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) / 1000.0
    $context = @{ schemaVersion = 1; platform = 'windows'; hostName = $script:CallerHostName; observedAt = $epoch; identity = $identity }
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($context | ConvertTo-Json -Depth 8 -Compress)))
}

function Get-RemoteBackendPackage {
    param([string[]]$Arguments, [string]$ApplicationRoot = $PSScriptRoot)
    $files = @{}
    foreach ($name in @('session_writer.py','writer_service.py','writer_force.py','writer_desktop.py','writer_history.py','writer_rpc.py','writer_log.py','caller_context.py','discovery_adapter.py','runtime_config.py','ssh_bridge.py','mac_controller.py','windows_controller.ps1')) {
        $files[$name] = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $ApplicationRoot $name)))
    }
    $config = @{}
    foreach ($name in @('codex_home','codex_app','codex_binary','ssh_controllers','enable_handoff','windows_ssh_alias','windows_peer_aliases','mac_ssh_alias','excluded_thread_ids')) {
        if ($null -ne $WriterConfig.PSObject.Properties[$name]) { $config[$name] = $WriterConfig.$name }
    }
    $json = @{ files=$files; config=$config; argv=@($Arguments) } | ConvertTo-Json -Depth 12 -Compress
    if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 2097152) { throw 'Backend package exceeds size limit' }
    return $json
}

# Run local inspection and SSH in a child so WMI/network waits never block the GUI.
if (-not [string]::IsNullOrWhiteSpace($BackendRequestBase64)) {
    $requestStage = 'decode_arguments'
    try {
        # PS 5.1 keeps ConvertFrom-Json arrays as one pipeline object; an outer
        # @(... | ConvertFrom-Json) would turn this into a nested array.
        $requestArguments = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($BackendRequestBase64)) | ConvertFrom-Json
        if ($requestArguments.Count -lt 1 -or $requestArguments[0] -notin @('snapshot','plan','claim','logs')) { throw 'Invalid backend operation' }
        $requestStage = 'local_identity'
        $contextArgument = Get-LocalWriterContext
        $requestStage = 'ssh_request'
        $backendArguments = @($requestArguments) + @('--client-context', $contextArgument)
        if ([string]::IsNullOrWhiteSpace($Backend)) {
            $bootstrap = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'remote_bootstrap.py'))
            $remoteParts = @($MacPython, '-c', $bootstrap)
            $remoteCommand = ($remoteParts | ForEach-Object { ConvertTo-RemoteArgument ([string]$_) }) -join ' '
            Get-RemoteBackendPackage -Arguments $backendArguments | & ssh.exe -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 $MacHost $remoteCommand
        } else {
            $remoteParts = @($MacPython, $Backend) + $backendArguments
            $remoteCommand = ($remoteParts | ForEach-Object { ConvertTo-RemoteArgument ([string]$_) }) -join ' '
            & ssh.exe -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 $MacHost $remoteCommand
        }
        exit $LASTEXITCODE
    } catch {
        @{ ok = $false; errorCode = 'WINDOWS_REQUEST_FAILED'; stage = $requestStage;
           exceptionType = $_.Exception.GetType().Name; line = $_.InvocationInfo.ScriptLineNumber;
           message = ('Windows 请求失败（' + $requestStage + '）；不代表 Windows 电脑离线。') } | ConvertTo-Json -Compress
        exit 1
    }
}
function Start-MacWriterRequest {
    param([string[]]$ToolArguments, [string]$Kind, [int]$TimeoutSeconds = 75)
    $requestId = [Guid]::NewGuid().ToString('N')
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    Write-WriterLog -Event 'request_started' -Kind $Kind -RequestId $requestId
    $serialized = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject @($ToolArguments) -Compress)))
    $childArguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $PSCommandPath, '-BackendRequestBase64', $serialized)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = (Get-Process -Id $PID).Path
    $startInfo.Arguments = ($childArguments | ForEach-Object { ConvertTo-ProcessArgument ([string]$_) }) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw '无法启动 SSH 连接。' }
    } catch {
        Write-WriterLog -Event 'request_failed' -Kind $Kind -RequestId $requestId -Details @{ failureCode = 'start_failed'; durationMs = [long]$stopwatch.ElapsedMilliseconds }
        $process.Dispose()
        throw
    }
    return [pscustomobject]@{
        Process = $process
        OutputTask = $process.StandardOutput.ReadToEndAsync()
        ErrorTask = $process.StandardError.ReadToEndAsync()
        Kind = $Kind
        RequestId = $requestId
        Stopwatch = $stopwatch
        TimeoutSeconds = $TimeoutSeconds
        Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    }
}
function Get-CompletedRequest {
    param($Request)
    if ([DateTime]::UtcNow -gt $Request.Deadline) {
        Write-RequestLog $Request 'request_failed' @{ failureCode = 'timeout' }
        try { if (-not $Request.Process.HasExited) { $Request.Process.Kill() } } catch { }
        $Request.Process.Dispose()
        if ($Request.Kind -eq 'claim') { throw '应用操作超过等待时间，结果尚未确认。请刷新检查当前占用者；本工具不会自动重试应用。' }
        throw "读取超过 $($Request.TimeoutSeconds) 秒。请检查 Mac 在线状态、SSH 连接及服务；现有列表已标为过期。"
    }
    if (-not $Request.Process.HasExited -or -not $Request.OutputTask.IsCompleted -or -not $Request.ErrorTask.IsCompleted) { return $null }
    $outputText = [string]$Request.OutputTask.Result
    $errorText = [string]$Request.ErrorTask.Result
    $exitCode = $Request.Process.ExitCode
    $Request.Process.Dispose()
    if ($exitCode -ne 0) {
        $failureCode = 'nonzero_exit'
        $parsedFailure = $null
        try { $parsedFailure = $outputText | ConvertFrom-Json } catch { }
        if ([string]$parsedFailure.errorCode -match '^[A-Za-z0-9_]{1,80}$') { $failureCode = [string]$parsedFailure.errorCode }
        Write-RequestLog $Request 'request_failed' @{ failureCode = $failureCode; exitCode = $exitCode }
        if (-not [string]::IsNullOrWhiteSpace([string]$parsedFailure.message)) { throw ([string]$parsedFailure.message + ' [' + $failureCode + ']') }
        $details = ($errorText + $NL + $outputText).Trim()
        if ([string]::IsNullOrWhiteSpace($details)) { $details = "SSH 返回 $exitCode。" }
        throw $details
    }
    if ([string]::IsNullOrWhiteSpace($outputText)) {
        Write-RequestLog $Request 'request_failed' @{ failureCode = 'empty_output'; exitCode = $exitCode }
        throw '服务没有返回数据。'
    }
    try { $value = $outputText | ConvertFrom-Json } catch {
        Write-RequestLog $Request 'request_failed' @{ failureCode = 'invalid_json'; exitCode = $exitCode }
        throw
    }
    $details = @{ exitCode = $exitCode }
    if ($Request.Kind -eq 'snapshot') { $details.recordsCount = @($value.records).Count; $details.machinesCount = @($value.machines).Count }
    if ($Request.Kind -eq 'plan') { $details.canApply = [bool]$value.canApply }
    if ($Request.Kind -eq 'claim') { $details.ok = [bool]$value.ok }
    Write-RequestLog $Request 'request_completed' $details
    return [pscustomobject]@{ Value = $value }
}
if ($ListOnly) {
    $request = Start-MacWriterRequest -ToolArguments @('snapshot', '--format', 'json') -Kind 'snapshot'
    do {
        $completed = Get-CompletedRequest $request
        if ($null -eq $completed) { Start-Sleep -Milliseconds 100 }
    } while ($null -eq $completed)
    $completed.Value | ConvertTo-Json -Depth 12 -Compress
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not ('SessionWriterChoice' -as [type])) {
    Add-Type -TypeDefinition @'
public sealed class SessionWriterChoice {
    public string Label { get; set; }
    public object Machine { get; set; }
    public override string ToString() { return Label ?? ""; }
}
'@
}
[System.Windows.Forms.Application]::EnableVisualStyles()
$script:request = $null
$script:snapshot = $null
$script:recordsById = @{}
$script:preferredMachineId = ''
$script:refreshingControls = $false
$script:stale = $true
$script:workflowBusy = $false
$script:pendingSelection = $null
$script:lastError = ''
$script:refreshCount = 0
$script:snapshotRequestCount = 0
$script:suppressedRequestCount = 0
$script:lastRenderedAt = $null

$form = New-Object System.Windows.Forms.Form
$form.Text = '会话写入者'
$form.Size = New-Object System.Drawing.Size(1090, 770)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

function Add-Label {
    param([string]$Text, [int]$X, [int]$Y, [int]$Width, [int]$Height, [string]$Anchor = 'Top, Left')
    $control = New-Object System.Windows.Forms.Label
    $control.Text = $Text
    $control.Location = New-Object System.Drawing.Point($X, $Y)
    $control.Size = New-Object System.Drawing.Size($Width, $Height)
    $control.Anchor = $Anchor
    $form.Controls.Add($control)
    return $control
}
function Add-Button {
    param([string]$Text, [int]$X, [int]$Y, [int]$Width, [string]$Anchor = 'Top, Right')
    $control = New-Object System.Windows.Forms.Button
    $control.Text = $Text
    $control.Location = New-Object System.Drawing.Point($X, $Y)
    $control.Size = New-Object System.Drawing.Size($Width, 32)
    $control.Anchor = $Anchor
    $form.Controls.Add($control)
    return $control
}
$taskLabel = Add-Label '选择会话' 20 20 160 25
$contextLabel = Add-Label ('操作端：' + $script:CallerHostName + '（本机 Windows） · 会话数据：正在读取…') 20 47 1030 26 'Top, Left, Right'
$autoRefreshBox = New-Object System.Windows.Forms.CheckBox
$autoRefreshBox.Text = '自动刷新（10 秒）'
$autoRefreshBox.Location = New-Object System.Drawing.Point(590, 14)
$autoRefreshBox.Size = New-Object System.Drawing.Size(170, 30)
$autoRefreshBox.Anchor = 'Top, Right'
$autoRefreshBox.Checked = $true
$form.Controls.Add($autoRefreshBox)
$refreshButton = Add-Button '刷新' 780 13 95
$discoverButton = Add-Button '刷新 SSH 主机' 890 13 160

$taskGrid = New-Object System.Windows.Forms.DataGridView
$taskGrid.Location = New-Object System.Drawing.Point(20, 80)
$taskGrid.Size = New-Object System.Drawing.Size(1030, 276)
$taskGrid.Anchor = 'Top, Bottom, Left, Right'
$taskGrid.ReadOnly = $true
$taskGrid.MultiSelect = $false
$taskGrid.SelectionMode = 'FullRowSelect'
$taskGrid.AutoGenerateColumns = $false
$taskGrid.AllowUserToAddRows = $false
$taskGrid.AllowUserToDeleteRows = $false
$taskGrid.AllowUserToResizeRows = $false
$taskGrid.RowHeadersVisible = $false
$taskGrid.AutoSizeColumnsMode = 'Fill'
$taskGrid.AutoSizeRowsMode = 'AllCells'
foreach ($columnSpec in @(@('会话名称', 24, 200), @('会话 ID', 33, 300), @('创建时间', 19, 180), @('当前占用者', 24, 220))) {
    $column = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
    $column.HeaderText = [string]$columnSpec[0]
    $column.FillWeight = [single]$columnSpec[1]
    $column.MinimumWidth = [int]$columnSpec[2]
    $column.SortMode = 'Automatic'
    [void]$taskGrid.Columns.Add($column)
}
$form.Controls.Add($taskGrid)
$currentOwnerLabel = Add-Label '当前占用主机：正在读取…' 20 342 1030 26 'Bottom, Left, Right'
$currentOwnerLabel.Visible = $false
$ownerStatusLabel = Add-Label '状态：等待快照' 20 371 1030 34 'Bottom, Left, Right'
$writerLabel = Add-Label '目标写入主机' 20 418 115 25 'Bottom, Left'
$writerBox = New-Object System.Windows.Forms.ComboBox
$writerBox.Location = New-Object System.Drawing.Point(135, 411)
$writerBox.Size = New-Object System.Drawing.Size(915, 30)
$writerBox.Anchor = 'Bottom, Left, Right'
$writerBox.DropDownStyle = 'DropDownList'
$writerBox.DisplayMember = 'Label'
$form.Controls.Add($writerBox)
$machineReasonLabel = Add-Label '' 135 443 915 25 'Bottom, Left, Right'
$forceContinueBox = New-Object System.Windows.Forms.CheckBox
$forceContinueBox.Text = '强制接管并继续被中断代理'
$forceContinueBox.Location = New-Object System.Drawing.Point(20, 467)
$forceContinueBox.Size = New-Object System.Drawing.Size(580, 25)
$forceContinueBox.Anchor = 'Bottom, Left'
$forceContinueBox.Checked = $false
$forceContinueBox.AccessibleName = '强制接管并继续被中断代理'
$form.Controls.Add($forceContinueBox)
$diagnosticsLabel = Add-Label '连接与基本排错' 20 501 870 25 'Bottom, Left, Right'
$openLogsButton = Add-Button '打开日志' 940 493 110 'Bottom, Right'
$openLogsButton.AccessibleName = '打开日志'
$diagnosticsBox = New-Object System.Windows.Forms.TextBox
$diagnosticsBox.Location = New-Object System.Drawing.Point(20, 530)
$diagnosticsBox.Size = New-Object System.Drawing.Size(1030, 117)
$diagnosticsBox.Anchor = 'Bottom, Left, Right'
$diagnosticsBox.Multiline = $true
$diagnosticsBox.ReadOnly = $true
$diagnosticsBox.ScrollBars = 'Vertical'
$diagnosticsBox.Text = '正在读取会话、占用者及可用机器…'
$form.Controls.Add($diagnosticsBox)
$refreshStatusLabel = Add-Label '尚未刷新' 20 666 780 45 'Bottom, Left, Right'
$confirmButton = Add-Button '应用' 830 675 100 'Bottom, Right'
$confirmButton.Enabled = $false
$cancelButton = Add-Button '关闭' 950 675 100 'Bottom, Right'
$cancelButton.Add_Click({ $form.Close() })

# Resize only after anchored controls exist, retaining margins on smaller screens.
$workingArea = [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position).WorkingArea
$initialWidth = [Math]::Min(1000, [Math]::Max(320, $workingArea.Width - 24))
$initialHeight = [Math]::Min(735, [Math]::Max(480, $workingArea.Height - 24))
$form.MinimumSize = New-Object System.Drawing.Size([Math]::Min(900, $initialWidth), [Math]::Min(640, $initialHeight))
$form.Size = New-Object System.Drawing.Size($initialWidth, $initialHeight)
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(($workingArea.Left + [int](($workingArea.Width - $initialWidth) / 2)), ($workingArea.Top + [int](($workingArea.Height - $initialHeight) / 2)))

function New-MachineChoice {
    param([string]$Label, $Machine)
    $choice = New-Object SessionWriterChoice
    $choice.Label = $Label
    $choice.Machine = $Machine
    return $choice
}
function Get-SourceLabel {
    param([string]$Source)
    switch -Regex ($Source) {
        '^local$' { return 'Mac 会话数据主机' }
        '^verified-ssh-peer$' { return 'Windows 接管连接' }
        'ssh' { return 'SSH 配置' }
        default { return '未确认来源' }
    }
}

function Get-SelectedRecord {
    if ($taskGrid.SelectedRows.Count -eq 0) { return $null }
    return $script:recordsById[[string]$taskGrid.SelectedRows[0].Tag]
}
function Get-SelectedMachine {
    if ($null -eq $writerBox.SelectedItem) { return $null }
    return $writerBox.SelectedItem.Machine
}
function Update-SelectionDetails {
    if ($script:refreshingControls) { return }
    $record = Get-SelectedRecord
    if ($null -eq $record) {
        $currentOwnerLabel.Text = '当前占用主机：请选择会话'
        $ownerStatusLabel.Text = '状态：' + $(if ($null -eq $script:snapshot) { '等待快照' } else { '没有选择会话' })
    } else {
        $owner = [string]$record.writerHostName
        if ([string]::IsNullOrWhiteSpace($owner)) { $owner = '未确认' }
        $currentOwnerLabel.Text = '当前占用主机：' + $owner
        $ownerStatusLabel.Text = '状态：' + [string]$record.statusLabel
        if (-not [string]::IsNullOrWhiteSpace([string]$record.diagnosticSummary)) { $ownerStatusLabel.Text += ' · ' + [string]$record.diagnosticSummary }
    }
    if ($script:stale) { $ownerStatusLabel.Text += '（数据已过期，请刷新后再应用）' }
    $machine = Get-SelectedMachine
    if ($null -eq $machine) {
        $machineReasonLabel.Text = '从本机与已登记的 SSH 主机中选择目标。'
    } else {
        $machineReasonLabel.Text = [string]$machine.reason
        $sourceText = if ($machine.sourceLabel) { [string]$machine.sourceLabel } else { Get-SourceLabel ([string]$machine.source) }
        if (-not [string]::IsNullOrWhiteSpace($sourceText)) { $machineReasonLabel.Text += '  来源：' + $sourceText }
    }
    $confirmButton.Enabled = ($null -ne $record -and $null -ne $machine -and [bool]$machine.selectable -and -not $script:stale -and -not $script:workflowBusy -and $null -eq $script:request)
    $refreshButton.Enabled = ($null -eq $script:request -and -not $script:workflowBusy)
    $discoverButton.Enabled = $refreshButton.Enabled
    $writerBox.Enabled = -not $script:workflowBusy
    $taskGrid.Enabled = -not $script:workflowBusy
    $forceContinueBox.Enabled = (-not $script:workflowBusy -and $null -eq $script:request)
}
function Update-Diagnostics {
    $lines = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($script:lastError)) {
        $lines.Add('读取或交接失败：' + $script:lastError)
        $lines.Add('检查两端的 SSH 主机配置和连接，然后点击「刷新 SSH 主机」。')
        $lines.Add('')
    }
    if ($null -ne $script:snapshot) {
        foreach ($diagnostic in @($script:snapshot.diagnostics)) {
            if ($null -eq $diagnostic) { continue }
            $line = [string]$diagnostic.title
            if (-not [string]::IsNullOrWhiteSpace([string]$diagnostic.detail)) { $line += '：' + [string]$diagnostic.detail }
            if (-not [string]::IsNullOrWhiteSpace([string]$diagnostic.action)) { $line += $NL + '建议：' + [string]$diagnostic.action }
            $lines.Add($line)
        }
    }
    if ($lines.Count -eq 0) { $lines.Add('当前快照没有报告诊断问题。') }
    $diagnosticsBox.Text = $lines -join $NL
}
function Show-Snapshot {
    param($NewSnapshot)
    if ([int]$NewSnapshot.schemaVersion -ne 5 -or $null -eq $NewSnapshot.PSObject.Properties['records'] -or $null -eq $NewSnapshot.PSObject.Properties['machines']) { throw '服务返回的数据版本不匹配，请更新当前工具。' }
    $selected = Get-SelectedRecord
    $selectedId = if ($null -ne $selected) { [string]$selected.id } else { '' }
    $sortColumnIndex = -1
    $sortDirection = [System.ComponentModel.ListSortDirection]::Ascending
    if ($null -ne $taskGrid.SortedColumn) {
        $sortColumnIndex = $taskGrid.SortedColumn.Index
        if ($taskGrid.SortOrder -eq [System.Windows.Forms.SortOrder]::Descending) { $sortDirection = [System.ComponentModel.ListSortDirection]::Descending }
    }
    $script:refreshingControls = $true
    $taskGrid.SuspendLayout()
    $writerBox.BeginUpdate()
    try {
        $script:snapshot = $NewSnapshot
        $storageName = [string]$NewSnapshot.storageHost.name
        if ([string]::IsNullOrWhiteSpace($storageName)) { $storageName = [string](@($NewSnapshot.machines | Where-Object { $_.id -eq 'local' })[0].name) }
        $contextLabel.Text = '操作端：' + $script:CallerHostName + '（本机 Windows） · 会话数据：' + $storageName + '（Mac）'
        $script:stale = [bool]$NewSnapshot.stale
        $script:recordsById = @{}
        $taskGrid.Rows.Clear()
        foreach ($record in @($NewSnapshot.records)) {
            if ($null -eq $record) { continue }
            $script:recordsById[[string]$record.id] = $record
            $ownerName = [string]$record.writerHostName
            if ([string]::IsNullOrWhiteSpace($ownerName)) { $ownerName = '未确认' }
            $rowIndex = $taskGrid.Rows.Add([string]$record.name, [string]$record.id, [string]$record.createdTime, $ownerName)
            $taskGrid.Rows[$rowIndex].Tag = [string]$record.id
            $taskGrid.Rows[$rowIndex].Cells[1].ToolTipText = [string]$record.id
            $taskGrid.Rows[$rowIndex].Cells[3].ToolTipText = $ownerName
        }
        if ($sortColumnIndex -ge 0) { $taskGrid.Sort($taskGrid.Columns[$sortColumnIndex], $sortDirection) }
        $taskGrid.ClearSelection()
        $selectedRow = $null
        foreach ($row in $taskGrid.Rows) { if ([string]$row.Tag -eq $selectedId) { $selectedRow = $row; break } }
        if ($null -eq $selectedRow -and $taskGrid.Rows.Count -gt 0) { $selectedRow = $taskGrid.Rows[0] }
        if ($null -ne $selectedRow) {
            $taskGrid.CurrentCell = $selectedRow.Cells[0]
            $selectedRow.Selected = $true
        }
        $writerBox.Items.Clear()
        [void]$writerBox.Items.Add((New-MachineChoice '请选择目标写入主机' $null))
        $targetIndex = 0
        $defaultMachineId = if ([string]::IsNullOrWhiteSpace($script:preferredMachineId)) { [string]$NewSnapshot.caller.machineId } else { $script:preferredMachineId }
        foreach ($machine in @($NewSnapshot.machines)) {
            if ($null -eq $machine) { continue }
            $label = [string]$machine.name + '  ·  ' + [string]$machine.statusLabel
            if (-not [bool]$machine.selectable) { $label += '（不可应用）' }
            $itemIndex = $writerBox.Items.Add((New-MachineChoice $label $machine))
            if ([string]$machine.id -eq $defaultMachineId -and [bool]$machine.selectable) { $targetIndex = $itemIndex }
        }
        $writerBox.SelectedIndex = $targetIndex
        $script:lastError = ''
        $script:lastRenderedAt = Get-Date
        $script:refreshCount++
        $snapshotTime = [string]$NewSnapshot.generatedAt
        try { $snapshotTime = ([DateTimeOffset]::Parse($snapshotTime)).ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss zzz') } catch { }
        $refreshStatusLabel.Text = '最近刷新：' + $snapshotTime + '  ·  ' + $taskGrid.Rows.Count + ' 个会话，' + (@($NewSnapshot.machines).Count) + ' 台机器'
        if ($script:stale) { $refreshStatusLabel.Text += '（过期）' }
    } finally {
        $writerBox.EndUpdate()
        $taskGrid.ResumeLayout()
        $script:refreshingControls = $false
    }
    Update-Diagnostics
    Update-SelectionDetails
}
function Show-RequestFailure {
    param([string]$Message, [string]$Kind)
    if ($Kind -eq 'logs') {
        [System.Windows.Forms.MessageBox]::Show($form, '本机日志已打开，但远端日志暂未取回。请检查连接后重试。', '打开日志', 'OK', 'Information') | Out-Null
        Update-SelectionDetails
        return
    }
    $script:lastError = $Message
    $script:stale = $true
    $script:workflowBusy = $false
    $refreshStatusLabel.Text = '刷新失败，保留上次数据。' + $(if ($null -ne $script:lastRenderedAt) { ' 上次成功：' + $script:lastRenderedAt.ToString('HH:mm:ss') } else { ' 尚无成功快照。' })
    if ($Kind -eq 'claim') { $refreshStatusLabel.Text = '应用结果尚未确认，请刷新当前占用者。' }
    Update-Diagnostics
    Update-SelectionDetails
    if ($Kind -ne 'snapshot') { [System.Windows.Forms.MessageBox]::Show($form, $Message, '会话写入者', 'OK', 'Warning') | Out-Null }
}
function Request-Snapshot {
    param([switch]$Rediscover)
    if ($null -ne $script:request -or $script:workflowBusy) { $script:suppressedRequestCount++; return }
    $arguments = @('snapshot', '--format', 'json')
    if ($Rediscover) { $arguments += '--rediscover' }
    try {
        $script:request = Start-MacWriterRequest -ToolArguments $arguments -Kind 'snapshot'
        $script:snapshotRequestCount++
        $refreshStatusLabel.Text = $(if ($Rediscover) { '正在读取 SSH 主机并刷新…' } else { '正在刷新…' })
        if ($null -ne $script:lastRenderedAt) { $refreshStatusLabel.Text += '  上次成功：' + $script:lastRenderedAt.ToString('HH:mm:ss') }
        Update-SelectionDetails
    } catch { Show-RequestFailure -Message $_.Exception.Message -Kind 'snapshot' }
}
function Complete-WriterRequest {
    param([string]$Kind, $Value)
    if ($Kind -eq 'logs') {
        # Backend supplies a bounded, sanitized event tail; retain only this one copy.
        Save-BackendLogExport $Value
        Update-SelectionDetails
        return
    }
    if ($Kind -eq 'snapshot') { Show-Snapshot $Value; return }
    if ($Kind -eq 'plan') {
        if (-not [bool]$Value.canApply) {
            $script:workflowBusy = $false
            $reason = [string]$Value.reason
            if ([string]::IsNullOrWhiteSpace($reason)) { $reason = [string]$Value.message }
            [System.Windows.Forms.MessageBox]::Show($form, $reason, '当前无法应用', 'OK', 'Information') | Out-Null
            Request-Snapshot
            return
        }
        if ([string]::IsNullOrWhiteSpace([string]$Value.revision)) { throw '交接预览缺少状态版本，请刷新后重试。' }
        $choice = [System.Windows.Forms.MessageBox]::Show($form, [string]$Value.message, '确认应用', 'YesNo', 'Warning')
        if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) {
            $script:workflowBusy = $false
            Update-SelectionDetails
            return
        }
        $claimArguments = @('claim', '--thread-id', [string]$script:pendingSelection.ThreadId, '--machine-id', [string]$script:pendingSelection.MachineId, '--expected-revision', [string]$Value.revision, '--format', 'json')
        if ($script:pendingSelection.ForceContinue) { $claimArguments += '--force-continue' }
        $script:request = Start-MacWriterRequest -ToolArguments $claimArguments -Kind 'claim' -TimeoutSeconds 240
        $refreshStatusLabel.Text = '正在应用，自动刷新已暂停…'
        Update-SelectionDetails
        return
    }
    if ($Kind -eq 'claim') {
        $script:workflowBusy = $false
        $message = [string]$Value.message
        if ([string]::IsNullOrWhiteSpace($message)) { $message = '操作已返回，请以刷新后的占用者为准。' }
        $icon = if ([bool]$Value.ok -and $Value.continuationComplete -ne $false) { 'Information' } else { 'Warning' }
        [System.Windows.Forms.MessageBox]::Show($form, $message, '会话写入者', 'OK', $icon) | Out-Null
        $forceContinueBox.Checked = $false
        Request-Snapshot
    }
}

$taskGrid.Add_SelectionChanged({ Update-SelectionDetails })
$writerBox.Add_SelectedIndexChanged({
    if (-not $script:refreshingControls) {
        $selectedMachine = Get-SelectedMachine
        $script:preferredMachineId = if ($null -ne $selectedMachine) { [string]$selectedMachine.id } else { '' }
        Update-SelectionDetails
    }
})
$refreshButton.Add_Click({ Request-Snapshot })
$discoverButton.Add_Click({ Request-Snapshot -Rediscover })
$openLogsButton.Add_Click({
    try {
        [void][System.IO.Directory]::CreateDirectory($script:LogDirectory)
        Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $script:LogDirectory + '"')
    } catch {
        [System.Windows.Forms.MessageBox]::Show($form, ('无法打开日志文件夹，请手动打开：' + $NL + $script:LogDirectory), '打开日志', 'OK', 'Warning') | Out-Null
        return
    }
    try {
        if ($null -eq $script:request -and -not $script:workflowBusy) {
            $script:request = Start-MacWriterRequest -ToolArguments @('logs', '--format', 'json') -Kind 'logs'
            Update-SelectionDetails
        }
    } catch { Show-RequestFailure -Message $_.Exception.Message -Kind 'logs' }
})
$confirmButton.Add_Click({
    $record = Get-SelectedRecord
    $machine = Get-SelectedMachine
    if ($null -eq $record -or $null -eq $machine -or -not [bool]$machine.selectable -or $script:stale -or $null -ne $script:request -or $script:workflowBusy) { return }
    $script:pendingSelection = [pscustomobject]@{ ThreadId = [string]$record.id; MachineId = [string]$machine.id; ForceContinue = $forceContinueBox.Checked }
    $script:workflowBusy = $true
    try {
        $arguments = @('plan', '--thread-id', [string]$record.id, '--machine-id', [string]$machine.id, '--format', 'json')
        if ($script:pendingSelection.ForceContinue) { $arguments += '--force-continue' }
        $script:request = Start-MacWriterRequest -ToolArguments $arguments -Kind 'plan' -TimeoutSeconds 120
        $refreshStatusLabel.Text = '正在核对当前占用者和目标主机，自动刷新已暂停…'
        Update-SelectionDetails
    } catch { Show-RequestFailure -Message $_.Exception.Message -Kind 'plan' }
})
$pollTimer = New-Object System.Windows.Forms.Timer
$pollTimer.Interval = 100
$pollTimer.Add_Tick({
    if ($null -eq $script:request) { return }
    $currentRequest = $script:request
    try {
        $completed = Get-CompletedRequest $currentRequest
        if ($null -eq $completed) { return }
        $script:request = $null
        Complete-WriterRequest -Kind $currentRequest.Kind -Value $completed.Value
    } catch {
        $script:request = $null
        Show-RequestFailure -Message $_.Exception.Message -Kind $currentRequest.Kind
    }
})
$refreshTimer = New-Object System.Windows.Forms.Timer
$refreshTimer.Interval = 10000
$refreshTimer.Add_Tick({ if ($autoRefreshBox.Checked) { Request-Snapshot } })
$autoRefreshBox.Add_CheckedChanged({ if ($autoRefreshBox.Checked) { $refreshTimer.Start() } else { $refreshTimer.Stop() } })
$form.Add_Shown({ $pollTimer.Start(); if ($autoRefreshBox.Checked) { $refreshTimer.Start() }; Request-Snapshot })
$form.Add_FormClosing({
    param($sender, $eventArgs)
    if ($null -ne $script:request -and $script:request.Kind -eq 'claim') {
        $eventArgs.Cancel = $true
        $refreshStatusLabel.Text = '正在应用，请等待结果返回后关闭窗口。'
        return
    }
    $pollTimer.Stop()
    $refreshTimer.Stop()
    if ($null -ne $script:request) {
        Write-RequestLog $script:request 'request_cancelled' @{ failureCode = 'window_closed' }
        try { if (-not $script:request.Process.HasExited) { $script:request.Process.Kill() } } catch { }
        $script:request.Process.Dispose()
        $script:request = $null
    }
})
function Wait-UiRefresh {
    param([int]$PreviousCount)
    $waitUntil = [DateTime]::UtcNow.AddSeconds(78)
    do {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 40
    } while ($null -ne $script:request -and [DateTime]::UtcNow -lt $waitUntil)
    return ($script:refreshCount -gt $PreviousCount)
}

if ($UiSelfTest -or -not [string]::IsNullOrWhiteSpace($RenderPath)) {
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $firstRefreshOk = Wait-UiRefresh 0
    $selfTestDetails = $null
    if ($UiSelfTest) {
        $refreshTimer.Stop()
        $taskGrid.Sort($taskGrid.Columns[0], [System.ComponentModel.ListSortDirection]::Descending)
        if ($taskGrid.Rows.Count -gt 0) {
            $taskGrid.ClearSelection()
            $taskGrid.CurrentCell = $taskGrid.Rows[$taskGrid.Rows.Count - 1].Cells[0]
            $taskGrid.Rows[$taskGrid.Rows.Count - 1].Selected = $true
        }
        for ($index = 1; $index -lt $writerBox.Items.Count; $index++) {
            if ([bool]$writerBox.Items[$index].Machine.selectable) { $writerBox.SelectedIndex = $index; break }
        }
        $beforeThread = Get-SelectedRecord
        $beforeThreadId = if ($null -ne $beforeThread) { [string]$beforeThread.id } else { '' }
        $beforeTarget = $script:preferredMachineId
        $beforeCount = $script:refreshCount
        $beforeRequests = $script:snapshotRequestCount
        $refreshButton.PerformClick()
        Request-Snapshot
        $singleFlightOk = ($script:snapshotRequestCount -eq ($beforeRequests + 1) -and $script:suppressedRequestCount -gt 0)
        $secondRefreshOk = Wait-UiRefresh $beforeCount
        $savedSnapshot = $script:snapshot
        $savedRefreshCount = $script:refreshCount
        $savedRowIds = @($taskGrid.Rows | ForEach-Object { [string]$_.Tag }) -join '|'
        Show-RequestFailure -Message '自检：模拟读取失败，检查旧数据保留与应用保护。' -Kind 'snapshot'
        $staleFailureKeepsRows = ($savedRowIds -eq (@($taskGrid.Rows | ForEach-Object { [string]$_.Tag }) -join '|'))
        $staleFailureDisablesApply = ($script:stale -and -not $confirmButton.Enabled)
        $staleFailureHasReason = $diagnosticsBox.Text.Contains('模拟读取失败')
        if ($null -ne $savedSnapshot) { Show-Snapshot $savedSnapshot; $script:refreshCount = $savedRefreshCount }
        $afterThread = Get-SelectedRecord
        $afterThreadId = if ($null -ne $afterThread) { [string]$afterThread.id } else { '' }
        $rowIdsMatchCells = $true
        $taskText = @(foreach ($row in $taskGrid.Rows) {
            if ([string]$row.Tag -ne [string]$row.Cells[1].Value) { $rowIdsMatchCells = $false }
            [pscustomobject]@{ Name = [string]$row.Cells[0].Value; Id = [string]$row.Cells[1].Value; Created = [string]$row.Cells[2].Value; CurrentOwner = [string]$row.Cells[3].Value }
        })
        $machineChoices = @(foreach ($item in $writerBox.Items) {
            if ($null -ne $item.Machine) { [pscustomobject]@{ Id = [string]$item.Machine.id; Name = [string]$item.Machine.name; Status = [string]$item.Machine.status; Selectable = [bool]$item.Machine.selectable; Reason = [string]$item.Machine.reason } }
        })
        $actualMachineIds = @($machineChoices | ForEach-Object { [string]$_.Id } | Sort-Object)
        $snapshotMachineIds = @($script:snapshot.machines | ForEach-Object { [string]$_.id } | Sort-Object)
        $machineChoicesMatch = (($actualMachineIds -join '|') -eq ($snapshotMachineIds -join '|'))
        $selfTestDetails = [pscustomobject]@{
            Title = $form.Text
            TaskRows = $taskGrid.Rows.Count
            Columns = @($taskGrid.Columns | ForEach-Object { $_.HeaderText })
            TaskText = $taskText
            HostChoices = $machineChoices
            SelectedMachineId = $script:preferredMachineId
            CurrentOwner = $currentOwnerLabel.Text
            OwnerBelowTableVisible = $currentOwnerLabel.Visible
            CurrentStatus = $ownerStatusLabel.Text
            TargetDisplayText = $writerBox.Text
            TargetDisplayMatchesSelection = ($writerBox.Text -eq [string]$writerBox.SelectedItem.Label)
            MachineSourceText = $machineReasonLabel.Text
            WindowFitsWorkingArea = ($form.Width -le $workingArea.Width -and $form.Height -le $workingArea.Height)
            WindowSize = @($form.Width, $form.Height)
            WorkingAreaSize = @($workingArea.Width, $workingArea.Height)
            ApplyButton = $confirmButton.Text
            OpenLogsButton = $openLogsButton.Text
            LogDirectory = $script:LogDirectory
            FirstRefreshOk = $firstRefreshOk
            SecondRefreshOk = $secondRefreshOk
            SelectionPreserved = ($beforeThreadId -eq $afterThreadId)
            TargetPreserved = ($beforeTarget -eq $script:preferredMachineId)
            RowIdsMatchCellsAfterSort = $rowIdsMatchCells
            MachineChoicesMatchSnapshot = $machineChoicesMatch
            SimulatedReadFailureKeepsRows = $staleFailureKeepsRows
            SimulatedReadFailureDisablesApply = $staleFailureDisablesApply
            SimulatedReadFailureHasReason = $staleFailureHasReason
            SingleFlight = $singleFlightOk
            RefreshCount = $script:refreshCount
            AutoRefreshSeconds = $refreshTimer.Interval / 1000
            Stale = $script:stale
            Diagnostics = $diagnosticsBox.Text
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($RenderPath)) {
        [System.Windows.Forms.Application]::DoEvents()
        $bitmap = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
        try {
            $rectangle = New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)
            $form.DrawToBitmap($bitmap, $rectangle)
            $bitmap.Save($RenderPath, [System.Drawing.Imaging.ImageFormat]::Png)
        } finally { $bitmap.Dispose() }
    }
    $form.Close()
    $pollTimer.Dispose()
    $refreshTimer.Dispose()
    $form.Dispose()
    if ($UiSelfTest) {
        $selfTestDetails | ConvertTo-Json -Depth 8 -Compress
        if (-not $selfTestDetails.FirstRefreshOk -or -not $selfTestDetails.SecondRefreshOk -or -not $selfTestDetails.RowIdsMatchCellsAfterSort -or -not $selfTestDetails.MachineChoicesMatchSnapshot -or -not $selfTestDetails.SingleFlight -or -not $selfTestDetails.SimulatedReadFailureKeepsRows -or -not $selfTestDetails.SimulatedReadFailureDisablesApply -or -not $selfTestDetails.SimulatedReadFailureHasReason -or -not $selfTestDetails.TargetDisplayMatchesSelection -or -not $selfTestDetails.WindowFitsWorkingArea) { exit 1 }
    }
    exit 0
}
[void]$form.ShowDialog()
$pollTimer.Dispose()
$refreshTimer.Dispose()
$form.Dispose()
