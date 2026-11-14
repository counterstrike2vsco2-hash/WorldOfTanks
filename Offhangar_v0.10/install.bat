@echo off
setlocal
cd /d "%~dp0"

echo === OffHangar v125 installer
echo.

set "PY="
if exist "C:\Python27\python.exe" set "PY=C:\Python27\python.exe"
if not defined PY (
  python -c "import sys;sys.exit(0 if sys.version_info[:2]==(2,7) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  py -2.7 -c "import sys" >nul 2>nul && set "PY=py -2.7"
)
if not defined PY (
  echo Python 2.7 not found. Install it from https://www.python.org/downloads/release/python-2718/
  pause
  exit /b 1
)

set "OUT=res_mods\0.9.0\scripts\client\gui\mods\offhangar"
if not exist "%OUT%" mkdir "%OUT%"

for %%F in (_constants nav drown matchmaker mm_core mm_forced capture spotting mapselect queuefake fakeprb tutor perf devices turret destructibles utils server logging bots battle shooting endbattle feedback dossier data requests) do (
  echo Compiling %%F.py ...
  %PY% -c "import py_compile,sys; py_compile.compile(sys.argv[1], sys.argv[2], sys.argv[3], True)" "src\gui_mods\offhangar\%%F.py" "%OUT%\%%F.pyc" "gui/mods/offhangar/%%F.py"
  if errorlevel 1 (
    echo.
    echo COMPILE ERROR in %%F.py - nothing was copied to the game.
    pause
    exit /b 1
  )
)
echo.
echo Compiling mod_offhangar.py ...
%PY% -c "import py_compile,sys; py_compile.compile(sys.argv[1], sys.argv[2], sys.argv[3], True)" "src\gui_mods\mod_offhangar.py" "res_mods\0.9.0\scripts\client\gui\mods\mod_offhangar.pyc" "gui/mods/mod_offhangar.py"
if errorlevel 1 (
  echo COMPILE ERROR in mod_offhangar.py - nothing was copied to the game.
  pause
  exit /b 1
)
echo Compiled OK.
echo.

set "GAME=%~1"
if not defined GAME (
  echo Drag your World of Tanks folder into this window and press Enter:
  set /p "GAME=> "
)
set "GAME=%GAME:"=%"

if not exist "%GAME%\WorldOfTanks.exe" if not exist "%GAME%\res_mods" (
  echo "%GAME%" does not look like a World of Tanks folder.
  echo Compiled files are in %CD%\%OUT% - copy all *.pyc manually.
  pause
  exit /b 1
)

xcopy /E /Y /I "res_mods\0.9.0" "%GAME%\res_mods\0.9.0\" >nul
if errorlevel 1 (
  echo Copy failed.
  pause
  exit /b 1
)
if exist "%GAME%\offhangar_debug.log" del "%GAME%\offhangar_debug.log"
if not exist "%GAME%\offline_user.txt" copy /Y "offline_user.txt" "%GAME%\offline_user.txt" >nul

echo DONE. Old offhangar_debug.log deleted. Start the game.
pause
