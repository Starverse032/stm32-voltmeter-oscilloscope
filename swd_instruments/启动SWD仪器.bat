@echo off
chcp 65001 >nul
setlocal EnableExtensions
pushd "%~dp0.." || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo 请先运行 swd_instruments\安装SWD依赖.bat，再启动本工具。
    popd
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m swd_instruments
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" pause
exit /b %RC%
