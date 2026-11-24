<#
.SYNOPSIS
  Let the CourseFlow VM use Ollama (Llama 3.2 3B) running on this Windows PC.

.DESCRIPTION
  1. Makes Ollama listen on the network (user variable OLLAMA_HOST=0.0.0.0:<Port>).
  2. Adds a Windows Firewall rule that allows TCP <Port> only from the VM subnet.
  3. Restarts Ollama so the setting takes effect, then pulls the model.
  4. Prints the LLAMA_BASE_URL line to paste into the VM's .env file.

  Ollama has no login of its own, so the firewall rule only admits the VM subnet.

.EXAMPLE
  # Run in PowerShell opened with "Run as administrator" (needed for the firewall rule):
  powershell -ExecutionPolicy Bypass -File .\setup-ollama.ps1
  powershell -ExecutionPolicy Bypass -File .\setup-ollama.ps1 -VmSubnet 192.168.169.0/24
#>
param(
  [string]$VmSubnet = "192.168.169.0/24",
  [string]$Model = "llama3.2:3b",
  [int]$Port = 11434
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Open PowerShell with 'Run as administrator' and run this script again (the firewall rule needs it)."
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  throw "Ollama is not installed or not on PATH. Install it from https://ollama.com/download/windows first."
}

Write-Host "1/4 Ollama will listen on 0.0.0.0:$Port (user environment variable OLLAMA_HOST)."
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:$Port", "User")

Write-Host "2/4 Allowing inbound TCP $Port from $VmSubnet only."
$ruleName = "CourseFlow Ollama $Port"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
# VMware's virtual adapters are often classed as Public networks, so the rule applies to every
# profile and relies on the remote-address limit instead.
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP `
  -LocalPort $Port -RemoteAddress $VmSubnet -Profile Any | Out-Null

Write-Host "3/4 Restarting Ollama."
Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$env:OLLAMA_HOST = "0.0.0.0:$Port"
$app = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
if (Test-Path $app) {
  Start-Process -FilePath $app
} else {
  Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
}
$deadline = (Get-Date).AddSeconds(60)
$version = $null
while (-not $version -and (Get-Date) -lt $deadline) {
  try { $version = (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/version" -TimeoutSec 3).version }
  catch { Start-Sleep -Seconds 2 }
}
if (-not $version) { throw "Ollama did not start on port $Port. Start the Ollama app manually and rerun." }
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not ($listeners | Where-Object { $_.LocalAddress -in @("0.0.0.0", "::") })) {
  Write-Warning "Ollama still listens on 127.0.0.1 only. Quit Ollama from the system tray, start it again, and rerun."
}
Write-Host "    Ollama $version is running."

Write-Host "4/4 Pulling $Model (about 2 GB the first time)."
$env:OLLAMA_HOST = "127.0.0.1:$Port"   # the CLI talks to the local server
& ollama pull $Model
if ($LASTEXITCODE -ne 0) { throw "ollama pull $Model failed." }

$prefix = ($VmSubnet -split "/")[0] -replace "\.\d+$", "."
$addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress.StartsWith($prefix) -or $_.InterfaceAlias -match "VMnet8" } |
  Select-Object -ExpandProperty IPAddress -Unique
Write-Host ""
if ($addresses) {
  Write-Host "Paste this line into the VM's .env file (Course_Registration_SRS/.env):"
  foreach ($address in $addresses) { Write-Host "  LLAMA_BASE_URL=http://${address}:$Port" }
} else {
  Write-Warning "No Windows address in $VmSubnet was found. Run ipconfig and use the 'VMware Network Adapter VMnet8' IPv4 address."
}
Write-Host "Then, on the VM: ./scripts/check-ollama.sh http://<that address>:$Port"
