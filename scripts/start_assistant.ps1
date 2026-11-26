<#
.SYNOPSIS
    Single-command startup script for India Post AI Shipment Assistant on Windows.
.DESCRIPTION
    Launches the multi-process service orchestrator supervising the web server,
    monitoring scheduler, and notification worker inside the project virtual environment.
#>
[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$NoMonitoring,
    [switch]$NoNotifications,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location -Path $ProjectRoot

$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment python not found at $VenvPython. Please run: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt"
}

$ArgsList = @("orchestrator.py", "--host", $HostAddress, "--port", $Port)
if ($NoMonitoring) { $ArgsList += "--no-monitoring" }
if ($NoNotifications) { $ArgsList += "--no-notifications" }
if ($CheckOnly) { $ArgsList += "--check-only" }

Write-Host "Starting India Post AI Shipment Assistant..." -ForegroundColor Cyan
& $VenvPython $ArgsList
