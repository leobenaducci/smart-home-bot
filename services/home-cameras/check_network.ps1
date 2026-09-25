#!/usr/bin/env powershell
# HomeCameras Network Diagnostic Script
# This script helps diagnose network access issues

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "HomeCameras Network Diagnostic" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$PORT = 5000

# 1. Check if server is running
Write-Host "[DIAGNOSTIC] Checking if server is running..." -ForegroundColor Yellow
$listening = netstat -an | Select-String ":$PORT.*LISTENING"
if ($listening) {
    Write-Host "  [PASS] Port $PORT is listening" -ForegroundColor Green
    Write-Host "    $($listening.Line)" -ForegroundColor Gray
} else {
    Write-Host "  [FAIL] Port $PORT is NOT listening" -ForegroundColor Red
    Write-Host "    Start the server with: python main.py" -ForegroundColor Cyan
}

# 2. Check firewall rules
Write-Host ""
Write-Host "[DIAGNOSTIC] Checking firewall rules..." -ForegroundColor Yellow
$firewallRule = Get-NetFirewallRule -DisplayName "HomeCameras Server" -ErrorAction SilentlyContinue
if ($firewallRule) {
    Write-Host "  [PASS] Firewall rule exists" -ForegroundColor Green
    Write-Host "    Profile: $($firewallRule.Profile)" -ForegroundColor Gray
    Write-Host "    Action: $($firewallRule.Action)" -ForegroundColor Gray
} else {
    Write-Host "  [FAIL] No firewall rule found for HomeCameras" -ForegroundColor Red
    Write-Host "    Run setup_network.ps1 to create the rule" -ForegroundColor Cyan
}

# 3. Check binding address
Write-Host ""
Write-Host "[DIAGNOSTIC] Checking server binding..." -ForegroundColor Yellow
$bindingInfo = netstat -an | Select-String ":$PORT.*LISTENING"
if ($bindingInfo) {
    $bindingAddress = ($bindingInfo.Line -split '\s+')[1] -replace ":$PORT", ""
    if ($bindingAddress -eq "0.0.0.0") {
        Write-Host "  [PASS] Server is bound to all interfaces (0.0.0.0)" -ForegroundColor Green
    } elseif ($bindingAddress -eq "127.0.0.1") {
        Write-Host "  [FAIL] Server is bound to localhost only (127.0.0.1)" -ForegroundColor Red
        Write-Host "    Modify main.py to use --host 0.0.0.0" -ForegroundColor Cyan
    } else {
        Write-Host "  [INFO] Server is bound to: $bindingAddress" -ForegroundColor Yellow
    }
}

# 4. Get IP addresses
Write-Host ""
Write-Host "[DIAGNOSTIC] Your IP addresses:" -ForegroundColor Yellow
$ipConfig = ipconfig | Select-String "IPv4" | Select-String -Pattern "\d+\.\d+\.\d+\.\d+" -AllMatches
if ($ipConfig.Matches) {
    $ipConfig.Matches.Value | ForEach-Object {
        Write-Host "    $_" -ForegroundColor Cyan
    }
} else {
    Write-Host "    No IPv4 addresses found" -ForegroundColor Yellow
}

# 5. Test local access
Write-Host ""
Write-Host "[DIAGNOSTIC] Testing local access..." -ForegroundColor Yellow
try {
    $response = Invoke-WebRequest -Uri "http://localhost:$PORT" -TimeoutSec 2 -ErrorAction SilentlyContinue
    if ($response) {
        Write-Host "  [PASS] Local access works (HTTP $($response.StatusCode))" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] Local access failed" -ForegroundColor Red
    }
} catch {
    Write-Host "  [FAIL] Local access failed: $_" -ForegroundColor Red
}

# 6. Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Diagnostic Summary" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "To access from another PC on your network:" -ForegroundColor White
Write-Host "  1. Use any IP address shown above" -ForegroundColor Gray
Write-Host "  2. Open browser: http://IP:$PORT" -ForegroundColor Gray
Write-Host "  3. Make sure you're on the same network" -ForegroundColor Gray
Write-Host ""
Write-Host "If still can't connect:" -ForegroundColor Yellow
Write-Host "  - Run setup_network.ps1 as Administrator" -ForegroundColor Gray
Write-Host "  - Check your router has no client isolation enabled" -ForegroundColor Gray
Write-Host "  - Try disabling Windows Defender Firewall temporarily for testing" -ForegroundColor Gray
Write-Host ""