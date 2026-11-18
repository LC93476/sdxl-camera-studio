# SDXL 相机工作台 · 创建桌面快捷方式
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs  = Join-Path $here "launch.vbs"
$icon = Join-Path $here "icon.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "SDXL 相机工作台.lnk"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = "wscript.exe"
$s.Arguments = '"' + $vbs + '"'
$s.WorkingDirectory = $here
$s.Description = "SDXL 相机工作台 - 3D 拖摄像机直接出图"
if (Test-Path $icon) { $s.IconLocation = $icon }
$s.WindowStyle = 7
$s.Save()
Write-Output ("SHORTCUT " + $lnk)
