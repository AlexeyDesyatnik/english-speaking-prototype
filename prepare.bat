@echo off
rem Run at home, with internet: downloads Python, packages and models (~800 MB)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare.ps1" %*
pause
