@echo off
rem SDXL Camera Studio launcher (console visible)
setlocal
cd /d "%~dp0"
set "PYEXE=D:\QUARK\ComfyUI-aki-v3.2\python\python.exe"
if exist "%PYEXE%" goto :run
set PYEXE=python
:run
"%PYEXE%" "%~dp0launch.py" %*
if errorlevel 1 (
  echo.
  echo [launch] exited with error, press any key to close
  pause >nul
)
endlocal