@echo off
rem cc-rpm-gate（Python 版）：功能与 Node 版对等，共用同一份 gate.config.json。
rem 两个版本监听同一个端口，只能启动其中一个。
rem 注意：本文件必须是 CRLF 行尾，cmd.exe 解析 LF 行尾的 bat 会把注释行拆断执行。
chcp 65001 >nul
setlocal
cd /d "%~dp0python"

rem 日志里有中文，强制 UTF-8 输出，避免重定向到文件时编码报错
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

rem 优先用 py 启动器，没有就退回 PATH 里的 python；要求 3.8+
rem 探测语句直接写在命令行里（不走变量），否则里面的 >= 会被当成重定向；
rem 也不要放进 if ( ) 块里，块内的 (3,8) 会让 cmd 的括号配对出错。
set "PYEXE="
py -3 -c "import sys;sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto run

python -c "import sys;sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto run

echo [cc-rpm-gate] 找不到 Python 3.8 或更高版本，请先安装。
pause
exit /b 1

:run
title cc-rpm-gate ^(python^)
echo [cc-rpm-gate] 正在启动 Python 版闸门，按 Ctrl+C 停止。
%PYEXE% -m gate %*
set "EXITCODE=%errorlevel%"

if "%EXITCODE%"=="0" goto done
echo.
echo [cc-rpm-gate] 闸门已退出，退出码 %EXITCODE%。
pause

:done
exit /b %EXITCODE%
