@echo off
rem Speed benchmark of offline TTS engines. Results go to results\<computer name>\
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 goto win32
if not exist "runtime\python.exe" goto noruntime

"runtime\python.exe" bench\bench.py %*
pause
exit /b

:win32
echo This is 32-bit Windows. The benchmark needs 64-bit Windows.
pause
exit /b 1

:noruntime
echo runtime\python.exe not found. Run prepare.bat first - at home, with internet.
pause
exit /b 1
