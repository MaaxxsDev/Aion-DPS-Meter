@echo off
cd /d "%~dp0"
python -c "import customtkinter, PIL" 2>nul
if errorlevel 1 (
    echo Installiere benoetigte Pakete ^(einmalig^)...
    python -m pip install customtkinter pillow --quiet
)
python aion_dps_meter.py
if errorlevel 1 (
    echo.
    echo Es ist ein Fehler aufgetreten. Siehe Meldung oben.
    pause
)
