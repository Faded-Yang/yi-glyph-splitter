# Build the Windows app (folder build) + installer.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -NoInstaller
param(
    [switch]$NoInstaller
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

# a running copy locks dist\* -> kill first
Get-Process YiGlyphSplit -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

Write-Host "[1/4] icon ..."
python packaging\make_icon.py

Write-Host "[2/4] pyinstaller (onedir) ..."
python -m PyInstaller --noconfirm --clean packaging\app.spec

$folder = Join-Path $PWD 'dist\YiGlyphSplit'
if (-not (Test-Path $folder)) { throw "build failed: $folder missing" }
$exeMB = [math]::Round((Get-Item (Join-Path $folder 'YiGlyphSplit.exe')).Length / 1MB, 2)
$allMB = [math]::Round((Get-ChildItem $folder -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host ("       launcher {0} MB, folder {1} MB" -f $exeMB, $allMB)

if ($NoInstaller) {
    Write-Host "[3/4] installer skipped (-NoInstaller)"
    return
}

Write-Host "[3/4] Inno Setup ..."
$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw 'ISCC.exe not found: install Inno Setup 6 (winget install JRSoftware.InnoSetup)'
}
& $iscc 'packaging\installer.iss'
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

Write-Host "[4/4] done"
Get-ChildItem dist -File | Select-Object Name, @{n='MB'; e={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize
Write-Host "Portable:  dist\YiGlyphSplit\YiGlyphSplit.exe"
Write-Host "Installer: dist\YiGlyphSplit-*-Setup.exe"
