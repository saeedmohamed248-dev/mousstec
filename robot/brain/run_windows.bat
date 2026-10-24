@echo off
rem «مخ موس» على ويندوز: أول مرة بيجهّز كل حاجة (دقايق)، وبعد كده بيشتغل على طول.
rem   run_windows.bat              الروبوت
rem   run_windows.bat devices      المايكات والسماعات اللي ويندوز شايفها
rem   run_windows.bat echo         اسمع المايك بيسمع إيه
rem   run_windows.bat autostart    الروبوت يشتغل لوحده أول ما ويندوز يفتح
rem الملف ده يا جوه فولدر brain نفسه، يا جنبه (في الـ zip) — زي run_mac.sh.
rem لازم Python 64-bit: موديل الكلام (faster-whisper) مالوش نسخة 32-bit.
rem 🐛 [FIX]: كان بياخد أول Python يلاقيه، ولو 32-bit (نسخة قديمة على الجهاز) الـ pip بيقع من غير ما حد
rem يفهم ليه. دلوقتي بيجرّب القايمة (:pick) وياخد أول 64-bit، والـ venv القديم لو 32-bit بيتعمل من جديد.
rem pymanager الأول: لو فيه Python قديمة على الجهاز، `py` بيبقى الـ launcher القديم بتاعها ومبيعرفش
rem أمر install. و«pymanager exec» بينزّل 3.12-64 لوحده لو مش موجودة.
rem العربي كله فوق الـ chcp: نسخ cmd القديمة بتتلخبط في سطور UTF-8 اللي بعده.
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

if exist "%PY%" (
  call :is64 "%PY%" && goto ready
  echo [SETUP] %VENV% is 32-bit or broken - making it again with 64-bit Python...
  rmdir /s /q "%VENV%"
  if exist "%VENV%" (
    echo [SETUP] could not delete %VENV% - close the robot window and run this file again.
    pause
    exit /b 1
  )
)
echo [SETUP] looking for 64-bit Python - downloads it if it is missing, a few minutes...
set "BASE="
set "SEEN32="
for %%C in ("pymanager exec -V:3.12-64" "py -V:3.12-64" "py -3.12-64" "py -V:3.12" "py -3-64" "py -3" "python") do (
  if not defined BASE call :pick %%~C
)
if not defined BASE (
  if defined SEEN32 (
    echo [SETUP] The Python on this computer is 32-bit - the speech model needs 64-bit Python.
    echo         Open Command Prompt and run:   pymanager install 3.12-64
    echo         then run this file again.
  ) else (
    echo Python is not installed. Get the Python install manager from https://www.python.org/downloads/
    echo then open Command Prompt and run:   pymanager install 3.12-64
    echo and run this file again.
  )
  pause
  exit /b 1
)
echo [SETUP] first run: setting everything up in %VENV% - a few minutes...
%BASE% -V
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
    echo         If Python is newer than 3.12, run in Command Prompt:   pymanager install 3.12-64
    pause
    exit /b 1
  )
  copy /y "%REQ%" "%VENV%\requirements.txt" >nul
)
rem ctranslate2.dll (the speech model) needs the C++ runtime; without it the server listens instead
if not exist "%SystemRoot%\System32\msvcp140.dll" (
  echo [SETUP] The Microsoft Visual C++ runtime is missing - the speech model on this computer needs it.
  echo         Install https://aka.ms/vs/17/release/vc_redist.x64.exe then run this file again.
  echo         Until then the server listens instead.
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

:pick
rem first 64-bit Python in the list; SEEN32 when only 32-bit ones run
call :is64 %*
if errorlevel 1 (
  %* -V >nul 2>nul && set "SEEN32=1"
  exit /b 0
)
set "BASE=%*"
exit /b 0

:is64
%* -c "import struct, sys; sys.exit(8 - struct.calcsize('P'))" >nul 2>nul
exit /b %errorlevel%
