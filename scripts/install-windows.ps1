$ErrorActionPreference='Stop'
$projectDir=Split-Path -Parent $PSScriptRoot
$installDir=Join-Path $env:LOCALAPPDATA 'Programs\SessionWriter'
if (Test-Path -LiteralPath $installDir) { throw 'Existing installation found. Move it to a backup before upgrading.' }
$configDir=Join-Path $env:USERPROFILE '.config\session-writer'
[void][IO.Directory]::CreateDirectory($installDir)
[void][IO.Directory]::CreateDirectory($configDir)
Copy-Item -LiteralPath (Join-Path $projectDir 'app\unlock.ps1') -Destination $installDir
Copy-Item -LiteralPath (Join-Path $projectDir 'app\windows_controller.ps1') -Destination $installDir
$config=Join-Path $configDir 'config.json'
if (-not (Test-Path -LiteralPath $config)) { Copy-Item -LiteralPath (Join-Path $projectDir 'config.example.json') -Destination $config }
$shortcutPath=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Session Writer.lnk'
if (Test-Path -LiteralPath $shortcutPath) { throw 'Shortcut already exists; leaving it unchanged.' }
$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath=(Join-Path $PSHOME 'powershell.exe')
if (-not (Test-Path -LiteralPath $shortcut.TargetPath)) { $shortcut.TargetPath=(Get-Process -Id $PID).Path }
$shortcut.Arguments='-NoProfile -File "'+(Join-Path $installDir 'unlock.ps1')+'"'
$shortcut.WorkingDirectory=$installDir
$shortcut.Save()
Write-Output ('Installed. Configure '+$config+'. No SSH/security policies were modified.')
