@echo off
rem 通过闸门启动 Claude Code（推荐的接入方式）。
rem
rem 为什么不直接改全局 ~/.claude/settings.json：
rem   CC-Switch 会不定时重写该文件，把 ANTHROPIC_BASE_URL 改回它自己的端口，改了会被覆盖。
rem   也不能只靠 shell 环境变量：实测 settings.json 的 env 块优先级更高，会把它盖掉。
rem   所以这里用 claude --settings 传一份独立配置，CC-Switch 不碰它，设置稳定生效。
rem
rem 用法：把平时启动 Claude Code 的命令换成双击本脚本，
rem       或在命令行里 connect-via-gate.bat 加上原有参数（参数会原样透传给 claude）
rem
rem 注意：本文件必须是 CRLF 行尾，cmd.exe 解析 LF 行尾的 bat 会把注释行拆断执行。
rem       注释里也别写百分号，cmd 对 rem 行同样做百分号展开。
chcp 65001 >nul
setlocal

rem 闸门地址，可用环境变量覆盖：set "GATE_HOST=..." / set "GATE_PORT=..."
if not defined GATE_HOST set "GATE_HOST=127.0.0.1"
if not defined GATE_PORT set "GATE_PORT=17530"
rem 容错：命令行里不带引号 set 后面再接命令时，结尾空格会被一起吃进变量，
rem 拼出来的地址就成了 "http://127.0.0.1:17540 /..."，这里统一去掉空格
set "GATE_HOST=%GATE_HOST: =%"
set "GATE_PORT=%GATE_PORT: =%"
set "GATE_URL=http://%GATE_HOST%:%GATE_PORT%"

rem 启动前先探一下闸门，没起来就提醒，避免窗口连不上
where curl >nul 2>nul
if errorlevel 1 goto probe_done
curl -s -m 3 -o nul "%GATE_URL%/__gate/health"
if not errorlevel 1 goto probe_ok
echo [cc-rpm-gate] 警告：闸门 %GATE_URL% 似乎没在跑。
echo                请先运行 start-gate.bat 或 start-gate-py.bat，否则此窗口无法连接。
echo                仍要继续吗？按任意键继续，或关掉窗口取消。
pause >nul
goto probe_done

:probe_ok
echo [cc-rpm-gate] 闸门在线：%GATE_URL%

:probe_done

rem --settings 收文件路径，也收 JSON 字符串（claude --help 里写的是 file-or-json）。
rem 这里直接传内联 JSON：不落临时文件，多窗口同时启动不会互相删配置，窗口被叉掉也没有残留。
rem 反斜杠转义的引号是给 claude.exe 的参数解析用的，cmd 只做变量展开、不动反斜杠。
set "GATE_JSON={\"env\":{\"ANTHROPIC_BASE_URL\":\"%GATE_URL%\"}}"

echo [cc-rpm-gate] 本窗口经闸门访问：%GATE_URL%
echo [cc-rpm-gate] 正在启动 Claude Code...

rem 透传所有参数给 claude；找不到 claude 就提示手动启动
where claude >nul 2>nul
if errorlevel 1 goto no_claude

rem 这里故意不加 call：claude 实际是 npm 生成的 claude.cmd，加 call 会让 cmd 对整行再做一轮
rem 百分号展开，prompt 里带百分号的内容会被改掉（实测例子见 PROGRESS_RPM_GATE.md）。
rem 直接链式调用，参数原样传给 claude，退出码也原样成为本脚本的退出码。
claude --settings "%GATE_JSON%" %*
rem 兜底：若 claude 是独立 exe（不是 npm 的 claude.cmd），上面这行会返回，把退出码带出去
exit /b %errorlevel%

:no_claude
echo [cc-rpm-gate] 没在 PATH 里找到 claude 命令。
echo                可手动启动：claude --settings "%GATE_JSON%" 再加上原有参数
echo                默认端口也可以用项目里的静态配置：claude --settings "%~dp0gate-settings.json"
cmd /k
