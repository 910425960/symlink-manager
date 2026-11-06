@echo off
REM Build SymlinkManager.exe (Windows). Pure ASCII, CRLF.
REM Usage: build.bat  [path-to-python.exe]
REM If a python path is given as %1 it is used, otherwise "python" on PATH.

setlocal
set "PY=%~1"
if "%PY%"=="" set "PY=python"

echo [1/4] Using interpreter: %PY%
"%PY%" --version
if errorlevel 1 goto :err

echo [2/4] Creating build venv (.venv-build) ...
if not exist ".venv-build\Scripts\python.exe" (
    "%PY%" -m venv .venv-build
    if errorlevel 1 goto :err
)

echo [3/4] Installing PyInstaller ...
".venv-build\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet pyinstaller
if errorlevel 1 goto :err

echo [4/4] Building EXE ...
".venv-build\Scripts\pyinstaller.exe" SymlinkManager.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 goto :err

echo.
echo BUILD OK. Output: dist\SymlinkManager.exe
goto :eof

:err
echo.
echo BUILD FAILED. See messages above.
exit /b 1
