@echo off
chcp 65001 >nul
echo Installiere benötigte Pakete für den ZeitTracker ...
py -m pip install --upgrade -r "%~dp0requirements.txt" || python -m pip install --upgrade -r "%~dp0requirements.txt"
echo.
echo Fertig. Starte den ZeitTracker mit Doppelklick auf zeittracker.pyw
pause
