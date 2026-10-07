"""
AnySong Gemini service.

Mo 1 cua so pywebview that chay gemini.google.com, dang nhap bang tai khoan
Google that cua ban (dang nhap that qua man hinh dang nhap that cua Google,
KHONG phai bypass gi ca), roi AN cua so di (WS_EX_LAYERED + alpha=0, cach nay
khong bi Windows/DWM coi la occluded nen trang van chay binh thuong o nen) sau
khi da dang nhap xong. Tu do ve sau, backend chinh cua AnySong goi HTTP vao
service nay (port 8004) de nho Gemini viet/chuan hoa prompt+lyrics - dung dung
ky thuat da kiem chung trong 2 project khac cua ban (LoreCharacter, Idea2Post),
khong qua Gemini API/billing.

Phien dang nhap duoc luu trong thu muc .webview_data canh file nay (gitignore),
khoa quyen chi chu may doc duoc (harden_session_store).

Chay: python service.py  (hoac qua scripts/start.ps1 cua AnySong)
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

BACKEND = (os.environ.get("ANYSONG_GEMINI_BACKEND") or "edgechromium").strip().lower()

_NO_THROTTLE = (" --disable-backgrounding-occluded-windows"
                " --disable-renderer-backgrounding"
                " --disable-background-timer-throttling")

if BACKEND == "qt":
    os.environ["QT_OPENGL"] = "software"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + _NO_THROTTLE).strip()
else:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "") + _NO_THROTTLE).strip()

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import webview  # noqa: E402
from flask import Flask, jsonify, request  # noqa: E402

from gemini_driver import GeminiDriver  # noqa: E402

GEMINI_URL = "https://gemini.google.com/app"
APP_DIR = Path(__file__).resolve().parent
STORAGE_DIR = APP_DIR / ".webview_data"
WINDOW_TITLE = "Gemini (nền) - AnySong, đăng nhập Google ở đây"
HOST, PORT = "127.0.0.1", 8004

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

CLOUD_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud",
                  "yandexdisk", "box sync", "pcloud", "megasync")


def harden_session_store(path: Path) -> str:
    """Khoa .webview_data lai, chi chu may doc duoc - cookie KHONG duoc ma hoa,
    ai doc duoc file la vao thang tai khoan Google."""
    if not path.exists():
        return "chua co thu muc phien"
    user = os.environ.get("USERNAME") or ""
    if not user:
        return "khong xac dinh duoc tai khoan Windows"
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"{user}:(OI)(CI)F",
             "/grant:r", "*S-1-5-18:(OI)(CI)F",
             "/grant:r", "*S-1-5-32-544:(OI)(CI)F",
             "/Q"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "ok"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"khong khoa duoc quyen ({exc})"


def cloud_synced_warning(path: Path) -> str:
    low = str(path).lower()
    for marker in CLOUD_MARKERS:
        if marker in low:
            return marker
    return ""


class GeminiService:
    def __init__(self):
        self.window = None
        self.driver = None
        self.logged_in = False
        self.ready = False
        self._ask_lock = threading.Lock()

    # -- window lifecycle --------------------------------------------------

    def _gemini_hwnd(self):
        user32 = ctypes.windll.user32
        target = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def callback(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith("Gemini ("):
                target.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        return target[0] if target else None

    def hide_window(self):
        try:
            hwnd = self._gemini_hwnd()
            if hwnd:
                user32 = ctypes.windll.user32
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
                user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
        except Exception:
            pass

    def show_window(self):
        try:
            hwnd = self._gemini_hwnd()
            if hwnd:
                user32 = ctypes.windll.user32
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_LAYERED)
            self.window.show()
        except Exception:
            pass

    def _startup(self):
        """Chay tren thread nen sau khi cua so da tao xong."""
        print("[gemini_service] _startup thread bat dau...")
        self.driver = GeminiDriver(self.window, log=print)
        time.sleep(3)
        print("[gemini_service] Dang cho trang Gemini san sang...")
        ready = self.driver.wait_ready(timeout=40)
        self.ready = bool(ready.get("ok"))
        print(f"[gemini_service] wait_ready ket qua: {ready}")
        if not self.ready:
            print("[gemini_service] Trang Gemini chua san sang.")
            return
        self._poll_login()

    def _poll_login(self):
        """Tu dong an cua so ngay khi phat hien da dang nhap; neu chua thi de
        cua so hien, ngoi cho nguoi dung tu dang nhap."""
        while True:
            info = self.driver.check_login()
            print(f"[gemini_service] check_login: {info}")
            if info.get("logged_in"):
                self.logged_in = True
                self.hide_window()
                print("[gemini_service] Da dang nhap, da an cua so.")
                return
            time.sleep(2)


service = GeminiService()
flask_app = Flask(__name__)


@flask_app.route("/status", methods=["GET"])
def status():
    return jsonify({"ready": service.ready, "logged_in": service.logged_in})


@flask_app.route("/show", methods=["POST"])
def show():
    """De nguoi dung tu bam mo lai cua so dang nhap neu can (vd session het han)."""
    service.show_window()
    return jsonify({"ok": True})


@flask_app.route("/hide", methods=["POST"])
def hide():
    service.hide_window()
    return jsonify({"ok": True})


@flask_app.route("/debug_state", methods=["GET"])
def debug_state():
    if not service.driver:
        return jsonify({"ok": False, "error": "NO_DRIVER"})
    return jsonify({
        "count_responses": service.driver.count_responses(),
        "state": service.driver.state(),
    })


@flask_app.route("/ask", methods=["POST"])
def ask():
    if not service.logged_in:
        return jsonify({"ok": False, "error": "NOT_LOGGED_IN"}), 409
    body = request.get_json(force=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "EMPTY_PROMPT"}), 400
    timeout = float(body.get("timeout") or 120)

    with service._ask_lock:
        new_chat_result = service.driver.new_chat()
        if not new_chat_result.get("ok"):
            return jsonify({"ok": False, "error": f"NEW_CHAT_FAILED: {new_chat_result.get('error')}"}), 502
        result = service.driver.ask_text(prompt, timeout=timeout)
    return jsonify(result)


def run_flask():
    flask_app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


def on_loaded():
    print("[gemini_service] on_loaded fired.")
    threading.Thread(target=service._startup, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()


def main():
    STORAGE_DIR.mkdir(exist_ok=True)
    harden_session_store(STORAGE_DIR)
    leak = cloud_synced_warning(STORAGE_DIR)
    if leak:
        print(f"[gemini_service] CANH BAO: thu muc phien nam trong '{leak}' (dong bo cloud) - "
              f"phien dang nhap Google se bi coi nhu da tai len ben thu 3. Nen chuyen project ra ngoai.")

    window = webview.create_window(
        WINDOW_TITLE, url=GEMINI_URL, width=1000, height=800,
    )
    service.window = window
    webview.start(on_loaded, private_mode=False, storage_path=str(STORAGE_DIR), gui=BACKEND)


if __name__ == "__main__":
    main()
