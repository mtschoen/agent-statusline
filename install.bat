@echo off
rem Install statusline hooks/settings for the selected platform.
rem Re-run any time -- it preserves unrelated existing keys.
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

where python >nul 2>&1
if errorlevel 1 (
    echo error: python not found on PATH
    exit /b 1
)

python "%SCRIPT_DIR%\install.py" --repo "%SCRIPT_DIR%" %*
exit /b %ERRORLEVEL%
