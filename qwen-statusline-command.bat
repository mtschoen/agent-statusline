@echo off
REM Windows batch shim for Qwen Code statusline.
REM Forwards stdin to qwen_statusline.py via the fastest available Python.

where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py -3 "%~dp0qwen_statusline.py"
) else (
    python "%~dp0qwen_statusline.py"
)
