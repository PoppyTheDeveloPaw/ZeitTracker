@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem In GitHub Actions (CI=true) wird "python" aus setup-python verwendet, lokal der Python-Launcher "py".
if defined CI (set "PY=python") else (set "PY=py")
%PY% -m pip install --upgrade pyinstaller -r requirements.txt || exit /b 1
%PY% -m PyInstaller --noconfirm --onefile --windowed --name ZeitTracker ^
  --hidden-import babel.numbers --collect-all holidays --collect-all tkcalendar ^
  --hidden-import keyring.backends.Windows --hidden-import pystray._win32 --collect-all msal ^
  zeittracker.pyw || exit /b 1
echo.
echo Die EXE liegt in dist\ZeitTracker.exe
if not defined CI pause
