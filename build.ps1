param([string]$Python = "$PSScriptRoot\.build-venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot '.build-cache'
$env:TEMP = Join-Path $PSScriptRoot '.build-tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
& $Python -B -m unittest test_app -q
if ($LASTEXITCODE -ne 0) { throw 'Application tests failed' }
& $Python -B selftest.py
if ($LASTEXITCODE -ne 0) { throw 'SIP selftest failed' }
& $Python -m PyInstaller --noconfirm --clean --onedir --name PhoneService --hidden-import yealink_web --collect-all edge_tts --collect-all soundfile app_service.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
$outDir = Join-Path $PSScriptRoot 'dist\PhoneService'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap 64,64
$graphics = [System.Drawing.Graphics]::FromImage($bmp)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(32,103,212))
$pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White),8
$pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
$pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
$graphics.DrawArc($pen,17,10,32,40,55,200)
$graphics.DrawLine($pen,15,21,24,15)
$graphics.DrawLine($pen,22,47,31,43)
$icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
$stream = [System.IO.File]::Create((Join-Path $outDir 'phone.ico'))
$icon.Save($stream); $stream.Dispose(); $graphics.Dispose(); $bmp.Dispose(); $pen.Dispose()
& "$PSScriptRoot\build-desktop.ps1"
foreach ($name in @('README.md','AI_SETUP.md','COMPATIBILITY.md','RELEASE.md','LICENSE','TEST_REPORT.md','setup-firewall.ps1')) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name)) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $outDir }
}
& $Python package_release.py
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed' }
Write-Output "Build ready: $outDir\AgentCall.exe"
