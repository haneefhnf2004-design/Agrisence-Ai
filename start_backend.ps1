$host.UI.RawUI.WindowTitle = "Nexora Backend"
Set-Location "$PSScriptRoot"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[Nexora] Starting Python backend..." -ForegroundColor Cyan
python -m pip install -q -r requirements.txt
python backend/server.py
