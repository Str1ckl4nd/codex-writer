# Pure fixtures: loading the implementation defines functions; no live process
# enumeration, SSH connection, or process shutdown is permitted in this script.
param([string]$ControllerPath=(Join-Path $PSScriptRoot 'windows_controller.ps1'))
$ErrorActionPreference='Stop'
. $ControllerPath

function Get-CimInstance { throw 'Fixture attempted live process enumeration' }
function Stop-Process { throw 'Fixture attempted process shutdown' }

function New-ProcessFixture {
    param([int]$ProcessNumber, [int]$ParentNumber, [string]$ProcessName,
          [string]$ProcessPath, [string]$ProcessCommand)
    [pscustomobject]@{
        ProcessId=$ProcessNumber
        ParentProcessId=$ParentNumber
        Name=$ProcessName
        ExecutablePath=$ProcessPath
        CommandLine=$ProcessCommand
        CreationDate=([datetime]'2026-09-10T01:00:00Z').ToUniversalTime().AddSeconds($ProcessNumber)
    }
}

function New-DesktopFixture {
    param([int]$ProcessNumber, [string]$AppName='ChatGPT.exe')
    $packagePath='C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1.2026.250.0_x64__2p2nqsd0c76g0\app\'+$AppName
    New-ProcessFixture $ProcessNumber 1 $AppName $packagePath ('"'+$packagePath+'"')
}

function New-ProxyFixture {
    param([int]$ProcessNumber, [int]$ParentNumber, [string]$HostAlias='mac')
    New-ProcessFixture $ProcessNumber $ParentNumber 'ssh.exe' 'C:\Windows\System32\OpenSSH\ssh.exe' `
        ('ssh.exe -T '+$HostAlias+' /Applications/ChatGPT.app/Contents/Resources/codex app-server proxy')
}

function Assert-Equal {
    param($Actual,$Expected,[string]$Context)
    if($Actual -cne $Expected){throw "$Context expected <$Expected>, received <$Actual>"}
}

function Assert-Controller {
    param($Identity,[int]$ExpectedProcess,[int]$ExpectedProxy,[string]$Context)
    Assert-Equal $Identity.detection 'confirmed' "$Context detection"
    Assert-Equal @($Identity.controllers).Count 1 "$Context controller count"
    Assert-Equal $Identity.controllers[0].pid $ExpectedProcess "$Context selected desktop"
    Assert-Equal @($Identity.controllers[0].proxyPids).Count 1 "$Context proxy count"
    Assert-Equal $Identity.controllers[0].proxyPids[0] $ExpectedProxy "$Context selected proxy"
}

$cases=0

# An unrelated second desktop must not block the one that owns the SSH child.
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100), (New-DesktopFixture 200), (New-ProxyFixture 300 100)
)
Assert-Equal @($identity.apps).Count 2 'unrelated desktop inventory'
Assert-Controller $identity 100 300 'unrelated desktop'
$cases++

# A genuine second controlling desktop remains ambiguous.
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100), (New-DesktopFixture 200),
    (New-ProxyFixture 300 100), (New-ProxyFixture 400 200)
)
Assert-Equal $identity.detection 'multiple_controllers' 'two controlling desktops'
Assert-Equal @($identity.controllers).Count 2 'two controllers remain visible'
$cases++

# A standalone CLI executable with the same basename is not a desktop client.
$identity=Get-WriterIdentity -Processes @(
    (New-ProcessFixture 100 1 'codex.exe' 'C:\Users\demo\.codex\bin\codex.exe' 'codex.exe app-server --listen unix://control'),
    (New-ProxyFixture 300 100)
)
Assert-Equal @($identity.apps).Count 0 'CLI desktop exclusion'
Assert-Equal @($identity.controllers).Count 0 'CLI controller exclusion'
Assert-Equal $identity.detection 'desktop_absent' 'CLI is not desktop'
$cases++

# The newer Codex GUI executable is recognized inside its installed package.
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100 'Codex.exe'), (New-ProxyFixture 300 100)
)
Assert-Controller $identity 100 300 'Codex WindowsApps GUI'
Assert-Equal $identity.controllers[0].name 'Codex.exe' 'Codex executable identity'
$cases++

# Electron helpers are not extra roots, but may appear in the ancestor chain.
$helper=New-DesktopFixture 200
$helper.ParentProcessId=100
$helper.CommandLine+=' --type=utility --utility-sub-type=node.mojom.NodeService'
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100), $helper, (New-ProxyFixture 300 200)
)
Assert-Equal @($identity.apps).Count 1 'Electron helper root exclusion'
Assert-Controller $identity 100 300 'Electron helper ancestry'
$cases++

# Several intermediate processes are followed to the owning GUI root.
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100),
    (New-ProcessFixture 200 100 'node.exe' 'C:\Tools\node.exe' 'node.exe worker.js'),
    (New-ProcessFixture 210 200 'cmd.exe' 'C:\Windows\System32\cmd.exe' 'cmd.exe /c ssh.exe'),
    (New-ProxyFixture 300 210)
)
Assert-Controller $identity 100 300 'nested parent ancestry'
$cases++

# An available GUI during reconnect is diagnosed as missing its SSH proxy.
$identity=Get-WriterIdentity -Processes @((New-DesktopFixture 100))
Assert-Equal @($identity.apps).Count 1 'reconnecting desktop exists'
Assert-Equal $identity.proxyCount 0 'reconnecting proxy absent'
Assert-Equal @($identity.controllers).Count 0 'reconnecting controller unconfirmed'
Assert-Equal $identity.detection 'proxy_not_connected' 'specific reconnect diagnosis'
$cases++

# Another SSH target cannot establish ownership of the configured Mac.
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100), (New-ProxyFixture 300 100 'other-machine')
)
Assert-Equal $identity.proxyCount 0 'other-host proxy excluded'
Assert-Equal $identity.detection 'proxy_not_connected' 'other-host controller excluded'
$cases++

# A broken parent chain terminates without guessing a GUI owner.
$identity=Get-WriterIdentity -Processes @(
    (New-DesktopFixture 100),
    (New-ProcessFixture 200 210 'node.exe' 'C:\Tools\node.exe' 'node.exe worker.js'),
    (New-ProcessFixture 210 200 'cmd.exe' 'C:\Windows\System32\cmd.exe' 'cmd.exe /c ssh.exe'),
    (New-ProxyFixture 300 210)
)
Assert-Equal @($identity.controllers).Count 0 'cyclic ancestry controller count'
Assert-Equal $identity.matchedProxyCount 0 'cyclic ancestry unmatched proxy'
$cases++

[pscustomobject]@{ok=$true;cases=$cases;liveProcessCalls=0}|ConvertTo-Json -Compress
