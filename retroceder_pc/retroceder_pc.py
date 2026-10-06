"""Retroceder PC: programa nativo (solo biblioteca estándar de Python, sin servicios externos).

Corre en la PC con Windows, se configura solo (Restaurar sistema, firewall, inicio automático),
crea un punto de restauración cada hora y sirve un panel en tu red local (WiFi) para que desde
el Android restaures la PC a como estaba hace 10 horas, al instante o con temporizador.
"""
import ctypes
import datetime
import hashlib
import ipaddress
import ssl
import struct
import zlib
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
PORT = int(os.environ.get("RETRO_PUERTO", "8780"))  # HTTPS; PORT+1 = página para instalar el certificado
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


def target_point(hours=None):
    limit = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - (HOURS if hours is None else hours) * 3600))
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


def restore(hours=None, number=None) -> str:
    if number is not None:
        p = next((p for p in list_points() if p["numero"] == number), None)
        if not p:
            raise RuntimeError("Ese punto de restauración ya no existe.")
    else:
        p = target_point(hours)
    if not p:
        raise RuntimeError(f"Aún no hay un punto de restauración de hace {HOURS if hours is None else hours:g} horas o más antiguo.")
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
                    "protocol=TCP", f"localport={PORT}-{PORT + 1}", "profile=private,domain"], capture_output=True)
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


def _timer_run(sec, hours, number):
    time.sleep(sec)
    with _lock:
        if _timer["t"] is not threading.current_thread():
            return
        _timer["at"] = _timer["t"] = None
    try:
        print(restore(hours, number))
    except Exception as e:
        print("restauración por temporizador falló:", e)


def set_timer(minutes, hours=None, number=None):
    with _lock:
        _timer["at"] = _timer["t"] = None
        if minutes:
            t = threading.Thread(target=_timer_run, args=(minutes * 60, hours, number), daemon=True)
            _timer["t"], _timer["at"] = t, int(time.time() + minutes * 60)
            t.start()
    return _timer["at"]


# ---------- HTTPS propio (autoridad local, sin proveedores) ----------
def ensure_certs(ip: str):
    """Crea una autoridad local (CA) una vez y un certificado del servidor para esta IP/nombre.
    Instalas la CA una vez en el Android y el candado queda verde."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    ca_key_f, ca_f = CONF_DIR / "ca.key", CONF_DIR / "ca.crt"
    srv_key_f, srv_f = CONF_DIR / "server.key", CONF_DIR / "server.crt"
    pem = serialization.Encoding.PEM
    nopw = serialization.NoEncryption()
    fmt = serialization.PrivateFormat.TraditionalOpenSSL
    now = datetime.datetime.now(datetime.timezone.utc)
    if not (ca_key_f.exists() and ca_f.exists()):
        k = rsa.generate_private_key(65537, 2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Retroceder PC (autoridad local)")])
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=3650))
             .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
             .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
             .sign(k, hashes.SHA256()))
        ca_key_f.write_bytes(k.private_bytes(pem, fmt, nopw))
        ca_f.write_bytes(c.public_bytes(pem))
    ca_key = serialization.load_pem_private_key(ca_key_f.read_bytes(), None)
    ca = x509.load_pem_x509_certificate(ca_f.read_bytes())
    names = [x509.IPAddress(ipaddress.ip_address(ip)), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
             x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]
    if srv_f.exists() and srv_key_f.exists():  # reusar si sigue válido para esta IP
        old = x509.load_pem_x509_certificate(srv_f.read_bytes())
        try:
            have = old.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
        except x509.ExtensionNotFound:
            have = []
        if ipaddress.ip_address(ip) in have and old.not_valid_after_utc > now + datetime.timedelta(days=30):
            return srv_f, srv_key_f, ca_f
    k = rsa.generate_private_key(65537, 2048)
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Retroceder PC")]))
         .issuer_name(ca.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=800))
         .add_extension(x509.SubjectAlternativeName(names), critical=False)
         .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
         .sign(ca_key, hashes.SHA256()))
    srv_key_f.write_bytes(k.private_bytes(pem, fmt, nopw))
    srv_f.write_bytes(c.public_bytes(pem))
    return srv_f, srv_key_f, ca_f


def make_icon(size: int) -> bytes:
    """PNG de un reloj con flecha atrás, hecho a mano (sin librerías)."""
    import math
    bg, fg = (15, 23, 42), (56, 189, 248)
    c = size / 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - c, y - c
            r = math.hypot(dx, dy) / size
            on = 0.30 < r < 0.37  # aro
            on = on or (abs(dx) < size * 0.025 and -size * 0.25 < dy < 0)  # aguja larga
            on = on or (abs(dy - dx * 0.0) < size * 0.025 and 0 < dx < size * 0.17)  # aguja corta
            row += bytes(fg if on else bg)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


ICONS = {}


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
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
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
        if path == "/sw.js":  # service worker mínimo: lo exige Chrome para «Instalar app»
            return self.send_body(200, "self.addEventListener('fetch',()=>{});", "text/javascript")
        if path in ("/icon-192.png", "/icon-512.png"):
            n = 192 if "192" in path else 512
            return self.send_body(200, ICONS.setdefault(n, make_icon(n)), "image/png")
        if path == "/ca.crt":
            return self.send_body(200, (CONF_DIR / "ca.crt").read_bytes(), "application/x-x509-ca-cert")
        if not self.authed():
            return self.send_body(200, LOGIN.replace("%ERR%", "Contraseña incorrecta" if "error" in self.path else ""), "text/html")
        if path == "/":
            return self.send_body(200, PAGE, "text/html")
        if path == "/api/estado":
            try:
                q = parse_qs(self.path.partition("?")[2])
                h = float(q.get("horas", [HOURS])[0])
                pts = list_points()
                return self.send_body(200, json.dumps({"horas": h, "puntos": pts[-30:], "objetivo": target_point(h),
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
                    ("Location", "/"), ("Set-Cookie", f"rp={make_token(pw)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200")])
            _fails[ip] = recent + [now]
            return self.send_body(303, "", "text/plain", [("Location", "/?error=1")])
        if not self.authed():
            return self.send_body(401, "{}")
        if self.headers.get("X-Requested-With") != "retroceder":  # anti-CSRF
            return self.send_body(403, "{}")
        try:
            body = json.loads(raw or "{}")
            if self.path == "/api/ejecutar":
                msg = restore(float(body.get("horas", HOURS)), body.get("numero"))
            elif self.path == "/api/crear":
                msg = create_point()
            elif self.path == "/api/temporizador":
                m = float(body.get("minutos", 0))
                if m and not 1 <= m <= 1440:
                    return self.send_body(400, json.dumps({"error": "minutos entre 1 y 1440"}))
                return self.send_body(200, json.dumps({"ok": True, "temporizador": set_timer(m, float(body.get("horas", HOURS)), body.get("numero"))}))
            else:
                return self.send_body(404, "{}")
            self.send_body(200, json.dumps({"ok": True, "msg": msg}))
        except Exception as e:
            self.send_body(500, json.dumps({"error": str(e)}))


MANIFEST = json.dumps({"name": "Retroceder PC", "short_name": "Retroceder", "start_url": "/", "scope": "/",
                       "display": "standalone", "background_color": "#0f172a", "theme_color": "#0f172a",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]})
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
small{color:#94a3b8}input{padding:12px;border-radius:8px;border:0;width:90px;font-size:16px}#msg{white-space:pre-wrap;color:#fbbf24}
.pt{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid #334155;font-size:14px}
.pt button{width:auto;padding:8px 12px;margin:0;font-size:14px;background:#7f1d1d}#lista{max-height:240px;overflow:auto}</style></head><body>
<div id=ov hidden style="position:fixed;inset:0;background:#0f172af2;z-index:9;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px"><h1 id=ovt></h1><p id=ovs></p></div>
<h1>⏪ Retroceder PC</h1>
<div class=card><b>¿Cuánto retroceder?</b><br><input id=horas type=number value=10 min=0.5 step=0.5> horas atrás
<div id=estado style="margin-top:8px">Cargando…</div></div>
<button class=big id=ahora>Restaurar ahora</button>
<div class=card><b>Temporizador</b><br><small>Restaura solo (con las horas de arriba) dentro de:</small><br>
<input id=min type=number value=60 min=1> minutos<button class=ok id=prog>Iniciar temporizador</button>
<button id=canc hidden>Cancelar temporizador</button><div id=cuenta></div></div>
<div class=card><b>Puntos disponibles</b><br><small>«Ir aquí» restaura y reinicia al instante, sin preguntar.</small><div id=lista></div></div>
<button id=crear>Crear punto de restauración ahora</button><div id=msg></div>
<script>
const H={"X-Requested-With":"retroceder","Content-Type":"application/json"},$=i=>document.getElementById(i);let tmp=null,hz=null;
const hrs=()=>+$("horas").value||10;
async function api(u,m="GET",b){const r=await fetch(u,{method:m,headers:H,body:b?JSON.stringify(b):undefined});
 try{return await r.json()}catch{return{error:"Sin respuesta (¿sesión vencida? recarga)"}}}
async function cargar(){const d=await api("/api/estado?horas="+hrs());if(d.error){$("estado").textContent=d.error;return}
 const t=d.objetivo;
 $("estado").innerHTML=t?`Se restaurará al punto del <b>${t.fecha.replace("T"," ")}</b>`:`⚠ No hay un punto de hace ${hrs()} h todavía. El programa crea uno cada hora.`;
 $("lista").innerHTML=d.puntos.slice().reverse().map(p=>`<div class=pt><span>${p.fecha.replace("T"," ")}</span><button data-n=${p.numero}>Ir aquí</button></div>`).join("")||"Ninguno todavía";
 tmp=d.temporizador;pintar()}
function pintar(){$("canc").hidden=!tmp;if(!tmp){$("cuenta").textContent="";return}
 const s=Math.max(0,tmp-Math.floor(Date.now()/1000));$("cuenta").textContent=`Restaura en ${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`}
async function ejecutar(b){const r=await api("/api/ejecutar","POST",b);
 if(r.error){$("msg").textContent=r.error;return}
 reiniciando(r.msg)}
function reiniciando(msg){const o=$("ov");o.hidden=false;$("ovt").textContent="🔄 PC reiniciando…";$("ovs").textContent=msg||"";
 let caido=false;const t=setInterval(async()=>{try{const c=new AbortController();setTimeout(()=>c.abort(),3000);
  const r=await fetch("/api/estado",{signal:c.signal,headers:H});if(caido&&r.ok){clearInterval(t);$("ovt").textContent="✅ PC en línea de nuevo";
  $("ovs").textContent="Restauración completada.";setTimeout(()=>{o.hidden=true;cargar()},2500)}}catch{caido=true;$("ovt").textContent="🔄 PC reiniciando…";$("ovs").textContent="Restaurando y reiniciando. Esta pantalla avisa cuando vuelva."}},3000)}
setInterval(pintar,1000);setInterval(cargar,5000);$("horas").oninput=()=>{clearTimeout(hz);hz=setTimeout(cargar,400)};
$("ahora").onclick=()=>ejecutar({horas:hrs()});
$("lista").onclick=e=>{const n=e.target.dataset.n;if(n)ejecutar({numero:+n})};
$("prog").onclick=async()=>{const r=await api("/api/temporizador","POST",{minutos:+$("min").value,horas:hrs()});tmp=r.temporizador;pintar()};
$("canc").onclick=async()=>{await api("/api/temporizador","POST",{minutos:0});tmp=null;pintar()};
$("crear").onclick=async()=>{const r=await api("/api/crear","POST");$("msg").textContent=r.msg||r.error||"";cargar()};
if("serviceWorker"in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});
cargar();</script></body></html>"""

INFO = """<!doctype html><html lang=es><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Retroceder PC - instalar</title><body style="font:16px system-ui;background:#0f172a;color:#e2e8f0;padding:20px;max-width:460px;margin:auto">
<h2>⏪ Retroceder PC</h2><p><b>Paso 1 (una sola vez):</b> descarga el certificado para que tu Android confíe en esta PC.</p>
<p><a href=/ca.crt style="display:block;padding:14px;background:#0284c7;color:#fff;border-radius:10px;text-align:center;text-decoration:none">Descargar certificado</a></p>
<p>Luego: Ajustes → Seguridad (o «Seguridad y privacidad») → Más seguridad → Cifrado y credenciales → <i>Instalar un certificado</i> → <i>Certificado de CA</i> → elige el archivo descargado.</p>
<p><b>Paso 2:</b> abre la app segura:</p>
<p><a href="%URL%" style="display:block;padding:14px;background:#16a34a;color:#fff;border-radius:10px;text-align:center;text-decoration:none">%URL%</a></p>
<p>Escribe la contraseña y en Chrome toca ⋮ → <b>Instalar app</b>. Queda como una app en tu pantalla de inicio.</p></body></html>"""


class InfoHandler(BaseHTTPRequestHandler):
    """Puerto HTTP aparte, solo para bajar el certificado e ir a la versión HTTPS."""
    url = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not is_private(self.client_address[0]):
            self.send_response(403); self.end_headers(); return
        if self.path == "/ca.crt":
            body, ctype = (CONF_DIR / "ca.crt").read_bytes(), "application/x-x509-ca-cert"
        else:
            body, ctype = INFO.replace("%URL%", self.url).encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    if "--desinstalar" in sys.argv:
        uninstall_system()
        print("Inicio automático y regla de firewall eliminados.")
        return
    if IS_WIN and not is_admin():
        relaunch_as_admin()
    conf = load_conf()
    setup_system()
    ip = lan_ip()
    cert, key, _ = ensure_certs(ip)
    Handler.conf = conf
    threading.Thread(target=hourly_points, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        info = ThreadingHTTPServer(("0.0.0.0", PORT + 1), InfoHandler)
    except OSError:
        print(f"El puerto {PORT} o {PORT + 1} está ocupado (¿ya está corriendo?). Usa RETRO_PUERTO para cambiarlo.")
        input("Enter para salir")
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    InfoHandler.url = f"https://{ip}:{PORT}"
    threading.Thread(target=info.serve_forever, daemon=True).start()
    print("=== Retroceder PC ===")
    print(f"1) Primera vez, en el Android (mismo WiFi): abre  http://{ip}:{PORT + 1}  e instala el certificado.")
    print(f"2) Después abre  https://{ip}:{PORT}  e instálala como app (Chrome ⋮ → Instalar app).")
    print(f"Contraseña: {conf['password']}   (guardada en {CONF})")
    print("Crea un punto de restauración cada hora. Deja esta ventana abierta (o inicia sola con Windows).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
