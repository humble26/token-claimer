@echo off
rem Calibrate an app's claim entry: dump its UI element list to
rem logs\ui_dump_<key>_<tag>_<stamp>.txt so the keywords can be pinned down.
rem
rem READ-ONLY with respect to the app: this script never closes and never
rem restarts it. An earlier version did taskkill + relaunch, which hard-killed
rem the whole Electron process tree and left the app unresponsive. That code is
rem gone for good - do not bring it back.
rem
rem Electron clients only expose their UI tree when started with
rem --force-renderer-accessibility, so:
rem   * app NOT running -> this script starts it with that flag (harmless);
rem   * app running     -> read its tree as-is; if the tree is empty, quit the
rem                        app YOURSELF (tray icon -> quit) and run this again.
rem
rem Usage: 校准领取入口.bat [app-key]      (default: traework)
cd /d "%~dp0"

set KEY=%~1
if "%KEY%"=="" set KEY=traework

set OPEN=
choice /C YN /N /M "Also open the account menu (menu only, no claim)? [Y/N] "
if errorlevel 2 goto run
set OPEN=--open-menu

:run
if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
    "%LOCALAPPDATA%\Python\bin\python.exe" token_claimer.py --calibrate %KEY% %OPEN%
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python token_claimer.py --calibrate %KEY% %OPEN%
    goto done
)
where py >nul 2>nul
if %errorlevel%==0 (
    py token_claimer.py --calibrate %KEY% %OPEN%
    goto done
)
echo Python not found. Install Python 3.8+ first.

:done
echo.
pause
