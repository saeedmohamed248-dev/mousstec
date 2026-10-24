@echo off
rem «مخ موس» على ويندوز: أول مرة بيجهّز كل حاجة (دقايق)، وبعد كده بيشتغل على طول.
rem   run_windows.bat              الروبوت
rem   run_windows.bat devices      المايكات والسماعات اللي ويندوز شايفها
rem   run_windows.bat echo         اسمع المايك بيسمع إيه
rem   run_windows.bat autostart    الروبوت يشتغل لوحده أول ما ويندوز يفتح
rem الملف ده يا جوه فولدر brain نفسه، يا جنبه (في الـ zip) — زي run_mac.sh.
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
if exist "%HERE%__init__.py" (
  for %%I in ("%HERE%..") do set "ROOT=%%~fI"
) else (
  set "ROOT=%HERE:~0,-1%"
)
set "PKG=brain"
if not exist "%ROOT%\%PKG%\__init__.py" (
  echo [SETUP] brain folder not found next to this file: %ROOT%\%PKG%
  pause
  exit /b 1
)
if "%MOUSS_HOME%"=="" (set "STATE=%USERPROFILE%\.mouss-brain") else (set "STATE=%MOUSS_HOME%")
set "VENV=%STATE%\venv"
set "PY=%VENV%\Scripts\python.exe"

if /i "%~1"=="autostart" goto autostart

if exist "%PY%" goto ready
set "BASE="
where py >nul 2>nul && set "BASE=py -3.12"
if defined BASE (
  %BASE% -V >nul 2>nul || set "BASE=py -3"
)
if not defined BASE (
  where python >nul 2>nul && set "BASE=python"
)
if not defined BASE (
  echo Python is not installed. Get Python 3.12 from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this file again.
  pause
  exit /b 1
)
echo [SETUP] first run: setting everything up in %VENV% - a few minutes...
if not exist "%STATE%" mkdir "%STATE%"
%BASE% -m venv "%VENV%"
if errorlevel 1 (
  echo [SETUP] could not create %VENV%
  pause
  exit /b 1
)
"%PY%" -m pip install -q --upgrade pip

:ready
set "REQ=%ROOT%\%PKG%\requirements.txt"
fc /b "%REQ%" "%VENV%\requirements.txt" >nul 2>nul
if errorlevel 1 (
  "%PY%" -m pip install -q -r "%REQ%"
  if errorlevel 1 (
    echo [SETUP] installing the requirements failed. Check the internet and run this file again.
    echo         If Python is newer than 3.12, install Python 3.12 too - the speech model needs it.
    pause
    exit /b 1
  )
  copy /y "%REQ%" "%VENV%\requirements.txt" >nul
)
cd /d "%ROOT%"
if "%~1"=="" (
  "%PY%" -m %PKG% run
) else (
  "%PY%" -m %PKG% %*
)
exit /b %errorlevel%

:autostart
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
> "%STARTUP%\mouss-brain.bat" (
  echo @echo off
  echo call "%~f0"
)
echo [SETUP] the robot now starts by itself when Windows starts:
echo         %STARTUP%\mouss-brain.bat
echo         To stop that, delete that file.
exit /b 0
