$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
foreach ($file in @(Get-ChildItem (Join-Path $root 'app') -Filter '*.ps1')+@(Get-ChildItem $PSScriptRoot -Filter '*.ps1')) {
    $parseTokens=$null
    $parseErrors=$null
    $null=[Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$parseTokens,[ref]$parseErrors)
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
}
& (Join-Path $root 'tests/test_windows_controller.ps1') -ControllerPath (Join-Path $root 'app/windows_controller.ps1')
& (Join-Path $root 'tests/test_windows_package.ps1') -ProjectRoot $root
Write-Output 'PowerShell parse and pure controller fixtures: PASS'
