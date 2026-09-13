@echo off
REM Start the media server and the channel page.
REM
REM Thin on purpose, matching run.sh: all the checks live in tools\launch.py so
REM there is one copy of them instead of three that drift apart.
REM
REM Not tested on a real Windows machine -- see server\README.md, which says so
REM plainly. It is written from the documented behaviour of the commands used.
setlocal

cd /d "%~dp0"

REM The py launcher first: it exists wherever Python was installed from
REM python.org, and unlike a bare "python" it is not shadowed by the Microsoft
REM Store stub that opens the Store instead of running anything.
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 tools\launch.py %*
    goto :done
)

where python >nul 2>&1
if %errorlevel%==0 (
    python tools\launch.py %*
    goto :done
)

echo.
echo Python not found.
echo This project needs Python 3.9 or newer.
echo Download it from https://www.python.org/downloads/
echo During installation, tick "Add Python to PATH".
echo.

:done
REM Keep the window open when double-clicked, so the message above can be read.
if "%~1"=="" pause
endlocal
