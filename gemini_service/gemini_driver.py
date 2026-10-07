"""
Driver diieu khien gemini.google.com qua JS injection (ban text-only, rut gon
tu Idea2Post/gemini_driver.py - bo toan bo phan lien quan anh vi AnySong chi
can hoi-dap text).

QUY TAC QUAN TRONG: moi doan JS o day phai la DONG BO (sync). Backend Qt cua
pywebview dung QWebEnginePage.runJavaScript() - ham nay KHONG cho Promise, nen
bat ky async/fetch(...)/await nao cung tra ve rong ngay lap tuc.

Cac selector duoi day da duoc xac minh truc tiep tren gemini.google.com (ban
tieng Viet, tai khoan da dang nhap) trong Idea2Post/LoreCharacter:
  - o nhap  : rich-textarea [contenteditable="true"]
  - nut gui : button[aria-label] khop /send|gui tin/i
  - cau tra loi : <model-response>, phan text trong <message-content>
  - tra loi xong: .markdown co aria-busy="false"
"""

import json
import re
import threading
import time

GEMINI_URL = "https://gemini.google.com/app"

_JS_LOCK = threading.Lock()
JS_TIMEOUT_DEFAULT = 45


class JsTimeout(Exception):
    """evaluate_js khong tra loi trong thoi gian cho phep."""


def run_js(window, script, timeout=JS_TIMEOUT_DEFAULT):
    """Moi lenh JS di qua day: window.evaluate_js() cua pywebview/Qt CO THE TREO
    VINH VIEN (cho semaphore tu phia Qt), nen luon goi qua thread phu co han
    gio: qua han thi bo thread do va bao loi len tren, thay vi treo ca app."""
    with _JS_LOCK:
        box = {}
        done = threading.Event()

        def work():
            try:
                box["value"] = window.evaluate_js(script)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(timeout):
            raise JsTimeout(f"evaluate_js qua {timeout}s khong phan hoi")
        if "error" in box:
            raise box["error"]
        return box.get("value")


_SR_PREFIX = re.compile(r"^\s*(gemini (đã nói|said)|bard said)\s*", re.IGNORECASE)


def clean_response_text(text: str) -> str:
    return _SR_PREFIX.sub("", text or "").strip()


def _parse(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"BAD_JS_RESULT: {result[:200]}"}
    if result is None:
        return {"ok": False, "error": "JS_RETURNED_NULL"}
    return result


# ---------------------------------------------------------------- JS snippets

COUNT_RESPONSES_JS = "document.querySelectorAll('model-response').length"

FILL_JS = """
(() => {
    const input = document.querySelector('rich-textarea [contenteditable="true"]')
        || document.querySelector('[contenteditable="true"][role="textbox"]');
    if (!input) return JSON.stringify({ok:false, error:'NO_INPUT_FOUND'});
    input.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, %s);
    input.dispatchEvent(new InputEvent('input', {bubbles: true}));
    return JSON.stringify({ok:true, len: (input.innerText || '').trim().length});
})()
"""

SEND_JS = """
(() => {
    const btn = Array.from(document.querySelectorAll('button[aria-label]'))
        .find(b => /send|g\\u1eedi tin/i.test(b.getAttribute('aria-label') || ''));
    if (!btn) return JSON.stringify({ok:false, error:'NO_SEND_BUTTON'});
    if (btn.disabled) return JSON.stringify({ok:false, error:'SEND_BUTTON_DISABLED'});
    btn.click();
    return JSON.stringify({ok:true});
})()
"""

STATE_JS = """
(() => {
    const responses = document.querySelectorAll('model-response');
    const info = {count: responses.length};
    const last = responses[responses.length - 1];
    if (last) {
        const md = last.querySelector('.markdown');
        info.busy = md ? md.getAttribute('aria-busy') : null;
        const mc = last.querySelector('message-content');
        info.text = ((mc || last).innerText || '').trim();
    }
    return JSON.stringify(info);
})()
"""

_FIRE_CLICK_JS = """
    const fireClick = (target) => {
        const opts = {bubbles: true, cancelable: true, view: window, buttons: 1};
        for (const type of ['pointerover','pointerenter','pointerdown','mousedown',
                            'pointerup','mouseup','click']) {
            const Ev = type.startsWith('pointer') ? PointerEvent : MouseEvent;
            try { target.dispatchEvent(new Ev(type, opts)); } catch (e) {}
        }
    };
"""

NEW_CHAT_JS = """
(() => {
""" + _FIRE_CLICK_JS + """
    const clickable = (el) => {
        if (!el) return null;
        if (el.matches('a, button, [role="button"]')) return el;
        return el.querySelector('a, button, [role="button"]') || el;
    };
    const pick = () => {
        const host = document.querySelector('[data-test-id="new-chat-button"]');
        if (host) return clickable(host);
        const labelled = Array.from(document.querySelectorAll(
            'a[aria-label], button[aria-label], [role="button"][aria-label]'));
        const byLabel = labelled.find(
            b => /new chat|cu\\u1ed9c tr\\u00f2 chuy\\u1ec7n m\\u1edbi/i
                 .test(b.getAttribute('aria-label') || ''));
        if (byLabel) return clickable(byLabel);
        const byText = Array.from(document.querySelectorAll('a, button, [role="button"]'))
            .find(b => /^\\s*(new chat|cu\\u1ed9c tr\\u00f2 chuy\\u1ec7n m\\u1edbi)\\s*$/i
                       .test(b.textContent || ''));
        return clickable(byText);
    };
    const target = pick();
    if (!target) return JSON.stringify({ok:false, error:'NO_NEW_CHAT_BUTTON'});
    fireClick(target);
    return JSON.stringify({ok:true, tag: target.tagName,
                           label: target.getAttribute('aria-label') || ''});
})()
"""

CHAT_STATE_JS = """
(() => JSON.stringify({
    path: location.pathname,
    responses: document.querySelectorAll('model-response').length
}))()
"""

READY_JS = """
(() => JSON.stringify({
    ready: document.readyState,
    input: !!document.querySelector('rich-textarea [contenteditable="true"]')
}))()
"""

LOGGED_IN_JS = """
(() => {
    const signIn = Array.from(document.querySelectorAll('a[aria-label], button[aria-label]'))
        .some(b => /^sign in$|^\\u0110\\u0103ng nh\\u1eadp$/i.test((b.getAttribute('aria-label')||'').trim()));
    const hasInput = !!document.querySelector('rich-textarea [contenteditable="true"]');
    return JSON.stringify({logged_in: !signIn, has_input: hasInput});
})()
"""


# ------------------------------------------------------------------ high level

class GeminiDriver:
    """Cac thao tac muc cao voi trang Gemini. Moi ham o day CHAY TREN THREAD NEN
    (khong phai thread GUI), vi chung cho bang time.sleep."""

    def __init__(self, window, log=None):
        self.window = window
        self._log = log or (lambda msg: None)

    def log(self, msg):
        self._log(msg)

    def _js(self, script, timeout=25):
        try:
            return _parse(run_js(self.window, script, timeout=timeout))
        except JsTimeout as exc:
            return {"ok": False, "error": f"JS_TIMEOUT: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"JS_ERROR: {exc}"}

    def count_responses(self):
        try:
            return int(run_js(self.window, COUNT_RESPONSES_JS, timeout=25) or 0)
        except Exception:  # noqa: BLE001
            return None

    def state(self):
        return self._js(STATE_JS)

    def check_login(self):
        return self._js(LOGGED_IN_JS)

    def wait_ready(self, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self._js(READY_JS, timeout=8)
            if isinstance(res, dict) and res.get("ready") == "complete" and res.get("input"):
                return {"ok": True}
            time.sleep(1)
        return {"ok": False, "error": "PAGE_NOT_READY"}

    def new_chat(self, timeout=45):
        deadline = time.time() + timeout
        for _ in range(2):
            if time.time() > deadline:
                break
            clicked = self._js(NEW_CHAT_JS)
            self.wait_ready(timeout=15)
            for _ in range(10):
                time.sleep(0.6)
                if self._is_blank_chat():
                    return {"ok": True, "clicked": clicked}
            if not clicked.get("ok"):
                break
        self.log("Nut 'Cuoc tro chuyen moi' khong an, dang tu chuyen ve trang chinh...")
        try:
            run_js(self.window,
                   f"setTimeout(() => location.href = '{GEMINI_URL}', 0); 1",
                   timeout=20)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"NEW_CHAT_FAILED: {exc}"}
        time.sleep(2)
        self.wait_ready(timeout=40)
        for _ in range(10):
            if self._is_blank_chat():
                return {"ok": True, "reloaded": True}
            time.sleep(0.6)
        return {"ok": False, "error": "NEW_CHAT_FAILED"}

    def _is_blank_chat(self) -> bool:
        state = self._js(CHAT_STATE_JS, timeout=15)
        if "path" not in state:
            return False
        path = (state.get("path") or "").rstrip("/")
        fresh_url = path in ("/app", "", "/")
        return fresh_url or state.get("responses") == 0

    def send(self, prompt):
        fill = {}
        for _attempt in range(3):
            fill = self._js(FILL_JS % json.dumps(prompt))
            if not fill.get("ok"):
                return fill
            if fill.get("len"):
                break
            time.sleep(1.0)
        if not fill.get("len"):
            return {"ok": False, "error": "INPUT_STAYED_EMPTY"}

        for _ in range(20):
            time.sleep(0.5)
            send = self._js(SEND_JS)
            if send.get("ok"):
                return send
            if send.get("error") not in ("NO_SEND_BUTTON", "SEND_BUTTON_DISABLED"):
                return send
        return {"ok": False, "error": "NO_SEND_BUTTON"}

    def _wait(self, prev_count, timeout, cancel=None):
        deadline = time.time() + timeout
        stable_text, stable_hits = None, 0
        last_text = ""
        while time.time() < deadline:
            if cancel and cancel():
                return {"ok": False, "error": "CANCELLED"}
            time.sleep(1.5)
            st = self.state()
            if not isinstance(st, dict) or st.get("count", 0) <= prev_count:
                continue
            last_text = clean_response_text(st.get("text")) or last_text
            busy = st.get("busy")
            if busy == "false":
                text = clean_response_text(st.get("text"))
                if text and text == stable_text:
                    stable_hits += 1
                    if stable_hits >= 2:
                        return {"ok": True, "text": text, "state": st}
                else:
                    stable_text, stable_hits = text, 0
        return {"ok": False, "error": "TIMEOUT_TEXT", "text": last_text[:500]}

    def ask_text(self, prompt, timeout=120, cancel=None):
        prev = self.count_responses()
        if prev is None:
            return {"ok": False, "error": "JS_TIMEOUT: trang Gemini khong phan hoi"}
        sent = self.send(prompt)
        if not sent.get("ok"):
            return sent
        return self._wait(prev, timeout, cancel=cancel)
