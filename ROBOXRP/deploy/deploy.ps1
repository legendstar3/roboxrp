# ============================================================
# ROBOXRP Bot - Deploy to Oracle Cloud from Windows
# Usage: .\deploy.ps1 -IP "your.server.ip" -KeyFile "path\to\ssh-key.key"
# ============================================================
param(
    [Parameter(Mandatory=$true)]
    [string]$IP,
    
    [Parameter(Mandatory=$true)]
    [string]$KeyFile
)

$User = "ubuntu"
$RemotePath = "/tmp/roboxrp"
$BotPath = Split-Path -Parent $PSScriptRoot  # Parent of deploy/ = ROBOXRP root

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " ROBOXRP Bot - Deploying to Oracle Cloud" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Server: $User@$IP"
Write-Host "  SSH Key: $KeyFile"
Write-Host "  Bot source: $BotPath"
Write-Host ""

# Check SSH key exists
if (-not (Test-Path $KeyFile)) {
    Write-Host "ERROR: SSH key not found: $KeyFile" -ForegroundColor Red
    exit 1
}

# 1. Upload bot files via SCP
Write-Host "[1/5] Uploading bot files..." -ForegroundColor Yellow
# Create remote directory structure
ssh -i $KeyFile -o StrictHostKeyChecking=no "$User@$IP" "mkdir -p $RemotePath/TradingBuddy/TradingBuddy $RemotePath/deploy"

# Copy files
scp -i $KeyFile -o StrictHostKeyChecking=no "$BotPath\requirements.txt" "${User}@${IP}:${RemotePath}/requirements.txt"
scp -i $KeyFile -o StrictHostKeyChecking=no "$BotPath\.env" "${User}@${IP}:${RemotePath}/.env"
scp -i $KeyFile -o StrictHostKeyChecking=no "$BotPath\TradingBuddy\TradingBuddy\unified_trading_system_WORKING.py" "${User}@${IP}:${RemotePath}/TradingBuddy/TradingBuddy/unified_trading_system_WORKING.py"
scp -i $KeyFile -o StrictHostKeyChecking=no "$BotPath\deploy\setup_server.sh" "${User}@${IP}:${RemotePath}/deploy/setup_server.sh"

Write-Host "  Files uploaded!" -ForegroundColor Green

# 2. Run setup script
Write-Host "[2/5] Running server setup..." -ForegroundColor Yellow
ssh -i $KeyFile "$User@$IP" "sudo cp -r $RemotePath/* /opt/roboxrp/ && sudo cp $RemotePath/.env /opt/roboxrp/.env"
ssh -i $KeyFile "$User@$IP" "cd /opt/roboxrp && sudo bash deploy/setup_server.sh"

# 3. Fix ownership
Write-Host "[3/5] Fixing file ownership..." -ForegroundColor Yellow
ssh -i $KeyFile "$User@$IP" "sudo chown -R botuser:botuser /opt/roboxrp"

# 4. Start bot
Write-Host "[4/5] Starting bot..." -ForegroundColor Yellow
ssh -i $KeyFile "$User@$IP" "sudo systemctl restart roboxrp"
Start-Sleep -Seconds 5

# 5. Check status
Write-Host "[5/5] Checking bot status..." -ForegroundColor Yellow
ssh -i $KeyFile "$User@$IP" "sudo systemctl status roboxrp --no-pager -l"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host " DEPLOYMENT COMPLETE!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host " Useful commands (run via SSH):"
Write-Host "   ssh -i $KeyFile $User@$IP"
Write-Host ""
Write-Host "   sudo systemctl status roboxrp     # Check status"
Write-Host "   sudo journalctl -u roboxrp -f     # Live logs"
Write-Host "   sudo systemctl restart roboxrp    # Restart bot"
Write-Host "   sudo systemctl stop roboxrp       # Stop bot"
Write-Host ""
Write-Host " Dashboard: http://${IP}:5001"
Write-Host " (Open port 5001 in Oracle Cloud Security List first!)"
Write-Host "==========================================" -ForegroundColor Green
