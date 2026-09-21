param([string]$ProjectRoot=(Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath (Join-Path $ProjectRoot 'app/unlock.ps1') -Raw -Encoding UTF8
$tokens=$null
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
$function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-RemoteBackendPackage'},$true)
if ($null -eq $function) { throw 'Package function missing' }
. ([scriptblock]::Create($function.Extent.Text))
$WriterConfig=[pscustomobject]@{
    enable_handoff=$false
    mac_ssh_alias='fixture-host'
    password='DO_NOT_FORWARD'
    ssh_controllers=@([pscustomobject]@{alias='fixture-pc';platform='windows';backend_alias='fixture-host'})
}
$data=Get-RemoteBackendPackage -Arguments @('snapshot','--format','json') -ApplicationRoot (Join-Path $ProjectRoot 'app') | ConvertFrom-Json
if (@($data.files.PSObject.Properties).Count -ne 13) { throw 'Wrong program manifest' }
if (($data.argv -join ',') -ne 'snapshot,--format,json') { throw 'Nested or changed arguments' }
if ($null -ne $data.config.PSObject.Properties['password']) { throw 'Unexpected credential field' }
if ($data.config.ssh_controllers[0].alias -ne 'fixture-pc') { throw 'Controller configuration lost' }
Write-Output 'PowerShell standalone package: PASS (4 checks; no SSH or GUI calls)'
