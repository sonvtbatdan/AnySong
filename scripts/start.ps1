# Starts the ACE-Step engine API server (background window) + the AnySong backend, then opens the browser.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$engineDir = Join-Path $root "engine\ACE-Step-1.5"
$backendDir = Join-Path $root "backend"
$geminiDir = Join-Path $root "gemini_service"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"
$geminiVenvPython = Join-Path $geminiDir ".venv\Scripts\python.exe"

if (-not (Test-Path $engineDir)) {
    Write-Host "Engine not found. Run scripts\setup_engine.ps1 first."
    exit 1
}
if (-not (Test-Path $venvPython)) {
    Write-Host "Backend venv not found. Run scripts\setup_backend.ps1 first."
    exit 1
}

# Gemini service (gemini_service/) is optional - "Tự động nhờ Gemini" in the UI just fails
# gracefully with a clear error if it's not running. Skip it silently if not set up yet.
if (Test-Path $geminiVenvPython) {
    Write-Host "Starting Gemini automation service (new window, port 8004)..."
    Write-Host "  First run only: a real Gemini window will appear - log into your Google account there once."
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$geminiDir'; & '$geminiVenvPython' service.py"
} else {
    Write-Host "gemini_service venv not found - skipping (run scripts\setup_gemini_service.ps1 to enable 'Tự động nhờ Gemini')."
}

# Two SEPARATE engine processes, each pinned to exactly one model for its whole lifetime:
# loading a second model checkpoint into an already-running process (a second
# ACESTEP_CONFIG_PATH2 slot, or a runtime /v1/init swap) reproducibly crashed the process
# on this Windows setup (silent death right after the second model's "Attempting to load
# model..." log line). Two single-model processes sidestep that entirely.
#   - port 8001: acestep-v15-xl-turbo - fast single-shot "cover" generation, LM DISABLED.
#   - port 8002: acestep-v15-base    - the only tier with Extract/Complete support, used
#     for the melody-lock pipeline's Extract step. LM disabled here - Extract always skips
#     it, so loading it would only add VRAM/startup overhead for no benefit. Needs offload
#     on <20GB VRAM (auto-enabled below that threshold) and more denoise steps than turbo.
#
# LM used to crash on load on this machine (Windows, no Triton/flash-attn -> PyTorch
# fallback backend) - that turned out to be the system pagefile being capped at 4GB despite
# 61GB RAM, not the missing Triton/flash-attn per se. Fixed by switching Windows to
# auto-manage the pagefile + a reboot. BUT the LM itself later turned out unusable on
# Windows regardless of that fix: with no Triton, ACE-Step's LM falls back to a plain
# PyTorch decode loop that took ~38min just for the few dozen metadata tokens in Phase 1
# on a real song (confirmed via engine log timestamps). Installing triton-windows does NOT
# fix this - it unlocks vLLM's *preflight check*, but nano-vllm's actual paged-attention
# kernels then hang indefinitely during init on Windows (verified: process alive, near-zero
# CPU progress, 11+ min with zero log output). ACESTEP_INIT_LLM defaults to "auto", which
# GPU-tier-detects to true on 16GB+ cards and eager-loads the LM on the very first request
# REGARDLESS of that request's own "thinking" flag - so backend/app.py setting
# thinking=False in its payload is not enough on its own; the LM must be disabled at the
# engine level too. Re-enable only once ACE-Step ships a working Windows LM backend.
#
# ACESTEP_OFFLOAD_TO_CPU='false': this card is a real 16GB GPU that the driver reports as
# 15.99GB, tripping the engine's "< 16GB -> auto-enable CPU offload" threshold by a hair.
# With the LM disabled there's no VRAM pressure left to justify it, and offload turned out
# to be the actual cause of a separate bug ("generation failed" after ~10min): a short test
# clip was fine, but a real full-length song (~259s) stalled for the full internal 600s
# watchdog with zero per-step progress logged. Verified fix directly: same job with offload
# forced off completed in ~4.4s diffusion time instead.
Write-Host "Starting ACE-Step engine - xl-turbo (new window, port 8001)..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$engineDir'; `$env:ACESTEP_INIT_LLM='false'; `$env:ACESTEP_OFFLOAD_TO_CPU='false'; `$env:ACESTEP_CONFIG_PATH='acestep-v15-xl-turbo'; uv run acestep-api --host 127.0.0.1 --port 8001"

Write-Host "Starting ACE-Step engine - base (new window, port 8002)..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$engineDir'; `$env:ACESTEP_INIT_LLM='false'; `$env:ACESTEP_CONFIG_PATH='acestep-v15-base'; uv run acestep-api --host 127.0.0.1 --port 8002"

Write-Host "Waiting for both engines to become healthy (this can take a while on first run while models download)..."
function Wait-EngineHealthy($port) {
    for ($i = 0; $i -lt 180; $i++) {
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3
            if ($resp.data.status -eq "ok") { return $true }
        } catch {}
        Start-Sleep -Seconds 5
    }
    return $false
}
if (-not (Wait-EngineHealthy 8001)) {
    Write-Host "xl-turbo engine (8001) did not report healthy in time - check its window for errors. Continuing anyway..."
}
if (-not (Wait-EngineHealthy 8002)) {
    Write-Host "base engine (8002) did not report healthy in time - check its window for errors. Continuing anyway..."
}

Write-Host "Starting AnySong backend on http://127.0.0.1:8877 ..."
Start-Process "http://127.0.0.1:8877"
& $venvPython -m uvicorn app:app --host 127.0.0.1 --port 8877 --app-dir $backendDir
