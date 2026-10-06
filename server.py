"""Escritorio remoto por URL HTTPS (Tailscale).

Corre en la PC que quieres controlar. Escucha solo en 127.0.0.1;
`tailscale serve` le pone HTTPS y lo expone dentro de tu tailnet.
"""
import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import time
from pathlib import Path

import mss
import pyautogui
from aiohttp import web
from PIL import Image

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

HOST = os.environ.get("RD_HOST", "127.0.0.1")
PORT = int(os.environ.get("RD_PORT", "8765"))
PASSWORD = os.environ.get("RD_PASSWORD", "")  # opcional; vacía = entra solo con tu identidad de Tailscale
# Opcional: solo esta cuenta de Tailscale (ej. tu@hotmail.com). `tailscale serve` inyecta el header.
ALLOWED_LOGIN = os.environ.get("RD_ALLOWED_LOGIN", "").lower()
FPS = int(os.environ.get("RD_FPS", "12"))
QUALITY = int(os.environ.get("RD_QUALITY", "60"))
MAX_WIDTH = int(os.environ.get("RD_MAX_WIDTH", "1600"))
SECRET = secrets.token_bytes(32)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # _MEIPASS: ejecutable PyInstaller
STATIC = BASE / "static"


def make_token() -> str:
    exp = str(int(time.time()) + 12 * 3600)
    sig = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(tok: str | None) -> bool:
    try:
        exp, sig = (tok or "").split(".")
        good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except ValueError:
        return False


def tailnet_ok(request: web.Request) -> bool:
    # `tailscale serve` inyecta este header solo para usuarios autenticados del tailnet;
    # exigirlo evita que un proceso local o un acceso directo al puerto entre sin pasar por Tailscale.
    login = request.headers.get("Tailscale-User-Login", "").lower()
    if not login:
        return False
    return not ALLOWED_LOGIN or login == ALLOWED_LOGIN


@web.middleware
async def guard(request, handler):
    if not tailnet_ok(request):
        raise web.HTTPForbidden(text="Cuenta de Tailscale no permitida")
    if not PASSWORD:
        return await handler(request)
    if request.path in ("/login", "/login.html") or request.path.startswith("/static/login"):
        return await handler(request)
    if not valid_token(request.cookies.get("rd")):
        if request.path == "/ws":
            raise web.HTTPUnauthorized()
        raise web.HTTPFound("/login")
    return await handler(request)


_fails: dict[str, list[float]] = {}


async def login(request: web.Request):
    if request.method == "GET":
        return web.FileResponse(STATIC / "login.html")
    ip = request.headers.get("X-Forwarded-For", request.remote or "")
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise web.HTTPTooManyRequests(text="Demasiados intentos, espera 5 min")
    data = await request.post()
    if hmac.compare_digest(str(data.get("password", "")), PASSWORD):
        resp = web.HTTPFound("/")
        resp.set_cookie("rd", make_token(), httponly=True, secure=True, samesite="Strict", max_age=12 * 3600)
        return resp
    recent.append(now)
    _fails[ip] = recent
    raise web.HTTPFound("/login?error=1")


async def index(request):
    return web.FileResponse(STATIC / "index.html")


def capture(sct, mon):
    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, int(img.height * MAX_WIDTH / img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=QUALITY)
    return buf.getvalue()


KEYMAP = {
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "Delete": "delete", "Home": "home", "End": "end", "PageUp": "pageup",
    "PageDown": "pagedown", "Control": "ctrl", "Shift": "shift", "Alt": "alt",
    "Meta": "win" if sys.platform == "win32" else "command", " ": "space",
    "CapsLock": "capslock", "Insert": "insert",
}


def apply_event(ev: dict, mon: dict):
    t = ev.get("t")
    if t in ("move", "down", "up", "scroll", "dbl"):
        x = mon["left"] + int(ev["x"] * mon["width"])
        y = mon["top"] + int(ev["y"] * mon["height"])
        btn = {0: "left", 1: "middle", 2: "right"}.get(ev.get("b", 0), "left")
        if t == "move":
            pyautogui.moveTo(x, y)
        elif t == "down":
            pyautogui.mouseDown(x, y, button=btn)
        elif t == "up":
            pyautogui.mouseUp(x, y, button=btn)
        elif t == "dbl":
            pyautogui.doubleClick(x, y, button=btn)
        elif t == "scroll":
            pyautogui.scroll(-int(ev.get("dy", 0)) // 4 or (-1 if ev.get("dy", 0) > 0 else 1), x, y)
    elif t in ("kdown", "kup"):
        k = ev.get("k", "")
        k = KEYMAP.get(k, k.lower() if len(k) == 1 else k.lower())
        if k in pyautogui.KEYBOARD_KEYS:
            (pyautogui.keyDown if t == "kdown" else pyautogui.keyUp)(k)
    elif t == "text":
        pyautogui.write(ev.get("s", "")[:500])


async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(max_msg_size=1 << 16)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    with mss.mss() as sct:
        mon = dict(sct.monitors[1])

        async def sender():
            while not ws.closed:
                start = time.time()
                frame = await loop.run_in_executor(None, capture, sct, mon)
                await ws.send_bytes(frame)
                await asyncio.sleep(max(0, 1 / FPS - (time.time() - start)))

        task = asyncio.create_task(sender())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        await loop.run_in_executor(None, apply_event, json.loads(msg.data), mon)
                    except Exception as e:  # evento inválido no debe tumbar la sesión
                        print("evento ignorado:", e)
        finally:
            task.cancel()
    return ws


def publish() -> None:
    """Publica con HTTPS en el tailnet vía `tailscale serve` y muestra la URL."""
    import shutil
    import subprocess
    ts = shutil.which("tailscale") or next(
        (p for p in (r"C:\Program Files\Tailscale\tailscale.exe",
                     "/Applications/Tailscale.app/Contents/MacOS/Tailscale") if os.path.exists(p)), None)
    if not ts:
        print("No encontré Tailscale; instálalo y ejecuta: tailscale serve --bg https / http://127.0.0.1:%d" % PORT)
        return
    r = subprocess.run([ts, "serve", "--bg", "--https=443", f"http://127.0.0.1:{PORT}"],
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())


def main():
    if PASSWORD and len(PASSWORD) < 8:
        sys.exit("RD_PASSWORD debe tener mínimo 8 caracteres (o déjala vacía para entrar sin contraseña).")
    publish()
    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", index), web.get("/ws", ws_handler),
        web.route("*", "/login", login),
    ])
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
