@echo off
REM Mousstec field CLI - one-command setup (Windows).
REM   Run from the repo root:  bmw_ecu\scripts\setup_field.bat
REM Creates .venv, installs the 3 transport deps, writes field.env and a
REM field.bat wrapper. No database, no web server.
setlocal enabledelayedexpansion

cd /d "%~dp0\..\.."
set "REPO_ROOT=%CD%"

where python >nul 2>nul
if errorlevel 1 (
  echo [X] No Python found. Install Python 3.10+ and re-run.
  exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo ^> %%v

if not exist ".venv" (
  echo ^> creating .venv ...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

if not "%SKIP_INSTALL%"=="1" (
  echo ^> installing python-can can-isotp pyserial ...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet python-can can-isotp pyserial
) else (
  echo ^> skipping install ^(SKIP_INSTALL=1^)
)

if not exist "field.env.bat" (
  echo ^> writing field.env.bat ^(edit it with your values^) ...
  (
    echo @echo off
    echo REM Edit to match your CANable + car. Find the port in Device Manager ^(COMx^).
    echo set BMW_ECU_KDCAN_PORT=COM4
    echo set BMW_ECU_CAN_TX_ID=0x6F1
    echo set BMW_ECU_CAN_RX_ID=0x612
    echo set BMW_ECU_CAN_BITRATE=500000
  ) > "field.env.bat"
) else (
  echo ^> field.env.bat already exists - left unchanged.
)

(
  echo @echo off
  echo cd /d "%%~dp0"
  echo call ".venv\Scripts\activate.bat"
  echo if exist "field.env.bat" call "field.env.bat"
  echo python -m bmw_ecu.scripts.field_cli %%*
) > "field.bat"

echo.
echo [OK] Done. Next:
echo    1^) edit field.env.bat ^(COM port + CAN IDs^)
echo    2^) field ping
echo    3^) field read-fa
echo    4^) field diagnose --engine N18 --bench
endlocal
