@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
color 0A
title ROBOXRP PRO Trading System v13.2

echo.
echo ================================================================================
echo    ROBOXRP PRO Trading System v13.2
echo    OKX Spot - Multi-Timeframe Strategy
echo ================================================================================
echo.

cd /d "%~dp0"

:: Verify .env exists (critical — contains API keys and all config)
if not exist ".env" (
    echo ERROR: .env file not found in %cd%
    echo Create .env with your OKX API keys and trading parameters.
    pause
    exit /b 1
)

echo Checking Python environment...
if exist ".venv\Scripts\python.exe" (
    echo Virtual Environment found: .venv
    set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
    set "PIP_CMD=%~dp0.venv\Scripts\pip.exe"
) else (
    echo Using System Python
    python --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found!
        echo Please install Python 3.9+ from https://python.org
        pause
        exit /b 1
    )
    set PYTHON_CMD=python
    set PIP_CMD=pip
)

:: Install dependencies from root requirements.txt first
echo.
echo Installing dependencies...
"%PIP_CMD%" install -q -r requirements.txt
if errorlevel 1 (
    echo WARNING: pip install had issues. Attempting to continue...
)

:: Navigate to bot directory
cd TradingBuddy\TradingBuddy

if not exist "unified_trading_system_WORKING.py" (
    echo ERROR: unified_trading_system_WORKING.py not found!
    echo Expected at: %cd%\unified_trading_system_WORKING.py
    pause
    exit /b 1
)

echo.
echo Launching ROBOXRP PRO v13.2...
echo Dashboard: http://localhost:5001
echo Press Ctrl+C to stop gracefully (active trades will be closed)
echo.
"%PYTHON_CMD%" -B -u unified_trading_system_WORKING.py

echo.
echo ================================================================================
echo    Session Complete - Bot shut down gracefully
echo ================================================================================
echo.
pause
