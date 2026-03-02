# ROBOXRP Bot — Oracle Cloud Deployment Guide

## Cost: €0/month (Always Free Tier)

---

## Step 1: Create Oracle Cloud Account

1. Go to **https://cloud.oracle.com/free**
2. Click **"Start for Free"**
3. Fill in: email, name, country
4. **Home Region**: select **eu-frankfurt-1** (closest to OKX servers)
5. Add credit card (verification only — €1 charge returned, never billed)
6. Wait for activation (usually instant, up to 30min)

## Step 2: Create VM Instance

1. Dashboard → **"Create a VM instance"**
2. **Name**: `roboxrp-bot`
3. **Image and shape**:
   - Click **"Edit"** → **"Change image"** → Select **Ubuntu 22.04**
   - Click **"Change shape"** → **"VM.Standard.E2.1.Micro"** (Always Free ✅)
   - This gives: 1 vCPU, 1GB RAM (plenty for the bot)
4. **Networking**: keep defaults, ensure **"Assign a public IPv4 address"** is checked
5. **Add SSH keys**:
   - Select **"Generate a key pair for me"**
   - **Download BOTH** the private key (.key) and public key (.pub)
   - **Save them safely!** You need the .key file to connect
6. Click **"Create"**
7. Wait ~2min for the instance to be "Running"
8. Copy the **Public IP address** shown on the instance page

## Step 3: Open Firewall Ports

To access the dashboard remotely:

1. Instance details → **"Subnet"** link → **"Security Lists"** → Default
2. **"Add Ingress Rules"**:
   - Source CIDR: `0.0.0.0/0`
   - Destination Port Range: `5001`
   - Description: `Bot Dashboard`
3. Also SSH into the VM and run:
   ```bash
   sudo iptables -I INPUT -p tcp --dport 5001 -j ACCEPT
   sudo netfilter-persistent save
   ```

## Step 4: Deploy the Bot

### Option A: Automated (recommended)
From PowerShell on your Windows PC:
```powershell
cd C:\Users\User\Desktop\ROBOXRP\deploy
.\deploy.ps1 -IP "YOUR_SERVER_IP" -KeyFile "C:\path\to\ssh-key.key"
```

### Option B: Manual
```bash
# 1. SSH into your VM
ssh -i your-key.key ubuntu@YOUR_SERVER_IP

# 2. Upload files (from another terminal on Windows)
scp -i your-key.key -r C:\Users\User\Desktop\ROBOXRP\* ubuntu@YOUR_SERVER_IP:/tmp/roboxrp/

# 3. Back on the VM, run setup
sudo mkdir -p /opt/roboxrp
sudo cp -r /tmp/roboxrp/* /opt/roboxrp/
sudo cp /tmp/roboxrp/.env /opt/roboxrp/.env
cd /opt/roboxrp
sudo bash deploy/setup_server.sh

# 4. Fix ownership and start
sudo chown -R botuser:botuser /opt/roboxrp
sudo systemctl start roboxrp
```

## Step 5: Verify

```bash
# Check bot is running
sudo systemctl status roboxrp

# View live logs
sudo journalctl -u roboxrp -f

# Check from browser
# http://YOUR_SERVER_IP:5001
```

---

## Daily Operations

| Command | What it does |
|---------|-------------|
| `sudo systemctl status roboxrp` | Check if bot is running |
| `sudo journalctl -u roboxrp -f` | Live log stream |
| `sudo journalctl -u roboxrp --since "1 hour ago"` | Recent logs |
| `sudo systemctl restart roboxrp` | Restart bot |
| `sudo systemctl stop roboxrp` | Stop bot |
| `sudo nano /opt/roboxrp/.env` | Edit config |

## Auto-Recovery

The systemd service is configured to:
- **Auto-start** on VM reboot
- **Auto-restart** on crash (after 30s delay)
- **Max 5 restarts** per 5 minutes (prevents crash loops)

## Updating the Bot

```bash
# On Windows, re-run deploy:
.\deploy.ps1 -IP "YOUR_SERVER_IP" -KeyFile "C:\path\to\ssh-key.key"

# Or manually:
scp -i key.key unified_trading_system_WORKING.py ubuntu@IP:/tmp/
ssh -i key.key ubuntu@IP "sudo cp /tmp/unified_trading_system_WORKING.py /opt/roboxrp/TradingBuddy/TradingBuddy/ && sudo chown botuser:botuser /opt/roboxrp/TradingBuddy/TradingBuddy/unified_trading_system_WORKING.py && sudo systemctl restart roboxrp"
```

## Security Notes

- Bot runs as non-root user `botuser`
- API keys stored in `/opt/roboxrp/.env` (readable only by botuser)
- SSH key is the only way to access the VM
- Dashboard on port 5001 — restrict IP in Security List if needed
