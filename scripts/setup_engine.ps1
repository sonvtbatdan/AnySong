# Sets up the ACE-Step 1.5 engine (clone + Python deps via uv).
# Model weights are NOT downloaded here - they auto-download on first generation request.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$engineDir = Join-Path $root "engine\ACE-Step-1.5"

if (-not (Test-Path $engineDir)) {
    Write-Host "Cloning ACE-Step 1.5..."
    New-Item -ItemType Directory -Force -Path (Join-Path $root "engine") | Out-Null
    git clone https://github.com/ace-step/ACE-Step-1.5.git $engineDir
} else {
    Write-Host "Engine already present at $engineDir"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Install it first: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    exit 1
}

Push-Location $engineDir
Write-Host "Installing engine dependencies (uv sync) - this downloads several GB of packages (torch/vllm/etc) on first run..."
uv sync
Pop-Location

Write-Host "Engine setup complete."
