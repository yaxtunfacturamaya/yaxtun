"""Retroceder PC: programa nativo (solo biblioteca estándar de Python, sin servicios externos).

Corre en la PC con Windows, se configura solo (Restaurar sistema, firewall, inicio automático),
crea un punto de restauración cada hora y sirve un panel en tu red local (WiFi) para que desde
el Android restaures la PC a como estaba hace 10 horas, al instante o con temporizador.
"""
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

IS_WIN = sys.platform == "win32"
FAKE = os.environ.get("RETRO_FAKE") == "1"  # solo para pruebas fuera de Windows
HOURS = float(os.environ.get("RETRO_HORAS", "10"))
PORT = int(os.environ.get("RETRO_PUERTO", "8780"))
CONF_DIR = Path(os.environ.get("PROGRAMDATA", Path.home())) / "RetrocederPC"
CONF = CONF_DIR / "config.json"
TASK = "RetrocederPC"
SECRET = secrets.token_bytes(32)
_lock = threading.Lock()
_timer = {"at": None, "t": None}
_fails: dict[str, list[float]] = {}


# ---------- Windows nativo ----------
def ps(cmd: str) -> tuple[int, str]:
    if FAKE:
        return fake(cmd)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                       capture_output=True, text=True, creationflags=0x08000000 if IS_WIN else 0)
    return r.returncode, (r.stdout + r.stderr).strip()


_fake_points = [{"numero": 1, "fecha": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 11 * 3600)),
                 "descripcion": "demo"}]


def fake(cmd):
    if "Restore-Computer" in cmd:
        return 0, "FAKE: reiniciando"
    if "Checkpoint-Computer" in cmd:
        _fake_points.append({"numero": len(_fake_points) + 1, "fecha": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "descripcion": "demo"})
    return 0, "ok"


def is_admin() -> bool:
    if FAKE or not IS_WIN:
        return True
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def relaunch_as_admin():
    exe = sys.executable
    args = " ".join(f'"{a}"' for a in ([] if getattr(sys, "frozen", False) else [sys.argv[0]]) + sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
    sys.exit(0)


def list_points() -> list[dict]:
    if FAKE:
        return list(_fake_points)
    code, out = ps("$ErrorActionPreference='Stop'; $p=@(Get-ComputerRestorePoint | ForEach-Object { @{ numero=$_.SequenceNumber; "
                   "fecha=[Management.ManagementDateTimeConverter]::ToDateTime($_.CreationTime).ToString('s'); "
                   "descripcion=$_.Description } }); ConvertTo-Json -InputObject $p -Compress")
    if code != 0:
        raise RuntimeError(out or "No se pudo leer Restaurar sistema")
    data = json.loads(out or "[]")
    data = [data] if isinstance(data, dict) else data
    return sorted(data, key=lambda p: p["fecha"])


def target_point():
    limit = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - HOURS * 3600))
    ok = [p for p in list_points() if p["fecha"] <= limit]
    return ok[-1] if ok else None


def create_point() -> str:
    # Windows por defecto limita a 1 punto cada 24 h; lo quitamos para poder crear uno por hora.
    code, out = ps(r"Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore' "
                   r"SystemRestorePointCreationFrequency 0 -Type DWord; "
                   "Checkpoint-Computer -Description ('Retroceder ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')) "
                   "-RestorePointType MODIFY_SETTINGS")
    if code != 0:
        raise RuntimeError(out)
    return "Punto de restauración creado"


def restore() -> str:
    p = target_point()
    if not p:
        raise RuntimeError(f"Aún no hay un punto de restauración de hace {HOURS:g} horas o más antiguo.")
    try:
        create_point()  # punto de seguridad para poder deshacer el retroceso
    except Exception:
        pass
    code, out = ps(f"Restore-Computer -RestorePoint {int(p['numero'])} -Confirm:$false")
    if code != 0:
        raise RuntimeError(out)
    return f"Restaurando al punto del {p['fecha'].replace('T', ' ')}. La PC se reiniciará."


def setup_system():
    """Configura todo lo necesario en la PC (idempotente)."""
    if FAKE or not IS_WIN:
        return
    ps('Enable-ComputerRestore -Drive "$env:SystemDrive\\"')
    subprocess.run(["vssadmin", "resize", "shadowstorage", f"/for={os.environ.get('SystemDrive', 'C:')}",
                    f"/on={os.environ.get('SystemDrive', 'C:')}", "/maxsize=10%"], capture_output=True)
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={TASK}"], capture_output=True)
    subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={TASK}", "dir=in", "action=allow",
                    "protocol=TCP", f"localport={PORT}", "profile=private,domain"], capture_output=True)
    cmd = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{Path(sys.argv[0]).resolve()}"'
    subprocess.run(["schtasks", "/create", "/f", "/tn", TASK, "/sc", "onlogon", "/rl", "highest", "/tr", cmd],
                   capture_output=True)


def uninstall_system():
    if FAKE or not IS_WIN:
        return
    subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK], capture_output=True)
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={TASK}"], capture_output=True)


# ---------- hilos internos (sin tareas externas) ----------
def hourly_points():
    while True:
        try:
            pts = list_points()
            if not pts or time.time() - time.mktime(time.strptime(pts[-1]["fecha"], "%Y-%m-%dT%H:%M:%S")) >= 3500:
                create_point()
        except Exception as e:
            print("punto horario falló:", e)
        time.sleep(600)


def _timer_run(sec):
    time.sleep(sec)
    with _lock:
        if _timer["t"] is not threading.current_thread():
            return
        _timer["at"] = _timer["t"] = None
    try:
        print(restore())
    except Exception as e:
        print("restauración por temporizador falló:", e)


def set_timer(minutes):
    with _lock:
        _timer["at"] = _timer["t"] = None
        if minutes:
            t = threading.Thread(target=_timer_run, args=(minutes * 60,), daemon=True)
            _timer["t"], _timer["at"] = t, int(time.time() + minutes * 60)
            t.start()
    return _timer["at"]


# ---------- servidor web ----------
def load_conf() -> dict:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if CONF.exists():
        return json.loads(CONF.read_text())
    conf = {"password": secrets.token_urlsafe(9)}
    CONF.write_text(json.dumps(conf))
    return conf


def make_token(pw: str) -> str:
    exp = str(int(time.time()) + 12 * 3600)
    return f"{exp}.{hmac.new(SECRET, (exp + pw).encode(), hashlib.sha256).hexdigest()}"


def valid_token(tok, pw) -> bool:
    try:
        exp, sig = (tok or "").split(".")
        good = hmac.new(SECRET, (exp + pw).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except ValueError:
        return False


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no envía nada; solo elige la interfaz local
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def is_private(ip: str) -> bool:
    # El panel solo responde a la red local; nunca a internet aunque abras el router.
    import ipaddress
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
        return a.is_private or a.is_loopback or a.is_link_local
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    conf: dict = {}

    def log_message(self, *a):
        pass

    def send_body(self, code, body, ctype="application/json", headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def authed(self) -> bool:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "rp" in c and valid_token(c["rp"].value, self.conf["password"])

    def guard(self) -> bool:
        if not is_private(self.client_address[0]):
            self.send_body(403, "Solo red local", "text/plain")
            return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        path = self.path.split("?")[0]
        if path == "/manifest.json":
            return self.send_body(200, MANIFEST, "application/manifest+json")
        if not self.authed():
            return self.send_body(200, LOGIN.replace("%ERR%", "Contraseña incorrecta" if "error" in self.path else ""), "text/html")
        if path == "/":
            return self.send_body(200, PAGE, "text/html")
        if path == "/api/estado":
            try:
                pts = list_points()
                t = target_point()
                return self.send_body(200, json.dumps({"horas": HOURS, "puntos": len(pts), "objetivo": t,
                                                       "temporizador": _timer["at"], "admin": is_admin()}))
            except Exception as e:
                return self.send_body(500, json.dumps({"error": str(e)}))
        self.send_body(404, "no", "text/plain")

    def do_POST(self):
        if not self.guard():
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 4096)).decode()
        if self.path == "/login":
            ip = self.client_address[0]
            now = time.time()
            recent = [t for t in _fails.get(ip, []) if now - t < 300]
            if len(recent) >= 5:
                return self.send_body(429, "Demasiados intentos, espera 5 min", "text/plain")
            pw = parse_qs(raw).get("password", [""])[0]
            if hmac.compare_digest(pw, self.conf["password"]):
                return self.send_body(303, "", "text/plain", [
                    ("Location", "/"), ("Set-Cookie", f"rp={make_token(pw)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")])
            _fails[ip] = recent + [now]
            return self.send_body(303, "", "text/plain", [("Location", "/?error=1")])
        if not self.authed():
            return self.send_body(401, "{}")
        if self.headers.get("X-Requested-With") != "retroceder":  # anti-CSRF
            return self.send_body(403, "{}")
        try:
            if self.path == "/api/ejecutar":
                msg = restore()
            elif self.path == "/api/crear":
                msg = create_point()
            elif self.path == "/api/temporizador":
                m = float(json.loads(raw or "{}").get("minutos", 0))
                if m and not 1 <= m <= 1440:
                    return self.send_body(400, json.dumps({"error": "minutos entre 1 y 1440"}))
                return self.send_body(200, json.dumps({"ok": True, "temporizador": set_timer(m)}))
            else:
                return self.send_body(404, "{}")
            self.send_body(200, json.dumps({"ok": True, "msg": msg}))
        except Exception as e:
            self.send_body(500, json.dumps({"error": str(e)}))


MANIFEST = json.dumps({"name": "Retroceder PC", "short_name": "Retroceder", "start_url": "/", "display": "standalone",
                       "background_color": "#0f172a", "theme_color": "#0f172a"})
LOGIN = """<!doctype html><html lang=es><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Retroceder PC</title><body style="font:16px system-ui;background:#0f172a;color:#e2e8f0;padding:24px;max-width:420px;margin:auto">
<h2>⏪ Retroceder PC</h2><form method=post action=/login><input name=password type=password placeholder=Contraseña autofocus
style="width:100%;padding:14px;border-radius:8px;border:0;font-size:16px;box-sizing:border-box"><p style=color:#f87171>%ERR%</p>
<button style="width:100%;padding:14px;border:0;border-radius:10px;background:#0284c7;color:#fff;font-size:17px">Entrar</button></form></body></html>"""
PAGE = """<!doctype html><html lang=es><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Retroceder PC</title><link rel=manifest href=/manifest.json><meta name=theme-color content="#0f172a">
<style>body{margin:0;font:16px system-ui;background:#0f172a;color:#e2e8f0;padding:16px;max-width:480px;margin:auto}
h1{font-size:22px}button{width:100%;padding:16px;margin:8px 0;border:0;border-radius:12px;font-size:17px;background:#334155;color:#fff}
button.big{background:#dc2626;font-weight:700}button.ok{background:#0284c7}.card{background:#1e293b;border-radius:12px;padding:12px;margin:12px 0}
small{color:#94a3b8}input{padding:12px;border-radius:8px;border:0;width:90px;font-size:16px}#msg{white-space:pre-wrap;color:#fbbf24}</style></head><body>
<h1>⏪ Retroceder PC</h1><div class=card id=estado>Cargando…</div>
<button class=big id=ahora>Restaurar a hace <span id=h>10</span> h (ahora)</button>
<div class=card><b>Temporizador</b><br><small>Restaura automáticamente después de:</small><br>
<input id=min type=number value=60 min=1> minutos<button class=ok id=prog>Iniciar temporizador</button>
<button id=canc hidden>Cancelar temporizador</button><div id=cuenta></div></div>
<button id=crear>Crear punto de restauración ahora</button><div id=msg></div>
<small>Para tener un icono: menú ⋮ de Chrome → «Añadir a pantalla de inicio».</small>
<script>
const H={"X-Requested-With":"retroceder","Content-Type":"application/json"},$=i=>document.getElementById(i);let tmp=null;
async function api(u,m="GET",b){const r=await fetch(u,{method:m,headers:H,body:b?JSON.stringify(b):undefined});
 try{return await r.json()}catch{return{error:"Sin respuesta"}}}
async function cargar(){const d=await api("/api/estado");if(d.error){$("estado").textContent=d.error;return}
 $("h").textContent=d.horas;const t=d.objetivo;
 $("estado").innerHTML=(t?`Se restaurará al punto del <b>${t.fecha.replace("T"," ")}</b>`:`⚠ Aún no hay un punto de hace ${d.horas} h. El programa crea uno cada hora; vuelve más tarde.`)+`<br><small>${d.puntos} puntos disponibles</small>`;
 tmp=d.temporizador;pintar()}
function pintar(){$("canc").hidden=!tmp;if(!tmp){$("cuenta").textContent="";return}
 const s=Math.max(0,tmp-Math.floor(Date.now()/1000));$("cuenta").textContent=`Restaura en ${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`}
setInterval(pintar,1000);
$("ahora").onclick=async()=>{if(!confirm("¿Restaurar la PC? Se reiniciará."))return;$("msg").textContent="Restaurando…";const r=await api("/api/ejecutar","POST");$("msg").textContent=r.msg||r.error||""};
$("prog").onclick=async()=>{const r=await api("/api/temporizador","POST",{minutos:+$("min").value});tmp=r.temporizador;pintar()};
$("canc").onclick=async()=>{await api("/api/temporizador","POST",{minutos:0});tmp=null;pintar()};
$("crear").onclick=async()=>{const r=await api("/api/crear","POST");$("msg").textContent=r.msg||r.error||"";cargar()};
cargar();</script></body></html>"""


def main():
    if "--desinstalar" in sys.argv:
        uninstall_system()
        print("Inicio automático y regla de firewall eliminados.")
        return
    if IS_WIN and not is_admin():
        relaunch_as_admin()
    conf = load_conf()
    setup_system()
    Handler.conf = conf
    threading.Thread(target=hourly_points, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"El puerto {PORT} está ocupado (¿ya está corriendo?). Usa RETRO_PUERTO para cambiarlo.")
        input("Enter para salir")
        return
    print("=== Retroceder PC ===")
    print(f"En el Android (misma WiFi) abre:  http://{lan_ip()}:{PORT}")
    print(f"Contraseña: {conf['password']}   (guardada en {CONF})")
    print(f"Crea un punto de restauración cada hora y restaura a hace {HOURS:g} h desde el panel.")
    print("Deja esta ventana abierta (o se iniciará sola al entrar a Windows).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
