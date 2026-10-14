@echo off
rem Build LogGrabber.exe (single file, GUI, embeds collector.ps1)
rem Needs: py -m pip install pyinstaller evtx reportlab
cd /d "%~dp0"
py -m PyInstaller --onefile --windowed --name LogGrabber --icon icon.ico --add-data "collector.ps1;." --add-data "icon.ico;." log_grabber.py
echo.
echo Result: dist\LogGrabber.exe
rem Sign if a cert is available - unsigned self-elevating exes get flagged:
rem signtool sign /fd SHA256 /a dist\LogGrabber.exe
