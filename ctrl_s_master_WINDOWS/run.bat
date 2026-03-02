@echo off
setlocal

:: =================================================================
::               MASTER AUTOMATION SCRIPT (SUPERVISOR)
:: =================================================================
::
::  Author:         gravi-ctrl
::  Created:        2025-05-17
::  Last Modified:  2025-11-06
::
::  Description:    The master supervisor script that orchestrates
::                  the entire automation workflow.
::
:: =================================================================

:: --- 1. Setup Paths ---
set "SCRIPT_DIR=%~dp0"
set "LOG_DIR=%SCRIPT_DIR%_logs\"
IF NOT EXIST "%LOG_DIR%" mkdir "%LOG_DIR%"

FOR /F "usebackq" %%I IN (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"`) DO set "timestamp=%%I"
set "LOG_FILE=%LOG_DIR%run_%timestamp%.log"

set "PYTHON_EXE=%SCRIPT_DIR%venv\Scripts\python.exe"
set "MASTER_SCRIPT=%SCRIPT_DIR%src\master_automation.py"

:: --- Clean up any old failure logs before starting ---
del /q "%LOG_DIR%failure_details.log" 2>nul

:: --- 2. Initial Console Output ---
echo Starting automation tasks...
echo This window will remain black. Detailed log will be at:
echo %LOG_FILE%

:: --- 3. PHASE 1: Execute all tasks and create the primary log file ---
(
    "%PYTHON_EXE%" "%MASTER_SCRIPT%" run-tasks run-all
) > "%LOG_FILE%" 2>&1

:: --- 4. Capture the exit code from Phase 1 ---
set "FINAL_ERRORLEVEL=%errorlevel%"

:: --- 5. PHASE 2: Pre-generate the report's log entries and append to the main log ---
echo. >> "%LOG_FILE%"
if %FINAL_ERRORLEVEL% equ 0 (
    (
        "%PYTHON_EXE%" "%MASTER_SCRIPT%" send-report success --generate-log-only
    ) >> "%LOG_FILE%" 2>&1
) else (
    (
        "%PYTHON_EXE%" "%MASTER_SCRIPT%" send-report failure --generate-log-only
    ) >> "%LOG_FILE%" 2>&1
)

:: --- 6. PHASE 3: Send the final email with the now-complete log, discarding screen output ---
if %FINAL_ERRORLEVEL% equ 0 (
    "%PYTHON_EXE%" "%MASTER_SCRIPT%" send-report success > NUL 2>&1
) else (
    "%PYTHON_EXE%" "%MASTER_SCRIPT%" send-report failure > NUL 2>&1
)

:: --- 7. Final Console Message ---
echo.
echo Automation run finished. Check log for details:
echo %LOG_FILE%

:: --- 8. Exit with the final error code from the main task run ---
exit /b %FINAL_ERRORLEVEL%