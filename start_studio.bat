@echo off
rem SDXL Camera Angle - one click studio launcher (Windows)
rem Root start script: finds a python, then runs studio/launch.py.
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY if exist "D:\QUARK\ComfyUI-aki-v3.2\python\python.exe" set "PY=D:\QUARK\ComfyUI-aki-v3.2\python\python.exe"
if not defined PY (
  echo [错误] 找不到可用的 Python 解释器。
  echo 请安装 Python 3.10 以上并加入 PATH，
  echo 或把 ComfyUI 自带的 python 放在 D:\QUARK\ComfyUI-aki-v3.2\python\python.exe
  pause
  exit /b 1
)
echo [启动] 解释器: %PY%
if not exist "%~dp0studio\launch.py" (
  echo [错误] 缺少 studio\launch.py，请在仓库根目录运行本脚本。
  pause
  exit /b 1
)
"%PY%" "%~dp0studio\launch.py" %*
if errorlevel 1 (
  echo.
  echo [错误] 工作台启动失败，请把上面的报错信息发给开发者。
  pause
  exit /b 1
)
endlocal
