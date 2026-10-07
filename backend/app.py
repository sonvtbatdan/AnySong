"""
AnySong backend.

Thin orchestrator: takes an uploaded song + a style prompt from the frontend,
forwards it to a running ACE-Step 1.5 API server, polls until finished, and
hands the resulting audio back to the browser. Generation runs as a background
asyncio task tracked in-memory (`_jobs`) so the frontend can poll live progress
instead of blocking on one long request.

Two generation modes:
- "cover" (default): single `task_type=cover` call on the fast XL-turbo model.
  This regenerates EVERYTHING (melody, instrumentation, and vocals) via diffusion
  conditioned on the source audio - the singer's voice is NOT preserved, since
  ACE-Step is a music generator, not a voice-cloning/conversion system.
- "melody_lock": a voice-preserving pipeline, closer to real audio-preservation than
  AI regeneration but not perfectly lossless:
    1. Extract isolates the chosen track (usually vocals) from the source with the
       base model. This is itself a diffusion step - the model regenerates just that
       track, conditioned on the full mix - not a lossless split like Demucs, so it's
       run at the top of the base model's quality range (64 steps + ADG) since the
       whole "preserve the singer" guarantee depends on its fidelity.
    2. A brand new instrumental-only backing track is generated from scratch with
       the fast XL-turbo model (task_type=text2music, lyrics="[Instrumental]"),
       matched to the extracted track's duration and detected tempo.
    3. ffmpeg mixes the two into one file.
  An earlier version used `task_type=complete` to regenerate the backing around the
  extracted track, but Complete is itself a diffusion step over the WHOLE mix - it
  resynthesizes audio rather than preserving the given track, so the vocal still came
  out sounding like a different singer. Isolating first and mixing separately keeps
  the singer's voice far closer to the original than any single-pass regeneration.

The actual music models live entirely in the ACE-Step engine (engine/ACE-Step-1.5).
This service has no heavyweight ML dependency of its own except a local Whisper
model used only as a fallback lyrics source (see /api/transcribe), and calls the
system `ffmpeg`/`ffprobe` binaries for audio mixing/duration probing.
"""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel

# Two separate engine PROCESSES, each pinned to exactly one DiT model for its whole
# lifetime. Loading a second model checkpoint into an already-running engine process
# (whether via a second ACESTEP_CONFIG_PATH2 slot at startup, or a runtime /v1/init
# swap) reproducibly crashed the process on this Windows setup (silent death right
# after the second model's "Attempting to load model with attention implementation:
# sdpa" log line - likely the same missing-Triton/flash-attn instability seen with the
# LM). Routing to a separate always-single-model process sidesteps that entirely.
ENGINE_URL_TURBO = os.environ.get("ANYSONG_ENGINE_URL_TURBO", "http://127.0.0.1:8001")  # acestep-v15-xl-turbo
ENGINE_URL_BASE = os.environ.get("ANYSONG_ENGINE_URL_BASE", "http://127.0.0.1:8002")  # acestep-v15-base
GEMINI_SERVICE_URL = os.environ.get("ANYSONG_GEMINI_SERVICE_URL", "http://127.0.0.1:8004")  # gemini_service/
OUTPUT_DIR = Path(os.environ.get("ANYSONG_OUTPUT_DIR", Path(__file__).resolve().parent.parent / "outputs"))
HISTORY_PATH = OUTPUT_DIR / "history.json"
SAVED_STYLES_PATH = OUTPUT_DIR / "saved_styles.json"
POLL_INTERVAL_SECONDS = 2.0
# melody_lock's instrumental phase no longer sets thinking=True (see that request dict) -
# on Windows the LM has no viable backend at all right now (plain PyTorch fallback takes
# ~38min just for a few dozen metadata tokens; vLLM via triton-windows hangs outright
# during init - see the comment there). Without the LM in the loop, pure diffusion for a
# batch of full-length songs should stay well under 15min; kept above the old 600s anyway
# since a real hang is now visible via logs/*.log (see app_launcher.py) rather than only
# inferable from this firing.
GENERATION_TIMEOUT_SECONDS = 900.0
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

# The base model needs more denoising steps than turbo to produce clean audio.
BASE_MODEL_INFERENCE_STEPS = 32
# "instrumental" isn't one of ACE-Step's own trained-on stem classes (its real vocabulary
# is woodwinds/brass/fx/synth/strings/percussion/keyboard/guitar/bass/drums/backing_vocals/
# vocals, see engine/.../acestep/constants.py TRACK_NAMES) - there's no single built-in
# "everything except vocals" extraction target. Passing "instrumental" as track_name works
# mechanically (the instruction template is generic text, "Extract the INSTRUMENTAL track
# from the audio:") and returns a plausible, DIFFERENT-sounding result from "vocals" on the
# same source (verified: distinct audio, not an error or an echo of the vocal extraction) -
# but since the model was never specifically trained on that label, its faithfulness is
# unverified beyond that spot check. Both tracks are always extracted and exposed for
# listening (see vocal_track_url/instrumental_extract_url in the job runners) specifically
# so the user can judge for themselves whether it's actually isolating the backing mix well
# before picking it as the one fed into melody_lock/melody_complete.
MELODY_TRACKS = ["vocals", "instrumental"]

# engine_base is spawned on demand (see _ensure_engine_base_running) rather than run
# as an always-on service like engine_turbo - see the comment in app_launcher.py's
# _start_backend_services for why (VRAM won't fit both models resident at once on a
# 16GB card without either OOMing or making turbo unusably slow).
ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine" / "ACE-Step-1.5"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Extract (isolating a track, e.g. vocals) is a 64-step base-model diffusion pass - not
# instant, and its result only depends on the exact source audio bytes + which track was
# requested. Re-running it every time the user regenerates the SAME uploaded song (trying
# a different style/method/variation count) wastes that time for no benefit, so its output
# is cached to disk keyed by a hash of (song bytes, track name) - see
# _get_cached_extract/_save_extract_cache, used by both melody job runners.
EXTRACT_CACHE_DIR = Path(os.environ.get("ANYSONG_OUTPUT_DIR", Path(__file__).resolve().parent.parent / "outputs")) / ".extract_cache"

WHISPER_MODEL_SIZE = os.environ.get("ANYSONG_WHISPER_MODEL", "small")
# CPU by default: avoids a separate cuBLAS/cuDNN dependency for ctranslate2 (distinct from
# the CUDA runtime PyTorch/ACE-Step already use) that failed to install reliably here.
# "small" on CPU is still fine for one-off transcription of a single uploaded song.
WHISPER_DEVICE = os.environ.get("ANYSONG_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("ANYSONG_WHISPER_COMPUTE_TYPE", "float16" if WHISPER_DEVICE == "cuda" else "int8")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AnySong")

_whisper_model: WhisperModel | None = None
_whisper_lock = threading.Lock()
_history_lock = threading.Lock()
_saved_styles_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Both engine processes share this GPU. They each auto-offload models between requests,
# but running two heavy jobs at once could still thrash VRAM - this keeps generation
# strictly one-at-a-time across both processes (this is a single-user local app, so that
# costs nothing in practice).
_engine_lock = asyncio.Lock()


def _get_whisper_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
    return _whisper_model


def _read_history() -> list:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _append_history(entry: dict) -> None:
    with _history_lock:
        history = _read_history()
        history.insert(0, entry)
        history = history[:100]
        HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _delete_history_entry(entry_id: str) -> bool:
    """Removes the history entry and every file on disk it owns - all variations plus the
    melody-lock debug vocal/instrumental tracks, from the entry's own `files` list (glob-by
    -prefix isn't reliable once there are multiple numbered variation files to account for)."""
    with _history_lock:
        history = _read_history()
        entry = next((e for e in history if e.get("id") == entry_id), None)
        if entry is None:
            return False
        remaining = [e for e in history if e.get("id") != entry_id]
        HISTORY_PATH.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")

    for name in entry.get("files", [entry_id]):
        (OUTPUT_DIR / name).unlink(missing_ok=True)
    return True


def _read_saved_styles() -> list:
    if not SAVED_STYLES_PATH.exists():
        return []
    try:
        return json.loads(SAVED_STYLES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_style(text: str) -> None:
    """Auto-called whenever Gemini successfully writes a style caption - lets the user
    browse/reuse past Gemini-optimized styles later via the "Saved style" picker instead of
    re-asking Gemini or retyping. Deduped by exact text (re-picking an already-saved style
    just bumps it to the front rather than creating a near-identical duplicate entry)."""
    text = text.strip()
    if not text:
        return
    with _saved_styles_lock:
        styles = _read_saved_styles()
        styles = [s for s in styles if s.get("text") != text]
        styles.insert(0, {"id": uuid.uuid4().hex, "text": text, "created_at": time.time()})
        styles = styles[:100]
        SAVED_STYLES_PATH.write_text(json.dumps(styles, ensure_ascii=False, indent=2), encoding="utf-8")


def _delete_saved_style(style_id: str) -> bool:
    with _saved_styles_lock:
        styles = _read_saved_styles()
        remaining = [s for s in styles if s.get("id") != style_id]
        if len(remaining) == len(styles):
            return False
        SAVED_STYLES_PATH.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


@app.get("/api/health")
async def health():
    async def _check(url: str):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url}/health")
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            return {"status": "unreachable", "error": str(exc)}

    turbo, base = await asyncio.gather(_check(ENGINE_URL_TURBO), _check(ENGINE_URL_BASE))
    return {"anysong": "ok", "engine_turbo": turbo, "engine_base": base}


@app.get("/api/history")
async def history():
    return JSONResponse(_read_history())


@app.delete("/api/history/{entry_id}")
async def delete_history_entry(entry_id: str):
    if not _delete_history_entry(entry_id):
        raise HTTPException(status_code=404, detail="Unknown history entry")
    return JSONResponse({"deleted": entry_id})


@app.get("/api/styles")
async def list_saved_styles():
    return JSONResponse(_read_saved_styles())


@app.delete("/api/styles/{style_id}")
async def delete_saved_style(style_id: str):
    if not _delete_saved_style(style_id):
        raise HTTPException(status_code=404, detail="Unknown saved style")
    return JSONResponse({"deleted": style_id})


@app.get("/api/lyrics/search")
async def search_lyrics(title: str = "", artist: str = ""):
    """Look up real, exact lyrics by track/artist name via LRCLIB's free public API.

    This is the accurate path: it returns actual published lyrics text (like a karaoke
    app would), instead of guessing from the audio. LLM chat tools (Gemini/ChatGPT) tend
    to refuse to reproduce copyrighted lyrics verbatim, and audio transcription (Whisper)
    is only an approximation - this sidesteps both problems when the song is identifiable
    by title/artist. Falls back to /api/transcribe when there's no match (obscure tracks,
    typos, etc).
    """
    if not title.strip():
        raise HTTPException(status_code=400, detail="title is required")

    params = {"track_name": title.strip()}
    if artist.strip():
        params["artist_name"] = artist.strip()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(LRCLIB_SEARCH_URL, params=params, headers={"User-Agent": "AnySong/1.0"})
            resp.raise_for_status()
            results = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Lyrics search failed: {exc}")

    candidates = [
        {
            "track_name": r.get("trackName"),
            "artist_name": r.get("artistName"),
            "album_name": r.get("albumName"),
            "duration": r.get("duration"),
            "instrumental": r.get("instrumental", False),
            "lyrics": r.get("plainLyrics") or "",
        }
        for r in results
        if r.get("plainLyrics") or r.get("instrumental")
    ]
    return JSONResponse(candidates[:5])


_GEMINI_STYLE_RE = re.compile(r"=+\s*STYLE\s*=+\s*(.*?)\s*=+\s*END\s*=*", re.DOTALL | re.IGNORECASE)


def _parse_gemini_enhance_response(text: str) -> str:
    style_match = _GEMINI_STYLE_RE.search(text)
    return style_match.group(1).strip() if style_match else ""


def _auto_tag_lyrics_structure(lyrics: str) -> str:
    """Inserts [Verse]/[Chorus] structure tags WITHOUT any AI call, by detecting exact-
    repeat paragraphs - a chorus almost always repeats verbatim, so any paragraph that
    shows up 2+ times gets tagged [Chorus], everything else becomes [Verse N] in order.
    Nothing here is sent anywhere, so it can never hit a copyright refusal the way asking
    an LLM to touch real song lyrics can. Less nuanced than a human (or a cooperative AI)
    - e.g. it can't tell a real Bridge from a one-off Verse - but 100% reliable."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", lyrics.strip()) if b.strip()]
    if not blocks:
        return lyrics

    def normalize(block: str) -> str:
        return re.sub(r"\s+", " ", block.lower()).strip()

    counts = Counter(normalize(b) for b in blocks)
    verse_num = 0
    tagged_blocks = []
    for block in blocks:
        if counts[normalize(block)] >= 2:
            tag = "[Chorus]"
        else:
            verse_num += 1
            tag = f"[Verse {verse_num}]"
        tagged_blocks.append(f"{tag}\n{block}")

    return "[Intro]\n\n" + "\n\n".join(tagged_blocks) + "\n\n[Outro - fade out]"


@app.post("/api/lyrics/auto_tag")
async def lyrics_auto_tag(lyrics: str = Form(...)):
    """Standalone, AI-free structure-tag insertion - see _auto_tag_lyrics_structure."""
    if not lyrics.strip():
        raise HTTPException(status_code=400, detail="lyrics is required")
    return JSONResponse({"lyrics": _auto_tag_lyrics_structure(lyrics.strip())})


@app.post("/api/gemini/enhance")
async def gemini_enhance(style_prompt: str = Form("")):
    """Automated version of the old copy-paste-to-Gemini workflow: drives a real, hidden
    gemini.google.com window (gemini_service/, logged into the user's own Google account -
    see that folder's docstring) to turn a short style idea into a full ACE-Step-style
    caption with an energy arc.

    Used to also insert structure tags into the user's lyrics in the same round trip, but
    that was dropped: Gemini routinely refused or silently paraphrased real song lyrics on
    copyright grounds even for the transformative task of just inserting tags, which
    needed a whole separate refusal-detection/fallback path (_gemini_lyrics_looks_refused)
    to catch. Structure-tagging lyrics is available AI-free and 100% reliably via the
    "Tự thêm tag" button (/api/lyrics/auto_tag, _auto_tag_lyrics_structure) instead - this
    endpoint now only ever touches style_prompt."""
    seed = style_prompt.strip()

    idea = (
        f'Ý tưởng/từ khoá của người dùng: "{seed}" (kể cả khi đây là 1 khái niệm không thuộc '
        "thuật ngữ âm nhạc, vd tên game/phim/thương hiệu - hãy dịch nó sang không khí, nhạc cụ, "
        "mood âm nhạc tương ứng)."
        if seed
        else "Người dùng chưa có ý tưởng cụ thể - hãy tự sáng tạo 1 thể loại/không khí bất kỳ, "
        "càng độc đáo càng tốt (đừng chọn thể loại quá phổ biến)."
    )

    prompt = f"""Bạn đang giúp chuẩn bị 1 tham số đầu vào cho 1 model tạo nhạc AI (ACE-Step). Trả lời CHÍNH XÁC theo format sau, không thêm giải thích, không dùng markdown code fence:

===STYLE===
<1 đoạn "music caption" bằng tiếng Anh, 2-4 câu văn liền mạch, mô tả rõ thể loại/nhạc cụ/không khí/tiết tấu, LUÔN gợi ý 1 arc năng lượng theo thời gian (mở nhẹ, build dần, bùng nổ ở chorus/climax - không mô tả 1 trạng thái tĩnh xuyên suốt)>
===END===

{idea}"""

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            status_resp = await client.get(f"{GEMINI_SERVICE_URL}/status")
            status_resp.raise_for_status()
            gemini_status = status_resp.json()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"Không kết nối được gemini_service: {exc}")
    if not gemini_status.get("logged_in"):
        raise HTTPException(
            status_code=409,
            detail="Chưa đăng nhập Gemini trong gemini_service - mở cửa sổ Gemini (scripts\\start.ps1 đã tự chạy) và đăng nhập Google trước.",
        )

    async with httpx.AsyncClient(timeout=150.0) as client:
        try:
            resp = await client.post(f"{GEMINI_SERVICE_URL}/ask", json={"prompt": prompt, "timeout": 120})
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Gemini request failed: {exc}")

    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=f"Gemini error: {result.get('error')}")

    new_style = _parse_gemini_enhance_response(result.get("text", ""))
    if not new_style:
        raise HTTPException(status_code=502, detail="Không đọc được format trả lời từ Gemini - thử lại.")

    _save_style(new_style)
    return JSONResponse({"style_prompt": new_style or seed})


@app.post("/api/transcribe")
async def transcribe(song: UploadFile, language: str = Form("")):
    """Best-effort lyrics transcription for an uploaded song, via a local Whisper model.

    Fallback for when /api/lyrics/search finds no match. This is real ASR (speech-to-text),
    unlike ACE-Step's own LM-based "understand_music" - which predicts plausible lyrics from
    a coarse 5Hz audio-code representation and is not meant to be verbatim. Whisper wasn't
    trained specifically on singing, so quality varies with vocal clarity/mixing/language.
    """
    ext = Path(song.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}")

    song_bytes = await song.read()

    def _run_transcribe():
        model = _get_whisper_model()
        segments, info = model.transcribe(
            io.BytesIO(song_bytes),
            language=language or None,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, info.language

    try:
        lyrics_text, detected_language = await asyncio.to_thread(_run_transcribe)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}")

    return JSONResponse({"lyrics": lyrics_text, "detected_language": detected_language})


# --- Engine call helpers -----------------------------------------------------

async def _is_engine_healthy(client: httpx.AsyncClient, engine_url: str) -> bool:
    try:
        resp = await client.get(f"{engine_url}/health", timeout=3.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _ensure_engine_base_running(client: httpx.AsyncClient) -> subprocess.Popen:
    """Spawns engine_base fresh for the brief window a melody_lock job's Extract phase
    needs it. Always spawns our own instance (killing anything already bound to its port
    first) rather than reusing whatever happens to already answer /health - a real
    production failure showed why: a base process left over from an earlier, improperly-
    torn-down session answered the health check successfully (this function used to
    return None and trust it), then died mid-request with "Mất kết nối tới engine_base ...
    All connection attempts failed" - we had no way to know that process's actual history
    or remaining lifespan since we hadn't spawned it ourselves.

    Retries the whole spawn once on failure (including a brief "still healthy a moment
    later" stability check after the first success) - a later production failure showed
    that "the health endpoint responded 200 once" isn't quite the same guarantee: the
    process can still be crashing in the narrow window right after, before it's had a
    chance to handle a real /release_task request (real repro: a job failed with the same
    connection error after only ~2 status polls - a few seconds - suggesting base died
    almost immediately after passing its own health check). One retry turns a transient
    flake into a self-healed success instead of an immediate failure; a second, sturdier
    failure is genuinely worth surfacing to the user rather than retrying forever.

    Caller must kill the returned process via _kill_engine_base_process once done with
    base."""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            return await _spawn_engine_base_once(client)
        except Exception as exc:
            last_exc = exc
    raise last_exc


async def _spawn_engine_base_once(client: httpx.AsyncClient) -> subprocess.Popen:
    if sys.platform == "win32":
        # subprocess.run() is BLOCKING - this backend runs a single uvicorn event loop, so
        # calling it directly here would freeze the ENTIRE app (every other request, health
        # checks, everything) for as long as the WMI query underneath Get-CimInstance takes,
        # which is genuinely slow (multiple seconds, sometimes much more, enumerating every
        # process on the system). Confirmed live: this exact bug, on its first deployment,
        # made the whole app hang and need a manual restart. asyncio.to_thread runs the
        # blocking call on a worker thread instead, keeping the event loop free.
        await asyncio.to_thread(
            subprocess.run,
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'port 8002' } "
             "| ForEach-Object { taskkill /F /T /PID $_.ProcessId }"],
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
        # Give Windows a moment to actually release the port before we try to bind it.
        for _ in range(10):
            if not await _is_engine_healthy(client, ENGINE_URL_BASE):
                break
            await asyncio.sleep(0.5)

    LOG_DIR.mkdir(exist_ok=True)
    log_handle = open(LOG_DIR / "engine_base.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        ["uv", "run", "acestep-api", "--host", "127.0.0.1", "--port", "8002"],
        cwd=str(ENGINE_DIR),
        env={**os.environ, "ACESTEP_CONFIG_PATH": "acestep-v15-base", "ACESTEP_INIT_LLM": "false"},
        creationflags=CREATE_NO_WINDOW,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    log_handle.close()  # child inherited its own fd copy; safe to close ours

    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if await _is_engine_healthy(client, ENGINE_URL_BASE):
            # Stability check: confirm it's STILL healthy a moment later, not just healthy
            # for the single instant of this one request - see the retry docstring above
            # for the real failure this catches.
            await asyncio.sleep(1.0)
            if await _is_engine_healthy(client, ENGINE_URL_BASE):
                return proc
            await _kill_engine_base_process(proc)
            raise RuntimeError("engine_base passed its health check but then went unreachable a moment later - check logs/engine_base.log")
        if proc.poll() is not None:
            raise RuntimeError(f"engine_base process exited during startup (code {proc.returncode}) - check logs/engine_base.log")
        await asyncio.sleep(1.0)
    await _kill_engine_base_process(proc)
    raise RuntimeError("engine_base did not become healthy within 120s - check logs/engine_base.log")


async def _kill_engine_base_process(proc: subprocess.Popen) -> None:
    """Tree-kills a backend-spawned engine_base process to actually free its VRAM
    (there's no lighter-weight 'unload model' API in ACE-Step - see the comment in
    _run_melody_lock_job). Best-effort: a failure here shouldn't fail the whole job,
    since the audio we needed from it is already downloaded by this point.

    subprocess.run() is blocking - runs on a worker thread via asyncio.to_thread so it
    can't freeze the single uvicorn event loop this whole app runs on (see the matching
    comment in _ensure_engine_base_running, where this exact class of bug was confirmed
    live to hang the entire app, not just the one job)."""
    try:
        if sys.platform == "win32":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
    except Exception:
        pass


def _friendly_engine_connection_error(engine_url: str, exc: httpx.HTTPError) -> RuntimeError:
    """httpx.ConnectError's own str() is just "All connection attempts failed" - no engine
    URL, no hint of what was even being attempted. That's what surfaced to the user
    verbatim, cryptic and undiagnosable, when an engine went unreachable mid-job (crashed,
    was still starting, or the whole app was closed). Naming which engine and pointing at
    its log at least makes the NEXT occurrence self-diagnosing instead of a dead end."""
    engine_name = "engine_base (port 8002)" if ":8002" in engine_url else "engine_turbo (port 8001)" if ":8001" in engine_url else engine_url
    log_hint = "logs/engine_base.log" if ":8002" in engine_url else "logs/engine_turbo.log" if ":8001" in engine_url else "logs/*.log"
    return RuntimeError(f"Mất kết nối tới {engine_name} ({exc}) - engine có thể đã crash hoặc app đã bị đóng. Kiểm tra {log_hint}.")


async def _submit_engine_job(client: httpx.AsyncClient, engine_url: str, data: dict, file_field: str | None = None, filename: str = "", file_bytes: bytes = b"", content_type: str = "") -> str:
    files = {file_field: (filename, file_bytes, content_type or "application/octet-stream")} if file_field else None
    try:
        resp = await client.post(f"{engine_url}/release_task", data=data, files=files)
    except httpx.HTTPError as exc:
        raise _friendly_engine_connection_error(engine_url, exc) from exc
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 200:
        raise RuntimeError(f"ACE-Step engine rejected job: {payload.get('error')}")
    return payload["data"]["task_id"]


async def _poll_engine_job(client: httpx.AsyncClient, engine_url: str, task_id: str, on_progress) -> dict:
    deadline = time.monotonic() + GENERATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            query_resp = await client.post(f"{engine_url}/query_result", json={"task_id_list": [task_id]})
        except httpx.HTTPError as exc:
            raise _friendly_engine_connection_error(engine_url, exc) from exc
        query_resp.raise_for_status()
        entries = query_resp.json().get("data") or []
        if not entries:
            raise RuntimeError("ACE-Step engine returned no task status")
        entry = entries[0]
        status = entry.get("status")
        if status == 1:
            return entry
        if status == 2:
            # entry["result"] on a failure is just the same item shape as a success, with
            # no error text in it at all (verified live: {"file":"","wave":"","status":2,
            # "create_time":...,"env":"development","progress":0.0,"stage":"failed"}) -
            # dumping it raw just shows the user a wall of unrelated field names instead of
            # the actual problem. The real human-readable reason (e.g. "VRAM pre-flight
            # failed: Insufficient free VRAM...") is logged separately in
            # entry["progress_text"]; prefer that, falling back to the item dict only if
            # it's ever missing.
            progress_text = (entry.get("progress_text") or "").strip()
            if progress_text:
                # progress_text is a raw log line ("HH:MM:SS | LEVEL | [module] message") -
                # keep just the message part for a clean, copyable error.
                error_detail = progress_text.rsplit("|", 1)[-1].strip()
            else:
                raw_result = entry.get("result")
                error_detail = raw_result
                try:
                    parsed = json.loads(raw_result or "[]")
                    item = parsed[0] if parsed else {}
                    error_detail = item.get("error") or item.get("status_message") or raw_result
                except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
                    pass
            raise RuntimeError(f"Generation failed: {error_detail}")
        try:
            parsed = json.loads(entry.get("result") or "[]")
            item = parsed[0] if parsed else {}
            on_progress(item.get("progress", 0.0), item.get("stage", ""))
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError("Generation timed out")


async def _download_engine_audio(client: httpx.AsyncClient, engine_url: str, file_path: str) -> bytes:
    if file_path.startswith("/v1/audio"):
        audio_url = f"{engine_url}{file_path}"
    else:
        audio_url = f"{engine_url}/v1/audio?path={file_path}"
    try:
        resp = await client.get(audio_url, timeout=120.0)
    except httpx.HTTPError as exc:
        raise _friendly_engine_connection_error(engine_url, exc) from exc
    resp.raise_for_status()
    return resp.content


def _extract_cache_path(song_bytes: bytes, track: str) -> Path:
    key = hashlib.sha256(song_bytes + b"|" + track.encode()).hexdigest()
    return EXTRACT_CACHE_DIR / f"{key}.wav"


def _get_cached_extract(song_bytes: bytes, track: str) -> bytes | None:
    path = _extract_cache_path(song_bytes, track)
    return path.read_bytes() if path.exists() else None


def _save_extract_cache(song_bytes: bytes, track: str, extracted_bytes: bytes) -> None:
    EXTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _extract_cache_path(song_bytes, track).write_bytes(extracted_bytes)


async def _extract_both_tracks(
    client: httpx.AsyncClient, song_bytes: bytes, filename: str, content_type: str, on_stage,
) -> tuple[bytes, bytes, "subprocess.Popen | None"]:
    """Extracts (or reuses cached results for) BOTH the 'vocals' and 'instrumental' stems
    from the source song - always both, regardless of which single one the caller's
    pipeline will actually use, so the user can listen to and compare them side by side
    (see vocal_track_url/instrumental_extract_url in the job runners) before deciding which
    one actually isolates the backing mix well for melody_lock/melody_complete. Shared by
    both job runners rather than duplicated.

    Returns (vocal_bytes, instrumental_bytes, base_proc) - base_proc is the engine_base
    Popen this call spawned (caller must kill it once entirely done with base) or None if
    nothing needed spawning here (both tracks were already cached)."""
    vocal_bytes = _get_cached_extract(song_bytes, "vocals")
    instrumental_bytes = _get_cached_extract(song_bytes, "instrumental")
    if vocal_bytes is not None and instrumental_bytes is not None:
        on_stage("Dùng lại 2 track đã tách trước đó (cache)...")
        return vocal_bytes, instrumental_bytes, None

    async def _extract_one(track: str) -> bytes:
        on_stage(f"Đang tách track '{track}' từ bài gốc (chất lượng cao)...")
        extract_data = {
            "task_type": "extract",
            "track_name": track,
            "prompt": track,
            "audio_format": "wav",
            "inference_steps": 64,
            "use_adg": True,
            "guidance_scale": 7.0,
        }
        task_id = await _submit_engine_job(client, ENGINE_URL_BASE, extract_data, "src_audio", filename, song_bytes, content_type)

        def on_progress(progress, stage):
            on_stage(f"Đang tách track '{track}'... {stage}")

        result = await _poll_engine_job(client, ENGINE_URL_BASE, task_id, on_progress)
        first = json.loads(result["result"])[0]
        data = await _download_engine_audio(client, ENGINE_URL_BASE, first["file"])
        _save_extract_cache(song_bytes, track, data)
        return data

    # Retry the whole spawn+extract cycle once if the FIRST real request to base fails
    # to connect - _ensure_engine_base_running's own retry/stability-check only proves the
    # process is answering /health, which doesn't load any models; a crash during actual
    # model loading (which only starts on this first real /release_task) wouldn't be
    # caught by that check at all. Real repro: base passed its health check, then died
    # before finishing its first real job. Giving up after a single connection failure
    # here would surface that as a hard error even though a fresh respawn is very likely
    # to just work - proven live for the exact same class of flake in
    # _ensure_engine_base_running.
    last_exc: Exception | None = None
    for attempt in range(2):
        base_proc = await _ensure_engine_base_running(client)
        try:
            if vocal_bytes is None:
                vocal_bytes = await _extract_one("vocals")
            if instrumental_bytes is None:
                instrumental_bytes = await _extract_one("instrumental")
            return vocal_bytes, instrumental_bytes, base_proc
        except RuntimeError as exc:
            last_exc = exc
            await _kill_engine_base_process(base_proc)
            if "Mất kết nối" not in str(exc):
                raise  # a real generation failure (not a connection drop) - don't retry
    raise last_exc


# --- Audio helpers (ffmpeg/ffprobe) -------------------------------------------

async def _get_audio_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='replace')}")
    return float(stdout.decode().strip())


async def _mix_audio(vocal_path: str, instrumental_path: str, out_path: str) -> None:
    """Overlay the (untouched) vocal track with the freshly generated instrumental,
    trimmed/padded to the vocal's exact length, with a limiter to avoid clipping."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-i", vocal_path, "-i", instrumental_path,
        "-filter_complex", "[0:a]volume=1.0[v];[1:a]volume=0.85[i];[v][i]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95",
        out_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mix failed: {stderr.decode(errors='replace')}")


def _build_instrumental_structure_lyrics(duration: float) -> str:
    """A single flat `[Instrumental]` tag gives the model no timeline to follow, which is
    why generated tracks tend to run flat start-to-end with no intro/build/drop arc -
    per ACE-Step's own docs, Lyrics is the music's "temporal script" and structure tags
    ([Intro], [Build], [Chorus], [Outro]...) are "the most powerful tool" for shaping how
    a track unfolds over time; a bare [Instrumental] carries none of that. This builds an
    instrumental section blueprint scaled to duration instead."""
    if duration < 30:
        return "[Intro]\n\n[Climax - powerful]"
    if duration < 90:
        return "[Intro]\n\n[Build - rising energy]\n\n[Climax - powerful]\n\n[Outro - fade out]"
    return (
        "[Intro - ambient]\n\n[Build - rising energy]\n\n[Climax - powerful]\n\n"
        "[Breakdown]\n\n[Climax - powerful]\n\n[Outro - fade out]"
    )


def _detect_bpm_sync(path: str) -> float | None:
    """Best-effort tempo detection on the ORIGINAL source mix (not the isolated vocal -
    drums/bass give librosa's beat tracker much more to work with). Used so the freshly
    generated instrumental (which has no other connection to the source) at least lands on
    the same tempo instead of being rhythmically unrelated to the preserved vocal."""
    import librosa

    try:
        y, sr = librosa.load(path, sr=None, mono=True, duration=60.0)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo.item() if hasattr(tempo, "item") else tempo)
        return bpm if 30.0 <= bpm <= 300.0 else None
    except Exception:
        return None


# --- Background job runners ---------------------------------------------------

# task_type=cover conditions on the FULL source audio (unlike text2music/extract/complete,
# which don't take a source track or take a much shorter one) - its real peak VRAM need on
# a long real song runs far higher than the engine's own preflight check accounts for
# (that check only runs right before the diffusion loop, using a simple 0.5GB/batch
# duration-linear estimate + a bare 0.5GB margin). Confirmed live, twice, on this 16GB
# card: a 259.13s/batch=1 cover request passed that engine-side check both times, then sat
# at 100% GPU making literally zero progress for the full 600s internal timeout - VRAM
# pinned near the 16.4GB ceiling (15.9GB and 16.3GB respectively), the signature of the
# CUDA allocator thrashing near-full memory rather than a clean OOM error.
#
# A live free-VRAM-based pre-check (read via nvidia-smi before submitting) was tried first
# but is fundamentally unreliable here: models lazy-load on the FIRST request, so a
# pre-check that runs before turbo has ever loaded sees near the FULL card free (~14.7GB
# observed) and passes - the exact scenario that hung both times above, since a real user's
# first generation after opening the app is exactly this case. There's no cheap way to
# know from the backend whether the engine's model is already resident without adding a
# separate round-trip, and even a generous safety margin computed against a live reading
# doesn't help when the reading itself is the wrong number.
#
# So: gate on duration alone instead, sidestepping the live-VRAM-reading problem entirely.
# 90s is a deliberately conservative cutoff - comfortably under the failure point observed
# at 259s, while still covering short clips/snippets. Longer real songs should use
# "Giữ nguyên melody gốc" (melody_lock) instead, which has no such limit - both its methods
# (Tách+Mix and Complete AI) were verified working end-to-end on this exact 259s file.
_COVER_MAX_DURATION_S = 90.0


def _check_cover_duration(duration_s: float) -> str | None:
    if duration_s <= _COVER_MAX_DURATION_S:
        return None
    return (
        f"Bài dài {duration_s:.0f}s vượt quá giới hạn an toàn ({_COVER_MAX_DURATION_S:.0f}s) "
        f"của cover mode trên GPU này - bài dài dễ làm cạn VRAM giữa chừng và treo tới 600 "
        f"giây thay vì báo lỗi rõ ràng. Hãy bật 'Giữ nguyên melody gốc' thay vì cover "
        f"thường - pipeline đó không bị giới hạn này, đã kiểm chứng chạy tốt với bài dài."
    )


async def _run_simple_cover_job(job_id: str, song_bytes: bytes, filename: str, content_type: str, p: dict) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="anysong_")
    try:
        original_ext = Path(filename).suffix or ".mp3"
        original_path = os.path.join(tmp_dir, f"original{original_ext}")
        with open(original_path, "wb") as f:
            f.write(song_bytes)
        duration_s = await _get_audio_duration(original_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    duration_error = _check_cover_duration(duration_s)
    if duration_error:
        raise RuntimeError(duration_error)

    request_data = {
        "task_type": "cover",
        "prompt": p["style_prompt"],
        "lyrics": p["lyrics_payload"],
        "vocal_language": p["vocal_language"],
        "audio_cover_strength": p["cover_strength"],
        "audio_format": p["audio_format"],
        "shift": 3.0,
        "inference_steps": p["inference_steps"],
        "batch_size": p["num_variations"],
    }
    if p["bpm"] is not None:
        request_data["bpm"] = p["bpm"]

    async with _engine_lock, httpx.AsyncClient(timeout=60.0) as client:
        task_id = await _submit_engine_job(client, ENGINE_URL_TURBO, request_data, "src_audio", filename, song_bytes, content_type)

        def on_progress(progress, stage):
            _set_job(job_id, progress=progress, stage=stage or "Đang tạo nhạc...")

        result_entry = await _poll_engine_job(client, ENGINE_URL_TURBO, task_id, on_progress)
        results = json.loads(result_entry["result"])

        # ACE-Step's own "For Best Quality" tip: generate several variations and pick the
        # best one instead of taking whatever the first sample happens to be.
        variations = []
        for item in results:
            audio_bytes = await _download_engine_audio(client, ENGINE_URL_TURBO, item["file"])
            out_name = f"{uuid.uuid4().hex}.{p['audio_format']}"
            (OUTPUT_DIR / out_name).write_bytes(audio_bytes)
            variations.append({"audio_url": f"/outputs/{out_name}", "seed_value": item.get("seed_value")})

        first = results[0]
        response_payload = {
            "audio_url": variations[0]["audio_url"],
            "variations": variations,
            "metas": first.get("metas"),
            "prompt": first.get("prompt"),
            "seed_value": first.get("seed_value"),
            "dit_model": first.get("dit_model"),
            "mode": "cover",
        }
        _append_history(
            {
                "id": Path(variations[0]["audio_url"]).name,
                "audio_url": variations[0]["audio_url"],
                "files": [Path(v["audio_url"]).name for v in variations],
                "source_filename": filename,
                "style_prompt": p["style_prompt"],
                "lyrics_preview": p["lyrics_payload"][:200],
                "mode": "cover",
                "created_at": time.time(),
            }
        )
        _set_job(job_id, status="succeeded", progress=1.0, stage="Xong!", result=response_payload)


async def _run_melody_lock_job(job_id: str, song_bytes: bytes, filename: str, content_type: str, p: dict) -> None:
    track = p["melody_track"]
    tmp_dir = tempfile.mkdtemp(prefix="anysong_")
    try:
        # Detect the source's tempo from the FULL original mix (not the isolated vocal -
        # drums/bass give a beat tracker much more to latch onto). Runs on CPU in the
        # background, overlapping with the Extract call below, so it doesn't add wall time.
        original_ext = Path(filename).suffix or ".mp3"
        original_path = os.path.join(tmp_dir, f"original{original_ext}")
        with open(original_path, "wb") as f:
            f.write(song_bytes)
        bpm_detect_task = asyncio.create_task(asyncio.to_thread(_detect_bpm_sync, original_path))

        async with _engine_lock, httpx.AsyncClient(timeout=60.0) as client:
            # Phase 1/3: Extract BOTH the vocals and instrumental stems (see
            # _extract_both_tracks) - not just whichever one `track` selects - so the user
            # can listen to and compare both before deciding which one actually isolates the
            # backing mix well for their song. Each is itself a diffusion step (the model
            # regenerates the track, conditioned on the full mix) - NOT a lossless/literal
            # split like Demucs - so fidelity matters; run at the top of the base model's
            # quality range (64 steps + ADG) since this is what the whole "preserve the
            # melody" guarantee depends on.
            #
            # engine_base is spawned fresh here rather than run always-on (see
            # app_launcher.py) because its DiT model stays resident in VRAM for its whole
            # process lifetime ("Keeping main model on cuda (persistent)" in its own logs) -
            # a 16GB card can't fit base's ~6GB alongside turbo's ~10-12GB at once. Tried
            # keeping both up: turbo with offload off -> OOM ("need ~2.7GB, only 0.2GB
            # available"); turbo with offload on -> technically fit but had to swap its
            # ~9.7GB of weights over PCIe every step, taking 160s+ and still not finishing
            # for a real song (vs 4.4s alone). Routing the instrumental phase through base
            # instead of turbo "fixed" the crash but made the generated music noticeably
            # worse - base needs its full 32-64 step range to sound as good as turbo does in
            # 8, and quality is the actual point of this app, not just avoiding errors. So:
            # base lives only for the few seconds Extract needs it, then gets killed to
            # actually free its VRAM (ACE-Step has no lighter-weight "unload model" API),
            # and the instrumental-generation phase below runs on the always-on, always-
            # fast, full-quality turbo - exactly like before, just no longer fighting base
            # for VRAM.
            def on_extract_stage(stage: str) -> None:
                _set_job(job_id, progress=0.1, stage=stage)

            base_proc = None
            try:
                extract_vocal_bytes, extract_instrumental_bytes, base_proc = await _extract_both_tracks(
                    client, song_bytes, filename, content_type, on_extract_stage
                )
            finally:
                # Free base's VRAM before turbo needs it below, whether extraction
                # succeeded or not - only if we're the ones who spawned it (None means it
                # was already running externally, e.g. scripts/start.ps1's debug mode, or
                # nothing needed spawning at all because both tracks were cached).
                if base_proc is not None:
                    await _kill_engine_base_process(base_proc)

            # Both extractions are always written out separately (for the debug players
            # below, unconditionally) - `preserved_path` additionally points at whichever
            # ONE of the two `track` actually selected, since that's the one fed into the
            # rest of this pipeline (duration matching + the final mix).
            extract_vocal_path = os.path.join(tmp_dir, "extract_vocals.wav")
            with open(extract_vocal_path, "wb") as f:
                f.write(extract_vocal_bytes)
            extract_instrumental_path = os.path.join(tmp_dir, "extract_instrumental.wav")
            with open(extract_instrumental_path, "wb") as f:
                f.write(extract_instrumental_bytes)
            preserved_path = extract_vocal_path if track == "vocals" else extract_instrumental_path
            vocal_duration = await _get_audio_duration(preserved_path)

            # Phase 2/3: Generate a brand new instrumental-only backing track from scratch
            # (fast XL-turbo, no relation to the extracted audio at all) matched to its
            # duration and tempo. Nothing here can "change the singer" - the voice isn't
            # involved yet. Manual BPM (if the user turned off Auto) always wins over the
            # detected one.
            detected_bpm = await bpm_detect_task
            bpm_to_use = p["bpm"] if p["bpm"] is not None else detected_bpm
            bpm_label = f"~{round(bpm_to_use)} BPM" if bpm_to_use else "BPM tự do"
            _set_job(job_id, progress=0.3, stage=f"Đang sinh nhạc nền mới (Turbo, {bpm_label})...")
            instrumental_data = {
                "task_type": "text2music",
                "prompt": p["style_prompt"],
                "lyrics": _build_instrumental_structure_lyrics(vocal_duration),
                "vocal_language": p["vocal_language"],
                "audio_format": "wav",
                "duration": max(10.0, min(600.0, vocal_duration)),
                "shift": 3.0,
                # thinking=True is not viable at all on Windows right now (see
                # ACESTEP_INIT_LLM comment in app_launcher.py) - not a quality/speed dial.
                "thinking": False,
                "inference_steps": p["inference_steps"],
                "batch_size": p["num_variations"],
            }
            if bpm_to_use is not None:
                instrumental_data["bpm"] = round(bpm_to_use)
            instrumental_task_id = await _submit_engine_job(client, ENGINE_URL_TURBO, instrumental_data)

            def on_instrumental_progress(progress, stage):
                _set_job(job_id, progress=0.3 + progress * 0.5, stage=f"Đang sinh nhạc nền mới... {stage}")

            instrumental_result = await _poll_engine_job(client, ENGINE_URL_TURBO, instrumental_task_id, on_instrumental_progress)
            instrumental_results = json.loads(instrumental_result["result"])

            instrumental_paths = []
            for idx, item in enumerate(instrumental_results):
                instrumental_bytes = await _download_engine_audio(client, ENGINE_URL_TURBO, item["file"])
                path_i = os.path.join(tmp_dir, f"instrumental_{idx}.wav")
                with open(path_i, "wb") as f:
                    f.write(instrumental_bytes)
                instrumental_paths.append(path_i)

        # Keep the extracted track(s) (shared across all variations) and the first generated
        # instrumental as separate downloadable files (not added to the main history
        # gallery) so a bad result can be diagnosed - e.g. Extract isolating near-silent
        # vocals for a given song - by listening to each stage alone instead of only the
        # final mix. Both extracted stems are kept regardless of which one `track` selected,
        # so the user can compare them (see _extract_both_tracks).
        job_uid = uuid.uuid4().hex
        vocal_debug_name = f"{job_uid}_vocal.wav"
        instrumental_extract_debug_name = f"{job_uid}_extract_instrumental.wav"
        instrumental_debug_name = f"{job_uid}_instrumental.wav"
        shutil.copy(extract_vocal_path, OUTPUT_DIR / vocal_debug_name)
        shutil.copy(extract_instrumental_path, OUTPUT_DIR / instrumental_extract_debug_name)
        shutil.copy(instrumental_paths[0], OUTPUT_DIR / instrumental_debug_name)

        # Phase 3/3: Mix the preserved track (whichever `track` selected) with each
        # instrumental variation - ACE-Step's own "generate several, pick the best" tip,
        # applied to the instrumental since the preserved track itself is fixed and not
        # worth regenerating multiple times.
        _set_job(job_id, progress=0.8, stage="Đang mix track gốc với nhạc nền mới...")
        variations = []
        for idx, instrumental_path in enumerate(instrumental_paths):
            out_name = f"{job_uid}_{idx}.{p['audio_format']}"
            out_path = str(OUTPUT_DIR / out_name)
            await _mix_audio(preserved_path, instrumental_path, out_path)
            variations.append({"audio_url": f"/outputs/{out_name}", "seed_value": instrumental_results[idx].get("seed_value")})

        instrumental_first = instrumental_results[0]
        response_payload = {
            "audio_url": variations[0]["audio_url"],
            "variations": variations,
            "vocal_track_url": f"/outputs/{vocal_debug_name}",
            "instrumental_extract_url": f"/outputs/{instrumental_extract_debug_name}",
            "instrumental_track_url": f"/outputs/{instrumental_debug_name}",
            "metas": instrumental_first.get("metas"),
            "prompt": p["style_prompt"],
            "seed_value": instrumental_first.get("seed_value"),
            "dit_model": instrumental_first.get("dit_model"),
            "mode": "melody_lock",
            "melody_track": track,
        }
        _append_history(
            {
                "id": Path(variations[0]["audio_url"]).name,
                "audio_url": variations[0]["audio_url"],
                "files": [Path(v["audio_url"]).name for v in variations] + [vocal_debug_name, instrumental_extract_debug_name, instrumental_debug_name],
                "source_filename": filename,
                "style_prompt": p["style_prompt"],
                "lyrics_preview": p["lyrics_payload"][:200],
                "mode": "melody_lock",
                "melody_track": track,
                "created_at": time.time(),
            }
        )
        _set_job(job_id, status="succeeded", progress=1.0, stage="Xong!", result=response_payload)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _run_melody_complete_job(job_id: str, song_bytes: bytes, filename: str, content_type: str, p: dict) -> None:
    """Experimental alternative to _run_melody_lock_job (melody_method="complete").

    _run_melody_lock_job isolates the vocal, then generates a brand new instrumental
    completely BLIND to the vocal's actual content - only matched via BPM + total
    duration + a generic 4-6 section skeleton (Intro/Build/Climax/...). On a real song
    that skeleton's timing has no relationship to where the vocal's own verses/chorus/
    pauses actually land, so the two drift out of sync (confirmed by ear on real
    generations: "nhạc và lời chạy lung tung"). "cover" mode doesn't have this problem
    because it's a single diffusion pass conditioned on the actual source audio, so the
    model can see/hear exactly where things happen and shape its output accordingly.

    This applies that same principle to melody preservation: extract the vocal (same as
    the mix method), then task_type=complete regenerates the WHOLE song (new vocals +
    new instrumentation) in a single pass CONDITIONED ON that specific vocal track, so it
    should follow its actual phrasing rather than a generic arbitrary timeline. Both
    Extract and Complete are base-tier-only, so this whole pipeline stays on the one
    on-demand base process - no turbo involved, no separate mix step needed (Complete's
    output already includes the vocal it was conditioned on).

    Trade-off vs the mix method: Complete is itself a diffusion resynthesis (like Extract
    already is) - it does NOT losslessly preserve the singer's exact voice/timbre, only
    the melodic/rhythmic content the model inferred from the given track. An earlier
    version of this app tried Complete for this reason and reverted to the extract+mix
    approach. Revisiting it now because exact voice preservation turned out to be a
    trade the user is willing to make for actually staying in sync - not something
    settled by reasoning alone, needs a real listen-and-compare against the mix method."""
    track = p["melody_track"]
    tmp_dir = tempfile.mkdtemp(prefix="anysong_")
    try:
        async with _engine_lock, httpx.AsyncClient(timeout=60.0) as client:
            # Extract BOTH the vocals and instrumental stems (see _extract_both_tracks),
            # not just whichever one `track` selects, so the user can compare them - base
            # has to spawn here regardless of cache state since Complete itself needs it
            # right after either way (unlike the mix method, which can skip spawning base
            # entirely on a full cache hit since its other phase runs on turbo).
            def on_extract_stage(stage: str) -> None:
                _set_job(job_id, progress=0.1, stage=stage)

            base_proc = None
            try:
                extract_vocal_bytes, extract_instrumental_bytes, base_proc = await _extract_both_tracks(
                    client, song_bytes, filename, content_type, on_extract_stage
                )
                extracted_bytes = extract_vocal_bytes if track == "vocals" else extract_instrumental_bytes

                extract_vocal_path = os.path.join(tmp_dir, "extract_vocals.wav")
                with open(extract_vocal_path, "wb") as f:
                    f.write(extract_vocal_bytes)
                extract_instrumental_path = os.path.join(tmp_dir, "extract_instrumental.wav")
                with open(extract_instrumental_path, "wb") as f:
                    f.write(extract_instrumental_bytes)

                # Regenerate the whole song around the extracted track in one pass - real
                # lyrics (not a structure-only placeholder) since Complete resings the
                # vocal too, unlike the mix method's instrumental-only generation.
                #
                # Same quality recipe as Extract above (64 steps + ADG - "only works for
                # base model" per acestep/inference.py's own docstring), not the lower
                # BASE_MODEL_INFERENCE_STEPS=32 floor - Complete is the harder of the two
                # tasks (synthesizing the ENTIRE mix, not isolating one given track) AND
                # its output IS the final result the user hears, unlike Extract which is
                # just an intermediate step. Confirmed by the user's own listening test:
                # first pass here omitted use_adg entirely and used only 32 steps, and the
                # result was "rất khó nghe" (hard to listen to).
                #
                # No "duration" field, same as cover/extract - this task conditions on the
                # given src_audio track directly, so it naturally follows THAT audio's
                # actual length; a separately-specified duration is a text2music/thinking
                # concept (LM CoT-mode auto-duration), not applicable here.
                #
                # audio_cover_strength does NOT apply to task_type=complete at all (it's a
                # cover-mode-only knob) - the frontend's "Độ bám sát bản gốc" slider has no
                # effect in this mode and should be hidden when melody_method=complete is
                # selected, to avoid the user (reasonably) assuming it does something here.
                #
                # track_classes tells the model WHAT to add, via the engine's own
                # "Complete the input track with {TRACK_CLASSES}:" instruction template
                # (acestep/constants.py TASK_INSTRUCTIONS["complete"]) - left unset before,
                # which falls back to the vague "Complete the input track:" default with no
                # hint of what's missing. Confirmed by the user's own listening test this
                # was the real cause of two distinct bad outcomes: vocals+Complete came back
                # as a near-acapella (model didn't reliably infer "add full instrumentation"
                # on its own), and instrumental+Complete came back "quái đản" (bizarre) -
                # plausibly the model attempting to regenerate/alter the given instrumental
                # itself rather than clearly being told to layer vocals on top of it. Now
                # explicit about which direction to complete in, using the engine's own
                # trained stem vocabulary (acestep/constants.py TRACK_NAMES).
                if track == "vocals":
                    track_classes = ["backing_vocals", "guitar", "bass", "drums", "keyboard", "synth", "strings", "brass", "woodwinds", "percussion", "fx"]
                else:
                    track_classes = ["vocals", "backing_vocals"]
                _set_job(job_id, progress=0.25, stage="Đang tạo lại toàn bộ bài hát bám theo giai điệu...")
                complete_data = {
                    "task_type": "complete",
                    "prompt": p["style_prompt"],
                    "lyrics": p["lyrics_payload"],
                    "vocal_language": p["vocal_language"],
                    "audio_format": p["audio_format"],
                    "shift": 3.0,
                    "inference_steps": 64,
                    "use_adg": True,
                    "guidance_scale": 7.0,
                    "batch_size": p["num_variations"],
                    "track_classes": track_classes,
                }
                def on_complete_progress(progress, stage):
                    _set_job(job_id, progress=0.25 + progress * 0.7, stage=f"Đang tạo lại toàn bộ bài hát... {stage}")

                async def _do_complete() -> list:
                    task_id = await _submit_engine_job(client, ENGINE_URL_BASE, complete_data, "src_audio", "vocal.wav", extracted_bytes, "audio/wav")
                    result = await _poll_engine_job(client, ENGINE_URL_BASE, task_id, on_complete_progress)
                    return json.loads(result["result"])

                # Retry once, on a fresh base respawn, if base went unreachable between
                # Extract finishing and this Complete call starting - same rationale as
                # _extract_both_tracks's own retry (a health check alone doesn't guarantee
                # the process stays up through the next real request).
                try:
                    complete_results = await _do_complete()
                except RuntimeError as exc:
                    if "Mất kết nối" not in str(exc):
                        raise
                    await _kill_engine_base_process(base_proc)
                    base_proc = await _ensure_engine_base_running(client)
                    complete_results = await _do_complete()

                variations = []
                for item in complete_results:
                    audio_bytes = await _download_engine_audio(client, ENGINE_URL_BASE, item["file"])
                    out_name = f"{uuid.uuid4().hex}.{p['audio_format']}"
                    (OUTPUT_DIR / out_name).write_bytes(audio_bytes)
                    variations.append({"audio_url": f"/outputs/{out_name}", "seed_value": item.get("seed_value")})
            finally:
                # Free base's VRAM whether this succeeded or not, only if we spawned it.
                if base_proc is not None:
                    await _kill_engine_base_process(base_proc)

        job_uid = uuid.uuid4().hex
        vocal_debug_name = f"{job_uid}_vocal.wav"
        instrumental_extract_debug_name = f"{job_uid}_extract_instrumental.wav"
        shutil.copy(extract_vocal_path, OUTPUT_DIR / vocal_debug_name)
        shutil.copy(extract_instrumental_path, OUTPUT_DIR / instrumental_extract_debug_name)

        first = complete_results[0]
        response_payload = {
            "audio_url": variations[0]["audio_url"],
            "variations": variations,
            "vocal_track_url": f"/outputs/{vocal_debug_name}",
            "instrumental_extract_url": f"/outputs/{instrumental_extract_debug_name}",
            "metas": first.get("metas"),
            "prompt": first.get("prompt"),
            "seed_value": first.get("seed_value"),
            "dit_model": first.get("dit_model"),
            "mode": "melody_complete",
            "melody_track": track,
        }
        _append_history(
            {
                "id": Path(variations[0]["audio_url"]).name,
                "audio_url": variations[0]["audio_url"],
                "files": [Path(v["audio_url"]).name for v in variations] + [vocal_debug_name, instrumental_extract_debug_name],
                "source_filename": filename,
                "style_prompt": p["style_prompt"],
                "lyrics_preview": p["lyrics_payload"][:200],
                "mode": "melody_complete",
                "melody_track": track,
                "created_at": time.time(),
            }
        )
        _set_job(job_id, status="succeeded", progress=1.0, stage="Xong!", result=response_payload)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _run_text2music_job(job_id: str, p: dict) -> None:
    """"Create" tab: a brand new song from just a style prompt (+ optional lyrics), no
    source audio at all - a direct, user-facing exposure of the same task_type=text2music
    call melody_lock's mix method already uses internally to generate its instrumental
    (see _run_melody_lock_job) - here run standalone on the fast XL-turbo engine, with
    real lyrics when the user supplied any instead of always forcing "[Instrumental]"."""
    lyrics_payload = p["lyrics_payload"] or _build_instrumental_structure_lyrics(p["duration"])

    request_data = {
        "task_type": "text2music",
        "prompt": p["style_prompt"],
        "lyrics": lyrics_payload,
        "vocal_language": p["vocal_language"],
        "audio_format": p["audio_format"],
        "duration": p["duration"],
        "shift": 3.0,
        "thinking": False,
        "inference_steps": p["inference_steps"],
        "batch_size": p["num_variations"],
    }
    if p["bpm"] is not None:
        request_data["bpm"] = p["bpm"]

    async with _engine_lock, httpx.AsyncClient(timeout=60.0) as client:
        task_id = await _submit_engine_job(client, ENGINE_URL_TURBO, request_data)

        def on_progress(progress, stage):
            _set_job(job_id, progress=progress, stage=stage or "Đang tạo nhạc...")

        result_entry = await _poll_engine_job(client, ENGINE_URL_TURBO, task_id, on_progress)
        results = json.loads(result_entry["result"])

        variations = []
        for item in results:
            audio_bytes = await _download_engine_audio(client, ENGINE_URL_TURBO, item["file"])
            out_name = f"{uuid.uuid4().hex}.{p['audio_format']}"
            (OUTPUT_DIR / out_name).write_bytes(audio_bytes)
            variations.append({"audio_url": f"/outputs/{out_name}", "seed_value": item.get("seed_value")})

        first = results[0]
        response_payload = {
            "audio_url": variations[0]["audio_url"],
            "variations": variations,
            "metas": first.get("metas"),
            "prompt": first.get("prompt"),
            "seed_value": first.get("seed_value"),
            "dit_model": first.get("dit_model"),
            "mode": "create",
        }
        _append_history(
            {
                "id": Path(variations[0]["audio_url"]).name,
                "audio_url": variations[0]["audio_url"],
                "files": [Path(v["audio_url"]).name for v in variations],
                "source_filename": None,
                "style_prompt": p["style_prompt"],
                "lyrics_preview": lyrics_payload[:200],
                "mode": "create",
                "created_at": time.time(),
            }
        )
        _set_job(job_id, status="succeeded", progress=1.0, stage="Xong!", result=response_payload)


async def _run_create_job(job_id: str, p: dict) -> None:
    try:
        await _run_text2music_job(job_id, p)
    except Exception as exc:
        _set_job(job_id, status="failed", error=str(exc))


async def _run_job(job_id: str, song_bytes: bytes, filename: str, content_type: str, p: dict) -> None:
    try:
        if p["melody_lock"] and p["melody_method"] == "complete":
            await _run_melody_complete_job(job_id, song_bytes, filename, content_type, p)
        elif p["melody_lock"]:
            await _run_melody_lock_job(job_id, song_bytes, filename, content_type, p)
        else:
            await _run_simple_cover_job(job_id, song_bytes, filename, content_type, p)
    except Exception as exc:
        _set_job(job_id, status="failed", error=str(exc))


@app.get("/api/generate/status/{job_id}")
async def generate_status(job_id: str):
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return JSONResponse(job)


@app.post("/api/generate")
async def generate(
    song: UploadFile,
    style_prompt: str = Form(...),
    lyrics: str = Form(""),
    vocal_language: str = Form("vi"),
    cover_strength: float = Form(0.7),
    bpm: str = Form(""),
    inference_steps: int = Form(8),
    audio_format: str = Form("mp3"),
    melody_lock: bool = Form(False),
    melody_track: str = Form("vocals"),
    melody_method: str = Form("mix"),
    num_variations: int = Form(1),
):
    ext = Path(song.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}")
    if not style_prompt.strip():
        raise HTTPException(status_code=400, detail="style_prompt is required")
    if not 0.0 <= cover_strength <= 1.0:
        raise HTTPException(status_code=400, detail="cover_strength must be between 0.0 and 1.0")
    if not 1 <= inference_steps <= 30:
        raise HTTPException(status_code=400, detail="inference_steps must be between 1 and 30")
    if melody_track not in MELODY_TRACKS:
        raise HTTPException(status_code=400, detail=f"melody_track must be one of {MELODY_TRACKS}")
    if melody_method not in ("mix", "complete"):
        raise HTTPException(status_code=400, detail="melody_method must be 'mix' or 'complete'")
    if not 1 <= num_variations <= 4:
        raise HTTPException(status_code=400, detail="num_variations must be between 1 and 4")

    bpm_value = None
    if bpm.strip():
        try:
            bpm_value = int(float(bpm))
        except ValueError:
            raise HTTPException(status_code=400, detail="bpm must be a number")
        if not 30 <= bpm_value <= 300:
            raise HTTPException(status_code=400, detail="bpm must be between 30 and 300")

    song_bytes = await song.read()
    lyrics_payload = lyrics.strip() or "[Instrumental]"

    params = {
        "style_prompt": style_prompt,
        "lyrics_payload": lyrics_payload,
        "vocal_language": vocal_language,
        "cover_strength": cover_strength,
        "bpm": bpm_value,
        "inference_steps": inference_steps,
        "audio_format": audio_format,
        "melody_lock": melody_lock,
        "melody_track": melody_track,
        "melody_method": melody_method,
        "num_variations": num_variations,
    }

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "progress": 0.0, "stage": "Đang gửi job...", "result": None, "error": None}

    asyncio.create_task(_run_job(job_id, song_bytes, song.filename, song.content_type, params))

    return JSONResponse({"job_id": job_id})


@app.post("/api/generate/create")
async def generate_create(
    style_prompt: str = Form(...),
    lyrics: str = Form(""),
    vocal_language: str = Form("vi"),
    bpm: str = Form(""),
    inference_steps: int = Form(8),
    audio_format: str = Form("mp3"),
    num_variations: int = Form(1),
    duration: float = Form(120.0),
):
    """"Create" tab: text2music straight from a prompt (+ optional lyrics), no source
    song upload - see _run_text2music_job. Shares the same job store/status endpoint
    (/api/generate/status/{job_id}) as /api/generate since job_id already disambiguates."""
    if not style_prompt.strip():
        raise HTTPException(status_code=400, detail="style_prompt is required")
    if not 1 <= inference_steps <= 30:
        raise HTTPException(status_code=400, detail="inference_steps must be between 1 and 30")
    if not 1 <= num_variations <= 4:
        raise HTTPException(status_code=400, detail="num_variations must be between 1 and 4")
    if not 10.0 <= duration <= 300.0:
        raise HTTPException(status_code=400, detail="duration must be between 10 and 300 seconds")

    bpm_value = None
    if bpm.strip():
        try:
            bpm_value = int(float(bpm))
        except ValueError:
            raise HTTPException(status_code=400, detail="bpm must be a number")
        if not 30 <= bpm_value <= 300:
            raise HTTPException(status_code=400, detail="bpm must be between 30 and 300")

    params = {
        "style_prompt": style_prompt,
        "lyrics_payload": lyrics.strip(),
        "vocal_language": vocal_language,
        "bpm": bpm_value,
        "inference_steps": inference_steps,
        "audio_format": audio_format,
        "num_variations": num_variations,
        "duration": duration,
    }

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "progress": 0.0, "stage": "Đang gửi job...", "result": None, "error": None}

    asyncio.create_task(_run_create_job(job_id, params))

    return JSONResponse({"job_id": job_id})


app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "frontend"), html=True), name="frontend")
