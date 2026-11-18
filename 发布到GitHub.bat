@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PY=D:\QUARK\ComfyUI-aki-v3.2\python\python.exe
if not exist "%PY%" set PY=python
echo ================================================================
echo  SDXL 相机工作台 - 发布到 GitHub
echo ================================================================
echo.
echo  用法一：仓库已建好
echo     本文件也可以用命令行传参，例如：
echo       发布到GitHub.bat --repo https://github.com/you/repo.git --token ghp_xxx
echo.
echo  用法二：连仓库一起建（token 需要 repo 权限）
echo       发布到GitHub.bat --name sdxl-camera-studio --token ghp_xxx --public
echo.
echo  不带参数运行 = 只整理本地仓库并提交，不联网。
echo.
"%PY%" -u tools\publish_github.py %*
echo.
pause
