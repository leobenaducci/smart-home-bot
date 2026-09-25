#!/usr/bin/env powershell
# HomeCameras Network Setup Script
# This script configures Windows Firewall to allow network access to the camera server

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "HomeCameras Network Configuration Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator', then run this script again." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Press any key to exit..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

# Configuration
$PORT = 5000
$PYTHON_PATH = "python.exe"

Write-Host "[1/4] Checking current firewall rules..." -ForegroundColor Yellow
$existingRule = Get-NetFirewallRule -DisplayName "HomeCameras Server" -ErrorAction SilentlyContinue
if ($existingRule) {
    Write-Host "  - Removing existing firewall rule..." -ForegroundColor Yellow
    Remove-NetFirewallRule -DisplayName "HomeCameras Server" -ErrorAction SilentlyContinue
}

Write-Host "[2/4] Creating firewall rule for port $PORT..." -ForegroundColor Yellow
try {
    New-NetFirewallRule -DisplayName "HomeCameras Server" `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $PORT `
        -Action Allow `
        -Profile Private,Public `
        -Description "Allow incoming connections to HomeCameras web server" | Out-Null
    Write-Host "  - Firewall rule created successfully!" -ForegroundColor Green
} catch {
    Write-Host "  - ERROR: Failed to create firewall rule: $_" -ForegroundColor Red
}

Write-Host "[3/4] Verifying server binding configuration..." -ForegroundColor Yellow
# Check if server is configured to bind to all interfaces
if (Test-Path "main.py") {
    $hostConfig = Select-String -Path "main.py" -Pattern "default='0\.0\.0\.0'" -SimpleMatch
    if ($hostConfig) {
        Write-Host "  - Server is configured to bind to all interfaces (0.0.0.0)" -ForegroundColor Green
    } else {
        Write-Host "  - WARNING: Server may not be configured to bind to all interfaces" -ForegroundColor Yellow
    }
} else {
    Write-Host "  - WARNING: main.py not found in current directory" -ForegroundColor Yellow
}

Write-Host "[4/4] Testing port $PORT listening status..." -ForegroundColor Yellow
$listening = netstat -an | Select-String ":$PORT.*LISTENING"
if ($listening) {
    Write-Host "  - Port $PORT is listening:" -ForegroundColor Green
    Write-Host "    $($listening.Line)" -ForegroundColor Green
} else {
    Write-Host "  - Port $PORT is NOT currently listening" -ForegroundColor Yellow
    Write-Host "  - Start the server with: python main.py" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Network Configuration Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "To access from another PC:" -ForegroundColor White
Write-Host "  1. Find your IP address: ipconfig | Select-String 'IPv4'" -ForegroundColor Gray
Write-Host "  2. Open browser: http://YOUR_IP:$PORT" -ForegroundColor Gray
Write-Host ""
Write-Host "Quick IP lookup:" -ForegroundColor White
$ipConfig = ipconfig | Select-String "IPv4" | Select-String -Pattern "\d+\.\d+\.\d+\.\d+" -AllMatches
if ($ipConfig.Matches) {
    Write-Host "  Your IP addresses:" -ForegroundColor Gray
    $ipConfig.Matches.Value | ForEach-Object { Write-Host "    $_" -ForegroundColor Cyan }
}
Write-Host ""