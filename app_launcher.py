"""
AnySong desktop launcher.

Chay toan bo he thong (2 engine ACE-Step, gemini_service, backend AnySong) nhu
cac tien trinh nen HOAN TOAN AN (khong co cua so cmd/PowerShell nao hien ra),
roi mo giao dien AnySong (http://127.0.0.1:8877) trong 1 cua so pywebview that -
tuc la trang web hien tai TRO THANH 1 app desktop, khong phai mo trong trinh
duyet voi thanh dia chi/tab nhu truoc.

Dong cua so app la tat het cac tien trinh nen (engine, gemini_service, backend).

Chay: backend\\.venv\\Scripts\\pythonw.exe app_launcher.py (run.bat da lam san).
"""

import atexit
import ctypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE_DIR = ROOT / "engine" / "ACE-Step-1.5"
BACKEND_DIR = ROOT / "backend"
GEMINI_DIR = ROOT / "gemini_service"
LOG_DIR = ROOT / "logs"

BACKEND_URL = "http://127.0.0.1:8877"
CREATE_NO_WINDOW = 0x08000000

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_procs: list[subprocess.Popen] = []
_log_files: list = []


def _spawn(args, cwd=None, env=None, log_name: str | None = None):
    """Chay 1 tien trinh nen, khong co cua so console nao ca (CREATE_NO_WINDOW) -
    ap dung cho ca process nay LAN moi tien trinh con no tu tao ra.

    stdout/stderr duoc ghi vao logs/<log_name>.log (ghi de moi lan chay) thay vi
    xa vao DEVNULL - truoc day mat het dau vet khi 1 engine chet/treo giua chung,
    ko co cach nao xac dinh la crash that hay chi la job chay lau."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    log_handle = None
    if log_name:
        LOG_DIR.mkdir(exist_ok=True)
        log_handle = open(LOG_DIR / f"{log_name}.log", "w", encoding="utf-8", errors="replace")
        _log_files.append(log_handle)
    proc = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        creationflags=CREATE_NO_WINDOW,
        stdout=log_handle if log_handle else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if log_handle else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    _procs.append(proc)
    return proc


def _kill_all():
    """taskkill /T (ca cay tien trinh con) cho tung process da spawn - Popen.terminate()
    don thuan CHI kill dung 1 process do, khong kill con chau (vd 'uv run' spawn ra 1
    process python engine rieng, hay pythonw.exe spawn ra ca dan msedgewebview2.exe) -
    bai hoc rut ra khi debug gemini_service: sot lai tien trinh gay treo lan sau."""
    for proc in _procs:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    for f in _log_files:
        try:
            f.close()
        except Exception:
            pass


atexit.register(_kill_all)


def _wait_healthy(url: str, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def _start_backend_services():
    # 2 engine ACE-Step rieng biet, moi cai chi load dung 1 model suot doi (xem
    # scripts/start.ps1 hoac README.md de biet ly do). Models lazy-load o lan
    # request dau, nen khong can cho engine "san sang" truoc khi mo UI.
    _spawn(
        ["uv", "run", "acestep-api", "--host", "127.0.0.1", "--port", "8001"],
        cwd=ENGINE_DIR,
        # ACESTEP_INIT_LLM left unset defaults to "auto", which GPU-tier-detects to
        # true on this 16GB card and eager-loads the 5Hz LM on the FIRST request
        # regardless of that request's own "thinking" flag (confirmed by reading
        # acestep/api/startup_llm_init.py - it's an engine-startup env var, not a
        # per-request switch). On Windows this LM has no viable backend at all right
        # now: no Triton -> ~38min for a few dozen metadata tokens (plain PyTorch
        # fallback); Triton installed (triton-windows) -> nano-vllm's paged-attention
        # kernels hang outright during init instead. Force it off here since
        # backend/app.py never sends thinking=True anymore anyway - this just stops
        # the engine from eager-loading (and risking hanging on) an LM nothing uses.
        #
        # ACESTEP_OFFLOAD_TO_CPU: this card is a real 16GB GPU that the driver reports
        # as 15.99GB, tripping the engine's "< 16GB -> auto-enable CPU offload"
        # threshold by a hair. With the LM now disabled there's no VRAM pressure left
        # to justify it, and offload turned out to be the actual cause of "generation
        # failed" on a real full-length song: a 10s test clip was fine (1.47s), but a
        # real 259s song stalled for the full internal 600s watchdog and errored out
        # with zero per-step progress logged the whole time. Verified fix directly:
        # same 259s job with offload forced off completed in ~4.4s diffusion time
        # (offload_time_cost: 0.0 in the log). Force it off.
        env={
            "ACESTEP_CONFIG_PATH": "acestep-v15-xl-turbo",
            "ACESTEP_INIT_LLM": "false",
            "ACESTEP_OFFLOAD_TO_CPU": "false",
        },
        log_name="engine_turbo",
    )
    # engine_base (port 8002) is NOT started here - backend/app.py spawns it itself,
    # on demand, only for the few seconds a melody_lock job's Extract phase needs it,
    # then kills it before the instrumental phase runs on turbo. Base's DiT model stays
    # resident in VRAM for as long as its process lives ("Keeping main model on cuda
    # (persistent)") - if both engines ran always-on together, this 16GB card can't fit
    # base's ~6GB + turbo's ~10-12GB at once: either it OOMs (turbo with offload off) or
    # it's unusably slow (turbo with offload on has to swap its ~9.7GB of weights over
    # PCIe every step - 160s+ and still not done for a real song, vs 4.4s alone). Only
    # spawning base for the brief Extract window keeps turbo's instrumental phase both
    # fast (no offload needed - it's the only model loaded) AND full quality (still the
    # xl-turbo model, not downgraded to base's 32-step tier just to dodge VRAM contention
    # - that "fixed" the crash but made the music noticeably worse). See
    # backend/app.py's _ensure_engine_base_running / _run_melody_lock_job.

    # gemini_service la tuy chon - bo qua neu chua setup (scripts/setup_gemini_service.ps1).
    gemini_python = GEMINI_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if gemini_python.exists():
        _spawn([str(gemini_python), "service.py"], cwd=GEMINI_DIR, log_name="gemini_service")

    backend_python = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    _spawn(
        [str(backend_python), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8877"],
        cwd=BACKEND_DIR,
        log_name="backend",
    )


def main():
    if not (BACKEND_DIR / ".venv" / "Scripts" / "python.exe").exists():
        ctypes.windll.user32.MessageBoxW(
            0, "Chua setup xong AnySong - chay run.bat truoc (no se tu cai dat).",
            "AnySong", 0x10,
        )
        sys.exit(1)

    _start_backend_services()

    if not _wait_healthy(f"{BACKEND_URL}/api/health"):
        ctypes.windll.user32.MessageBoxW(
            0, "Backend AnySong khong khoi dong duoc trong thoi gian cho. "
               "Kiem tra bang cach chay scripts\\start.ps1 truc tiep de xem loi chi tiet.",
            "AnySong", 0x10,
        )
        _kill_all()
        sys.exit(1)

    import webview  # noqa: E402 (import sau khi chac chan venv co pywebview)

    webview.create_window(
        "AnySong", BACKEND_URL, width=1440, height=920, min_size=(1000, 650),
    )
    webview.start()
    _kill_all()


if __name__ == "__main__":
    main()
