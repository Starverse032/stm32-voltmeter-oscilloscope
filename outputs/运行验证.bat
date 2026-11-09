@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0.." || exit /b 1
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 请先运行 outputs\安装依赖.bat 创建项目虚拟环境。
    popd
    pause
    exit /b 1
)
"%PY%" -X utf8 "outputs\最终验证.py"
set "VERIFY_RC=%ERRORLEVEL%"
echo.
echo [完成] 最终验证退出码 %VERIFY_RC%，详细结果见 outputs\验证日志.txt
popd
pause
exit /b %VERIFY_RC%
