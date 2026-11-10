@echo off
title Nexora V2 - Auto Launcher
cd /d "%~dp0"

echo ============================================
echo   Nexora V2 - Starting All Services...
echo ============================================
echo.

:: Start Backend in a new window
echo [1/2] Starting Backend (Python FastAPI)...
start "Nexora Backend" powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0start_backend.ps1"

:: Wait 4 seconds for backend to initialize
echo Waiting for backend to initialize...
timeout /t 4 /nobreak >nul

:: Start Frontend in a new window
echo [2/2] Starting Frontend (Vite + Electron)...
start "Nexora Frontend" powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0start_frontend.ps1"

echo.
echo ============================================
echo   Both services are starting!
echo   Backend  -> http://127.0.0.1:8000
echo   Frontend -> http://localhost:5173
echo ============================================
echo.
echo Close this window anytime - services run in their own windows.
pause
