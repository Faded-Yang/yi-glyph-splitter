# Build the Windows exe with PyInstaller.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "[1/3] icon ..."
python packaging\make_icon.py

Write-Host "[2/3] pyinstaller ..."
python -m PyInstaller --noconfirm --clean packaging\app.spec

Write-Host "[3/3] done"
Get-ChildItem dist | Select-Object Name, @{n='MB'; e={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize
Write-Host "Run: dist\YiGlyphSplit.exe"
