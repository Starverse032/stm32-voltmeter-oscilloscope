# ============================================================================
# build.ps1 —— 用本机 Keil uVision (UV4.exe) 命令行构建固件
#
# 用法（在 firmware/ 目录下，PowerShell）：
#     powershell -ExecutionPolicy Bypass -File .\build.ps1
#   或直接：  .\build.ps1
#
# 命令行方式：UV4.exe -b Project.uvprojx -j0 -o Objects\build.log
#   退出码：0 = 成功，1 = 有警告，2 及以上 = 有错误。
#   构建日志保留在 firmware/Objects/build.log；HEX 输出到 firmware/Objects/Project.hex
#   （CreateHexFile 已在 Project.uvprojx 中置 1）。
#
# 本项目使用独立工程配置，不修改电协笔试参考工程。
# ============================================================================

$ProjectDir = $PSScriptRoot
if (-not $ProjectDir) { $ProjectDir = (Get-Location).Path }

$Keil   = 'C:\Keil_v5\UV4\UV4.exe'
$LogRel = 'Objects\build.log'
$LogAbs = Join-Path $ProjectDir $LogRel

if (-not (Test-Path $Keil)) {
    Write-Host "ERROR: 未找到 UV4.exe：$Keil"
    exit 2
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir 'Objects')  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir 'Listings') | Out-Null

$p = Start-Process $Keil -ArgumentList '-b','Project.uvprojx','-j0','-o',$LogRel `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden -Wait -PassThru

Write-Host "===== build.log ====="
if (Test-Path $LogAbs) { Get-Content $LogAbs } else { Write-Host "(未生成 build.log)" }
Write-Host "===== UV4 ExitCode = $($p.ExitCode)  (0=成功, 1=警告, 2+=错误) ====="

exit $p.ExitCode
