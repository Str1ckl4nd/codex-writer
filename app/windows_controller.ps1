param([ValidateSet('identity','close-desktop')][string]$Operation='identity',
      [int]$ExpectedPid=0, [string]$ExpectedStart='')
$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)

function Get-WriterIdentity {
    param([object[]]$Processes,[string]$MacAlias='mac')
    $byId=@{}
    foreach($p in $Processes){$byId[[int]$p.ProcessId]=$p}
    # Codex CLI also has codex.exe; a GUI client must belong to the desktop package.
    $apps=@($Processes | Where-Object {
        $_.Name -in @('ChatGPT.exe','Codex.exe') -and
        $_.CommandLine -and $_.CommandLine -notmatch '--type=|\bapp-server\b' -and
        $_.ExecutablePath -match '\\(?:WindowsApps\\OpenAI\.[^\\]+\\app|OpenAI\\(?:Codex|ChatGPT)\\app)\\(?:ChatGPT|Codex)\.exe$'
    })
    $roots=@{}
    foreach($p in $apps){$roots[[int]$p.ProcessId]=$p}
    $proxies=@($Processes | Where-Object {
        $_.Name -ieq 'ssh.exe' -and $_.CommandLine -match '\bapp-server\s+proxy\b' -and
        $_.CommandLine -match ('(?:\s|["\x27])'+[regex]::Escape($MacAlias)+'(?:\s|["\x27])')
    })
    $controllers=@{}
    $matchedProxies=@()
    foreach($proxy in $proxies){
        $parent=[int]$proxy.ParentProcessId
        $seen=@{}
        for($n=0;$n -lt 12 -and $parent -gt 0;$n++){
            if($seen.ContainsKey($parent)){break};$seen[$parent]=$true
            if($roots.ContainsKey($parent)){
                $app=$roots[$parent]
                if(-not $controllers.ContainsKey($parent)){
                    $controllers[$parent]=[pscustomobject]@{pid=$parent;start=$app.CreationDate.ToUniversalTime().ToString('o');name=$app.Name;proxyPids=@()}
                }
                $controllers[$parent].proxyPids+=@([int]$proxy.ProcessId)
                $matchedProxies+=@([int]$proxy.ProcessId)
                break
            }
            if(-not $byId.ContainsKey($parent)){break}
            $parent=[int]$byId[$parent].ParentProcessId
        }
    }
    foreach($controller in $controllers.Values){$controller.proxyPids=@($controller.proxyPids|Sort-Object)}
    [pscustomobject]@{
        schemaVersion=2;hostName=[Net.Dns]::GetHostName()
        apps=@($apps|ForEach-Object{[pscustomobject]@{pid=[int]$_.ProcessId;start=$_.CreationDate.ToUniversalTime().ToString('o');name=$_.Name}})
        controllers=@($controllers.Values|Sort-Object pid)
        proxyCount=$proxies.Count;matchedProxyCount=$matchedProxies.Count
        detection=if($controllers.Count -gt 1){'multiple_controllers'}elseif($controllers.Count -eq 1 -and $matchedProxies.Count -eq $proxies.Count){'confirmed'}elseif($controllers.Count -eq 1){'unmatched_proxies'}elseif($apps.Count -eq 0){'desktop_absent'}else{'proxy_not_connected'}
    }
}

function Invoke-WriterIdentityOperation {
    param([Parameter(Mandatory=$true)][ValidateSet('identity','close-desktop')][string]$Action,[int]$PidToStop,[string]$StartToMatch,[string]$MacAlias='mac',[string]$HostNameToMatch='')
    $processes=@(Get-CimInstance Win32_Process)
    $identity=Get-WriterIdentity -Processes $processes -MacAlias $MacAlias
    if($Action -eq 'identity'){$identity|ConvertTo-Json -Depth 6 -Compress;return}
    if($HostNameToMatch -and $identity.hostName -cne $HostNameToMatch){throw 'CONTROLLER_CHANGED'}
    $owners=@($identity.controllers)
    if($identity.detection -ne 'confirmed' -or $owners.Count -ne 1 -or $owners[0].pid -ne $PidToStop -or $owners[0].start -cne $StartToMatch){throw 'CONTROLLER_CHANGED'}
    Stop-Process -Id $PidToStop -Force
    [pscustomobject]@{stopped=$PidToStop}|ConvertTo-Json -Compress
}

# The SSH wrapper calls the operation explicitly after loading these functions.
