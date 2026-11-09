@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 项目内虚拟环境安装脚本；全部路径用 %%~dp0 相对定位，兼容中文路径与空格。
set "ROOT=%~dp0.."
pushd "%ROOT%" || (echo [错误] 无法进入项目目录 & pause & exit /b 1)

echo ============================================================
echo  STM32 电压表 / 示波器 —— 安装项目内 Python 依赖
echo  项目目录: %CD%
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建项目内虚拟环境 .venv ...
    where python >nul 2>nul
    if errorlevel 1 (
        where py >nul 2>nul
        if errorlevel 1 (
            echo [错误] 未找到 Python。请先安装 Python 3.10+ 并勾选 Add to PATH。
            popd & pause & exit /b 1
        )
        py -3 -m venv ".venv"
    ) else (
        python -m venv ".venv"
    )
) else (
    echo [1/3] 已存在项目内虚拟环境 .venv，跳过创建。
)

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 虚拟环境创建失败，请检查 Python 安装与磁盘权限。
    popd & pause & exit /b 1
)

set "PY=%CD%\.venv\Scripts\python.exe"

echo [2/3] 升级 pip ...
"%PY%" -m pip install --upgrade pip

echo [3/3] 安装 desktop\requirements.txt ...
"%PY%" -m pip install -r "desktop\requirements.txt"
if errorlevel 1 (
    echo [错误] 依赖安装失败。常见原因：无网络、公司代理、Python 版本与 PySide6 不匹配。
    echo        请把上面的 pip 原始输出保留下来，用于排查。
    popd & pause & exit /b 1
)

echo.
echo [完成] 依赖已装入项目内 .venv，未改动全局 Python 环境。
echo        现在可以运行「启动仪器.bat」。
popd
pause
