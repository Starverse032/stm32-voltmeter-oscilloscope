@echo off
chcp 65001 >nul
setlocal EnableExtensions
pushd "%~dp0.." || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv ".venv"
)
if not exist ".venv\Scripts\python.exe" (
    echo 未能创建 Python 环境。请先安装 Python 3.13，再重新运行。
    popd
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r "swd_instruments\requirements.txt"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo 依赖安装失败，请保留上方错误信息。
if "%RC%"=="0" echo 安装完成。请运行本目录的“启动SWD仪器.bat”。
popd
pause
exit /b %RC%
