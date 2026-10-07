# Sets up gemini_service's Python virtual environment (pywebview + Qt + Flask).
# Optional: only needed for the "Tự động nhờ Gemini" auto-fill feature in the UI.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$geminiDir = Join-Path $root "gemini_service"
$venvDir = Join-Path $geminiDir ".venv"

if (-not (Test-Path $venvDir)) {
    Write-Host "Creating gemini_service venv..."
    python -m venv $venvDir
}

& (Join-Path $venvDir "Scripts\pip.exe") install -q -r (Join-Path $geminiDir "requirements.txt")
Write-Host "gemini_service setup complete."
Write-Host "Run scripts\start.ps1 to launch it - a real Gemini window will appear on first run, log into your Google account there once."
