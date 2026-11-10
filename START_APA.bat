@echo off
title Nexora V2 launcher
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
echo Starting Nexora V2... (full log: apa_launch.log)
echo [%date% %time%] START > apa_launch.log
rem Prefer Python 3.12 (pyaudio has no build for newer versions); Electron reads APA_PYTHON.
if not defined APA_PYTHON for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "APA_PYTHON=%%p"
if not defined APA_PYTHON set "APA_PYTHON=python"
echo Using python: %APA_PYTHON% >> apa_launch.log
"%APA_PYTHON%" --version >> apa_launch.log 2>&1
where npm >> apa_launch.log 2>&1
echo Installing Python packages...
"%APA_PYTHON%" -m pip install -q -r requirements.txt >> apa_launch.log 2>&1
echo [pip exit %errorlevel%] >> apa_launch.log
if not exist node_modules (echo Installing Node packages... & call npm install >> apa_launch.log 2>&1)
echo Launching...
call npm run dev >> apa_launch.log 2>&1
echo [%date% %time%] EXIT %errorlevel% >> apa_launch.log
