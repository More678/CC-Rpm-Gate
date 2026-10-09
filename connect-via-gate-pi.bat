@echo off
rem Launch pi through cc-rpm-gate. Docs: pi\README.md (en), pi\README.zh-CN.md (zh)
rem
rem This file is deliberately ASCII-only. Reason, measured on this repo:
rem   cmd.exe tracks a byte offset while parsing a .bat, but counts
rem   characters under chcp 65001. Any non-ASCII text in the file can
rem   desync that offset, after which cmd resumes mid-line and runs the
rem   tail of a line as a command ("'XX' is not recognized ...").
rem   Whether it lands on a line boundary depends on the exact byte
rem   layout, so a file with Chinese text may look fine until edited.
rem   connect-via-gate.bat currently survives this by luck; this file did
rem   not (3-7 broken lines), so all its text is ASCII and the
rem   Chinese status output is printed by pi\gate-extension.ts instead.
rem Also required: CRLF line endings, and no percent signs in rem lines.
chcp 65001 >nul
setlocal

rem Gate address. Override with GATE_HOST / GATE_PORT.
if not defined GATE_HOST set "GATE_HOST=127.0.0.1"
if not defined GATE_PORT set "GATE_PORT=17530"
rem Strip stray spaces picked up from unquoted set before a chained command.
set "GATE_HOST=%GATE_HOST: =%"
set "GATE_PORT=%GATE_PORT: =%"
set "GATE_URL=http://%GATE_HOST%:%GATE_PORT%"
rem The extension reads this too, so it need not guess the address.
set "PI_GATE_URL=%GATE_URL%"

set "GATE_EXT=%~dp0pi\gate-extension.ts"
if exist "%GATE_EXT%" goto ext_ok
echo [cc-rpm-gate] Extension not found: %GATE_EXT%
pause
exit /b 1

:ext_ok

rem Probe the gate first; pi would otherwise bypass it and hit upstream direct.
where curl >nul 2>nul
if errorlevel 1 goto probe_done
curl -s -m 3 -o nul "%GATE_URL%/__gate/health"
if not errorlevel 1 goto probe_ok
echo [cc-rpm-gate] WARNING: gate %GATE_URL% does not look like it is running.
echo                Start start-gate.bat or start-gate-py.bat first.
echo                Otherwise pi talks to the upstream directly and neither
echo                the rate limit nor the 429 stashing applies.
echo                Press any key to continue anyway, or close this window.
pause >nul
goto probe_done

:probe_ok
echo [cc-rpm-gate] gate online: %GATE_URL%

:probe_done

where pi >nul 2>nul
if errorlevel 1 goto no_pi

echo [cc-rpm-gate] routing this window through %GATE_URL%
echo [cc-rpm-gate] run /gate inside pi to see gate status
echo [cc-rpm-gate] starting pi ...

rem No "call" here: pi is an npm-generated pi.cmd, and call makes cmd run
rem another percent-expansion pass over the whole line, which rewrites any
rem percent signs inside the prompt. Chain directly so the exit code carries.
pi -e "%GATE_EXT%" %*
exit /b %errorlevel%

:no_pi
echo [cc-rpm-gate] pi was not found on PATH.
echo                Start it manually: pi -e "%GATE_EXT%" plus your usual args
cmd /k
