# ROBOXRP PRO v13.2 - Trading System Launcher (PowerShell)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

Write-Host "================================================================================" -ForegroundColor Green
Write-Host "   ROBOXRP PRO Trading System v13.2" -ForegroundColor Yellow
Write-Host "   OKX Spot - Multi-Timeframe Strategy" -ForegroundColor Yellow
Write-Host "================================================================================" -ForegroundColor Green
Write-Host ""

# Change to script directory first
Set-Location $PSScriptRoot

# Verify .env exists (critical — contains API keys and all config)
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env file not found in $PSScriptRoot" -ForegroundColor Red
    Write-Host "Create .env with your OKX API keys and trading parameters." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

# Check for venv or system Python
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvPip = Join-Path $PSScriptRoot ".venv\Scripts\pip.exe"

if (Test-Path $venvPython) {
    Write-Host "Virtual Environment found: .venv" -ForegroundColor Green
    $pythonCmd = $venvPython
    $pipCmd = $venvPip
} else {
    try {
        $pythonVersion = python --version 2>$null
        if ($LASTEXITCODE -ne 0) { throw "not found" }
        Write-Host "Python found: $pythonVersion" -ForegroundColor Green
        $pythonCmd = "python"
        $pipCmd = "pip"
    } catch {
        Write-Host "ERROR: Python not found!" -ForegroundColor Red
        Write-Host "Please install Python 3.9+ from https://python.org" -ForegroundColor Yellow
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# Install dependencies from root requirements.txt
Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $pipCmd install -r requirements.txt --quiet 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: pip install had issues. Attempting to continue..." -ForegroundColor Yellow
}

# Navigate to bot directory
Set-Location "TradingBuddy\TradingBuddy"

if (-not (Test-Path "unified_trading_system_WORKING.py")) {
    Write-Host "ERROR: unified_trading_system_WORKING.py not found!" -ForegroundColor Red
    Write-Host "Expected at: $(Get-Location)\unified_trading_system_WORKING.py" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Trading system found" -ForegroundColor Green
Write-Host ""

# Run system
Write-Host "Launching ROBOXRP PRO v13.2..." -ForegroundColor Green
Write-Host "Dashboard: http://localhost:5001" -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop gracefully (active trades will be closed)" -ForegroundColor Red
Write-Host ""
& $pythonCmd -B -u unified_trading_system_WORKING.py

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "   Session Complete - Bot shut down gracefully" -ForegroundColor Yellow
Write-Host "================================================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to exit"
