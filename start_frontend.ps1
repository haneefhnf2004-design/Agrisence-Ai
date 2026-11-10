$host.UI.RawUI.WindowTitle = "Nexora Frontend"
Set-Location "$PSScriptRoot"

Write-Host "[Nexora] Installing Node packages if needed..." -ForegroundColor Cyan
if (-not (Test-Path "node_modules")) { npm install }

Write-Host "[Nexora] Starting Vite + Electron..." -ForegroundColor Green
npm run dev
