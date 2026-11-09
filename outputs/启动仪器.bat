@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 电脑端仪器窗口启动入口；只使用项目内 .venv，不要求全局安装。
set "ROOT=%~dp0.."
pushd "%ROOT%" || (echo [错误] 无法进入项目目录 & pause & exit /b 1)

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 未找到项目内虚拟环境 .venv。
    echo        请先双击运行同目录下的「安装依赖.bat」，再回来启动。
    popd & pause & exit /b 1
)

if not exist "desktop\main.py" (
    echo [错误] 未找到 desktop\main.py，交付文件可能不完整。
    popd & pause & exit /b 1
)

echo ============================================================
echo  STM32 电压表 / 示波器 —— 电脑端仪器窗口
echo  项目目录: %CD%
echo  没接开发板时请点界面上的「模拟数据」，界面会明确标注来源。
echo  关闭本窗口或按 Ctrl+C 即可退出程序。
echo ============================================================
echo.

"%CD%\.venv\Scripts\python.exe" "desktop\main.py"
set "RC=%ERRORLEVEL%"
echo.
echo [程序已退出] 退出码 %RC%
if not "%RC%"=="0" (
    echo        若出现 Traceback，请把上面的原始输出保留下来用于排查。
)
popd
pause
