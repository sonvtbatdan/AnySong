# Sets up the AnySong backend's own (lightweight) Python virtual environment.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$venvDir = Join-Path $backendDir ".venv"

if (-not (Test-Path $venvDir)) {
    Write-Host "Creating backend venv..."
    python -m venv $venvDir
}

& (Join-Path $venvDir "Scripts\pip.exe") install -q -r (Join-Path $backendDir "requirements.txt")
Write-Host "Backend setup complete."
