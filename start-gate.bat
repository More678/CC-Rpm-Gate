@echo off
rem cc-rpm-gate（Node 版）：把多个 Claude Code 窗口的请求统一排队，防止打爆上游 RPM。
rem 和 Python 版共用 gate.config.json，两者只能启动一个（都监听同一个端口）。
rem 注意：本文件必须是 CRLF 行尾，cmd.exe 解析 LF 行尾的 bat 会把注释行拆断执行。
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where node >nul 2>nul
if not errorlevel 1 goto run
echo [cc-rpm-gate] 找不到 node，请先安装 Node.js 18 或更高版本。
pause
exit /b 1

:run
title cc-rpm-gate ^(node^)
echo [cc-rpm-gate] 正在启动 Node 版闸门，按 Ctrl+C 停止。
node "%~dp0src\server.js" %*
set "EXITCODE=%errorlevel%"

if "%EXITCODE%"=="0" goto done
echo.
echo [cc-rpm-gate] 闸门已退出，退出码 %EXITCODE%。
pause

:done
exit /b %EXITCODE%
