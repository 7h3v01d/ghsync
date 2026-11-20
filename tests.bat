@echo off
rem Runs the ghsync regression suite (engine + offscreen GUI if PyQt6 is installed).
cd /d "%~dp0"
python -m unittest discover -s tests -v
pause