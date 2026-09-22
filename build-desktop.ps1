param([string]$Output = "$PSScriptRoot\dist\PhoneService\AgentCall.exe")
$ErrorActionPreference = 'Stop'
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
$wpf = Join-Path $framework 'WPF'
& $compiler /nologo /target:winexe "/out:$Output" "/win32icon:$PSScriptRoot\dist\PhoneService\phone.ico" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "/reference:$wpf\PresentationCore.dll" "/reference:$wpf\PresentationFramework.dll" "/reference:$wpf\WindowsBase.dll" "/reference:$framework\System.Xaml.dll" "/resource:$PSScriptRoot\Desktop.xaml,CodexPhone.UI.xaml" "$PSScriptRoot\DesktopWpf.cs"
if ($LASTEXITCODE -ne 0) { throw 'Desktop compilation failed' }
