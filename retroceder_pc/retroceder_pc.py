"""Retroceder PC: programa nativo (solo biblioteca estándar de Python, sin servicios externos).

Corre en la PC con Windows, se configura solo (Restaurar sistema, firewall, inicio automático),
crea un punto de restauración cada hora y sirve un panel en tu red local (WiFi) para que desde
el Android restaures la PC a como estaba hace 10 horas, al instante o con temporizador.
"""
import ctypes
import datetime
import re
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
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       creationflags=0x08000000 if IS_WIN else 0)
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
    threading.Thread(target=save_snapshot, daemon=True).start()
    return "Punto de restauración creado"


def restore(hours=None, number=None, keep=None) -> str:
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
    kept = save_keep(keep)
    code, out = ps(f"Restore-Computer -RestorePoint {int(p['numero'])} -Confirm:$false")
    if code != 0:
        KEEP_F.unlink(missing_ok=True)
        raise RuntimeError(out)
    extra = f" Se volverán a aplicar {kept} ajustes que querías conservar." if kept else ""
    return f"Restaurando al punto del {p['fecha'].replace('T', ' ')}. La PC se reiniciará.{extra}"


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


# ---------- fotos del estado de la PC (para la comparativa) ----------
SNAPS = CONF_DIR / "snaps"
# categoría: (nombre, ¿lo revierte Restaurar sistema?). Restaurar sistema devuelve el sistema (HKLM),
# programas, controladores y servicios; NO la configuración de tu usuario (tema, fondo, proxy...) ni tus archivos.
CATS = {
    "programas": ("Programas instalados", True), "actualizaciones": ("Actualizaciones de Windows", True),
    "controladores": ("Controladores", True), "servicios": ("Servicios (tipo de inicio)", True),
    "tareas": ("Tareas programadas", True), "inicio_sistema": ("Inicio con Windows (sistema)", True),
    "firewall": ("Firewall", True), "sistema": ("Ajustes del sistema", True),
    "programas_usuario": ("Programas de usuario", False), "inicio_usuario": ("Inicio con Windows (tu usuario)", False),
    "apariencia": ("Apariencia y navegador (tu usuario)", False),
}
SNAP_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$o=@{}
$u=@{}
foreach($p in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'){Get-ItemProperty $p|?{$_.DisplayName -and -not $_.SystemComponent}|%{$u[$_.DisplayName]="$($_.DisplayVersion)"}}
$o.programas=$u
$u=@{};Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'|?{$_.DisplayName}|%{$u[$_.DisplayName]="$($_.DisplayVersion)"};$o.programas_usuario=$u
$u=@{};Get-HotFix|%{$u[$_.HotFixID]="$($_.InstalledOn)"};$o.actualizaciones=$u
$u=@{};Get-CimInstance Win32_PnPSignedDriver|?{$_.DeviceName}|%{$u[$_.DeviceName]="$($_.DriverVersion)"};$o.controladores=$u
$u=@{};Get-CimInstance Win32_Service|%{$u[$_.Name]="$($_.StartMode)"};$o.servicios=$u
$u=@{};Get-ScheduledTask|%{$u[$_.TaskPath+$_.TaskName]=$(if($_.State -eq 'Disabled'){'Desactivada'}else{'Activa'})};$o.tareas=$u
$u=@{};foreach($p in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'){$k=Get-Item $p;if($k){foreach($n in $k.GetValueNames()){$u[$n]=[string]$k.GetValue($n)}}};$o.inicio_sistema=$u
$u=@{};$k=Get-Item 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run';if($k){foreach($n in $k.GetValueNames()){$u[$n]=[string]$k.GetValue($n)}};$o.inicio_usuario=$u
$u=@{};Get-NetFirewallProfile|%{$u[[string]$_.Name]=$(if($_.Enabled){'Activado'}else{'Desactivado'})};$o.firewall=$u
$u=@{};$u['Plan de energía']=((powercfg /getactivescheme) -join ' ') -replace '^.*:\s*','';$u['Zona horaria']=(Get-TimeZone).Id;$u['Nombre del equipo']=$env:COMPUTERNAME;$u['PATH del sistema']=[Environment]::GetEnvironmentVariable('Path','Machine');$o.sistema=$u
$u=@{};$c='HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize'
$u['Tema de apps']=$(if((Get-ItemProperty $c).AppsUseLightTheme -eq 0){'Oscuro'}else{'Claro'})
$u['Tema del sistema']=$(if((Get-ItemProperty $c).SystemUsesLightTheme -eq 0){'Oscuro'}else{'Claro'})
$u['Fondo de pantalla']=[string](Get-ItemProperty 'HKCU:\Control Panel\Desktop').WallPaper
$i=Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings'
$u['Proxy']=$(if($i.ProxyEnable -eq 1){[string]$i.ProxyServer}else{'Sin proxy'})
$u['Navegador predeterminado']=[string](Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice').ProgId
$v=Get-CimInstance Win32_VideoController|Select-Object -First 1;$u['Resolución']="$($v.CurrentHorizontalResolution)x$($v.CurrentVerticalResolution)"
$o.apariencia=$u
ConvertTo-Json -InputObject $o -Depth 3 -Compress
"""


def take_snapshot() -> dict:
    if FAKE:
        d = {"programas": {"Chrome": "126", "7-Zip": "23"}, "servicios": {"Spooler": "Auto"},
             "apariencia": {"Tema de apps": "Claro", "Fondo de pantalla": "C:/fondo1.jpg"}}
        if (CONF_DIR / "fake_changed").exists():
            d["programas"]["Zoom"] = "6.0"
            d["programas"].pop("7-Zip")
            d["servicios"]["Spooler"] = "Disabled"
            d["apariencia"]["Tema de apps"] = "Oscuro"
        return d
    code, out = ps(SNAP_PS)
    if code != 0 or not out.startswith("{"):
        raise RuntimeError(out or "No se pudo leer el estado de la PC")
    return json.loads(out)


def _png(w, h, rgb) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    rows = b"".join(b"\0" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 3)) + chunk(b"IEND", b""))


def grab_screen_png(max_w=640) -> bytes:
    """Captura de pantalla reducida, solo con ctypes/GDI (sin librerías)."""
    if FAKE or not IS_WIN:
        return make_icon(192)
    from ctypes import wintypes as wt
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    vp = ctypes.c_void_p
    u.GetDC.restype = vp; u.GetDC.argtypes = [vp]; u.ReleaseDC.argtypes = [vp, vp]
    g.CreateCompatibleDC.restype = vp; g.CreateCompatibleDC.argtypes = [vp]
    g.CreateCompatibleBitmap.restype = vp; g.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = vp; g.SelectObject.argtypes = [vp, vp]
    g.StretchBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, vp,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
    g.GetDIBits.argtypes = [vp, vp, wt.UINT, wt.UINT, vp, vp, wt.UINT]
    g.DeleteObject.argtypes = [vp]; g.DeleteDC.argtypes = [vp]; g.SetStretchBltMode.argtypes = [vp, ctypes.c_int]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("x", wt.LONG), ("y", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
    u.SetProcessDPIAware()
    w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    tw = min(max_w, w)
    th = max(1, int(h * tw / w))
    hdc = u.GetDC(None)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, tw, th)
    old = g.SelectObject(mdc, bmp)
    g.SetStretchBltMode(mdc, 4)  # HALFTONE: reducción suave
    g.StretchBlt(mdc, 0, 0, tw, th, hdc, 0, 0, w, h, 0x00CC0020)
    bih = BIH(ctypes.sizeof(BIH), tw, -th, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(tw * th * 4)
    g.GetDIBits(mdc, bmp, 0, th, buf, ctypes.byref(bih), 0)
    g.SelectObject(mdc, old); g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(None, hdc)
    raw = buf.raw
    rgb = bytearray(tw * th * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
    return _png(tw, th, rgb)


def save_snapshot(t=None):
    try:
        SNAPS.mkdir(parents=True, exist_ok=True)
        t = int(t or time.time())
        datos = take_snapshot()
        (SNAPS / f"{t}.json").write_text(json.dumps(datos))
        (SNAPS / f"{t}.png").write_bytes(grab_screen_png())
        for old in sorted(SNAPS.glob("*.json"))[:-72]:  # ~3 días
            old.unlink(missing_ok=True)
            old.with_suffix(".png").unlink(missing_ok=True)
    except Exception as e:
        print("foto del estado falló:", e)


def snapshot_ids() -> list[int]:
    return sorted(int(p.stem) for p in SNAPS.glob("*.json")) if SNAPS.exists() else []


def point_epoch(p) -> float:
    return time.mktime(time.strptime(p["fecha"], "%Y-%m-%dT%H:%M:%S"))


def snapshot_for(p):
    pe = point_epoch(p)
    best = min(snapshot_ids(), key=lambda t: abs(t - pe), default=None)
    return best if best is not None and abs(best - pe) <= 900 else None


_live = {"t": 0.0, "datos": None, "busy": False}


def _refresh_live():
    try:
        _live["datos"] = take_snapshot()
        _live["t"] = time.time()
    except Exception as e:
        print("estado en vivo falló:", e)
    finally:
        _live["busy"] = False


def live_state():
    if time.time() - _live["t"] > 60 and not _live["busy"]:
        _live["busy"] = True
        threading.Thread(target=_refresh_live, daemon=True).start()
    return _live["datos"]


SETTING_CATS = {"servicios", "tareas", "inicio_sistema", "inicio_usuario", "firewall", "sistema", "apariencia"}
SVC = {"Auto": "Automatic", "Manual": "Manual", "Disabled": "Disabled"}
RUN = r"\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
PERS = r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
INET = r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings"


def q(x) -> str:
    return "'" + str(x).replace("'", "''") + "'"


def setting_cmd(cat, item, value):
    """PowerShell que deja UN ajuste en `value` (None = que no exista). None si no se puede revertir por separado."""
    if cat == "servicios" and value in SVC:
        return f"Set-Service -Name {q(item)} -StartupType {SVC[value]}"
    if cat == "tareas" and value in ("Activa", "Desactivada"):
        path, _, name = item.rpartition("\\")
        verb = "Enable" if value == "Activa" else "Disable"
        return f"{verb}-ScheduledTask -TaskPath {q(path + chr(92))} -TaskName {q(name)} | Out-Null"
    if cat in ("inicio_sistema", "inicio_usuario"):
        hk = "HKLM:" if cat == "inicio_sistema" else "HKCU:"
        keys = [hk + RUN] + ([hk + r"\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"] if hk == "HKLM:" else [])
        rm = "; ".join(f"Remove-ItemProperty -Path {q(k)} -Name {q(item)} -ErrorAction SilentlyContinue" for k in keys)
        return rm if value is None else rm + f"; Set-ItemProperty -Path {q(keys[0])} -Name {q(item)} -Value {q(value)}"
    if cat == "firewall" and value in ("Activado", "Desactivado"):
        return f"Set-NetFirewallProfile -Name {q(item)} -Enabled {'True' if value == 'Activado' else 'False'}"
    if value is None:
        return None
    if cat == "sistema":
        if item == "Plan de energía":
            m = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value)
            return f"powercfg /setactive {m.group(0)}" if m else None
        if item == "Zona horaria":
            return f"Set-TimeZone -Id {q(value)}"
        if item == "PATH del sistema":
            return f"[Environment]::SetEnvironmentVariable('Path', {q(value)}, 'Machine')"
    if cat == "apariencia":
        light = "1" if value == "Claro" else "0"
        if item == "Tema de apps" and value in ("Claro", "Oscuro"):
            return f"Set-ItemProperty -Path {q(PERS)} -Name AppsUseLightTheme -Value {light} -Type DWord"
        if item == "Tema del sistema" and value in ("Claro", "Oscuro"):
            return f"Set-ItemProperty -Path {q(PERS)} -Name SystemUsesLightTheme -Value {light} -Type DWord"
        if item == "Fondo de pantalla":
            return (f"Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' -Name WallPaper -Value {q(value)}; "
                    "rundll32.exe user32.dll,UpdatePerUserSystemParameters 1, True")
        if item == "Proxy":
            if value == "Sin proxy":
                return f"Set-ItemProperty -Path {q(INET)} -Name ProxyEnable -Value 0 -Type DWord"
            return (f"Set-ItemProperty -Path {q(INET)} -Name ProxyServer -Value {q(value)}; "
                    f"Set-ItemProperty -Path {q(INET)} -Name ProxyEnable -Value 1 -Type DWord")
    return None


def apply_value(cat, item, value) -> tuple[bool, str]:
    cmd = setting_cmd(cat, item, value)
    if cmd is None:
        return False, "No se puede revertir por separado"
    code, out = ps("$ErrorActionPreference='Stop'; " + cmd)
    return code == 0, ("" if code == 0 else (out or "Error")[:200])


def diff_states(then: dict, now: dict) -> list[dict]:
    out = []
    for cat, (label, rev) in CATS.items():
        a, b = now.get(cat) or {}, then.get(cat) or {}  # a = ahora, b = antes (a lo que volvería)
        for k in sorted(set(a) | set(b)):
            if k not in b:
                tipo = "quitar"      # se instaló/creó después del punto: al restaurar desaparece
            elif k not in a:
                tipo = "volver"      # existía y ya no está: al restaurar vuelve
            elif a[k] != b[k]:
                tipo = "cambiar"
            else:
                continue
            deseado = None if tipo == "quitar" else b[k]
            ok = cat in SETTING_CATS and (tipo == "cambiar" or cat not in ("servicios", "tareas")) \
                and setting_cmd(cat, k, deseado) is not None
            out.append({"id": f"{cat}|{k}", "cat": cat, "label": label, "revierte": rev, "ajuste": cat in SETTING_CATS,
                        "soportado": ok, "item": k, "tipo": tipo, "ahora": str(a.get(k, ""))[:140],
                        "antes": str(b.get(k, ""))[:140], "_av": a.get(k), "_bv": b.get(k)})
    return out


def pick_point(hours=None, number=None):
    pts = list_points()
    return next((x for x in pts if x["numero"] == number), None) if number is not None else target_point(hours)


def compare(hours=None, number=None) -> dict:
    p = pick_point(hours, number)
    if not p:
        return {"sin_punto": True}
    sid = snapshot_for(p)
    if sid is None:
        return {"punto": p, "sin_datos": True}
    live = live_state()
    if live is None:
        return {"punto": p, "snap": sid, "calculando": True}
    then = json.loads((SNAPS / f"{sid}.json").read_text())
    ch = diff_states(then, live)
    ch.sort(key=lambda c: (not c["ajuste"], list(CATS).index(c["cat"]), c["item"].lower()))
    pub = [{k: v for k, v in c.items() if not k.startswith("_")} for c in ch[:400]]
    return {"punto": p, "snap": sid, "cambios": pub, "total": len(ch),
            "revierten": sum(1 for c in ch if c["revierte"]), "ajustes": sum(1 for c in ch if c["ajuste"] and c["revierte"]),
            "actualizado": int(_live["t"])}


# ---------- aplicar solo ajustes (sin apps, sin reiniciar), con progreso en vivo y deshacer ----------
UNDO_F, KEEP_F, KEPT_F = CONF_DIR / "deshacer.json", CONF_DIR / "reaplicar.json", CONF_DIR / "reaplicado.json"
_job: dict = {"items": [], "terminado": True, "titulo": ""}
_job_lock = threading.Lock()


def _run_job(titulo, todo):
    """todo: lista de (id, texto, cat, item, valor_deseado, valor_previo). Ejecuta uno por uno publicando el avance."""
    _job.update(items=[{"id": t[0], "texto": t[1], "estado": "pendiente", "msg": ""} for t in todo], terminado=False, titulo=titulo)
    undo = []
    for i, (_id, _txt, cat, item, deseado, previo) in enumerate(todo):
        _job["items"][i]["estado"] = "aplicando"
        ok, msg = apply_value(cat, item, deseado)
        _job["items"][i].update(estado="ok" if ok else "error", msg=msg)
        if ok:
            undo.append({"cat": cat, "item": item, "valor": previo, "texto": _txt})
    if titulo != "Deshaciendo":
        UNDO_F.write_text(json.dumps(undo))
    else:
        UNDO_F.unlink(missing_ok=True)
    _live["t"] = 0  # fuerza releer el estado en vivo
    _job["terminado"] = True


def start_job(titulo, todo):
    with _job_lock:
        if not _job["terminado"]:
            raise RuntimeError("Ya hay una aplicación de ajustes en curso.")
        _job["terminado"] = False
    threading.Thread(target=_run_job, args=(titulo, todo), daemon=True).start()


def apply_settings(hours, number, ids):
    p = pick_point(hours, number)
    sid = snapshot_for(p) if p else None
    if sid is None:
        raise RuntimeError("Ese punto no tiene foto del estado; no se pueden revertir ajustes por separado.")
    then = json.loads((SNAPS / f"{sid}.json").read_text())
    fresh = take_snapshot()
    ch = [c for c in diff_states(then, fresh) if c["id"] in set(ids) and c["soportado"]]
    if not ch:
        raise RuntimeError("No hay ajustes seleccionados que revertir.")
    todo = [(c["id"], f"{c['label']}: {c['item']}", c["cat"], c["item"],
             None if c["tipo"] == "quitar" else c["_bv"], c["_av"]) for c in ch]
    start_job("Aplicando ajustes", todo)
    return len(todo)


def undo_settings():
    if not UNDO_F.exists():
        raise RuntimeError("No hay nada que deshacer.")
    items = json.loads(UNDO_F.read_text())
    todo = [(f"{u['cat']}|{u['item']}", u["texto"], u["cat"], u["item"], u["valor"], None) for u in items]
    start_job("Deshaciendo", todo)
    return len(todo)


def save_keep(ids):
    """Guarda los ajustes (con su valor ACTUAL) que se volverán a aplicar solos después de restaurar y reiniciar."""
    ids = set(ids or [])
    if not ids:
        KEEP_F.unlink(missing_ok=True)
        return 0
    fresh = take_snapshot()
    keep = []
    for i in ids:
        cat, _, item = i.partition("|")
        if cat not in SETTING_CATS:
            continue
        val = (fresh.get(cat) or {}).get(item)
        if (val is None and cat not in ("inicio_sistema", "inicio_usuario")) or setting_cmd(cat, item, val) is None:
            continue
        keep.append({"cat": cat, "item": item, "valor": val, "texto": f"{CATS[cat][0]}: {item}"})
    KEEP_F.write_text(json.dumps(keep))
    return len(keep)


def apply_pending():
    """Tras el reinicio: vuelve a aplicar los ajustes que el usuario pidió conservar."""
    if not KEEP_F.exists():
        return
    time.sleep(20)  # que Windows termine de arrancar
    keep = json.loads(KEEP_F.read_text())
    res = []
    for k in keep:
        ok, msg = apply_value(k["cat"], k["item"], k["valor"])
        res.append({"texto": k["texto"], "ok": ok, "msg": msg})
    KEPT_F.write_text(json.dumps({"t": int(time.time()), "items": res}))
    KEEP_F.unlink(missing_ok=True)


# ---------- hilos internos (sin tareas externas) ----------
def hourly_points():
    if not snapshot_ids():
        save_snapshot()
    while True:
        try:
            pts = list_points()
            if not pts or time.time() - time.mktime(time.strptime(pts[-1]["fecha"], "%Y-%m-%dT%H:%M:%S")) >= 3500:
                create_point()
        except Exception as e:
            print("punto horario falló:", e)
        time.sleep(600)


def _timer_run(sec, hours, number, keep=None):
    time.sleep(sec)
    with _lock:
        if _timer["t"] is not threading.current_thread():
            return
        _timer["at"] = _timer["t"] = None
    try:
        print(restore(hours, number, keep))
    except Exception as e:
        print("restauración por temporizador falló:", e)


def set_timer(minutes, hours=None, number=None, keep=None):
    with _lock:
        _timer["at"] = _timer["t"] = None
        if minutes:
            t = threading.Thread(target=_timer_run, args=(minutes * 60, hours, number, keep), daemon=True)
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
        if path.startswith("/api/captura/") and path.endswith(".png"):
            name = path[len("/api/captura/"):-4]
            try:
                if name == "ahora":
                    png = grab_screen_png()
                elif name.isdigit():
                    png = (SNAPS / f"{int(name)}.png").read_bytes()
                else:
                    raise FileNotFoundError
                return self.send_body(200, png, "image/png")
            except Exception:
                return self.send_body(404, "no", "text/plain")
        if path == "/":
            return self.send_body(200, PAGE, "text/html")
        if path == "/api/comparar":
            q = parse_qs(self.path.partition("?")[2])
            try:
                num = int(q["numero"][0]) if "numero" in q else None
                return self.send_body(200, json.dumps(compare(float(q.get("horas", [HOURS])[0]), num)))
            except Exception as e:
                return self.send_body(500, json.dumps({"error": str(e)}))
        if path == "/api/ajustes/estado":
            kept = json.loads(KEPT_F.read_text()) if KEPT_F.exists() else None
            return self.send_body(200, json.dumps({"job": _job, "deshacer": UNDO_F.exists(), "reaplicado": kept}))
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
        raw = self.rfile.read(min(n, 1 << 20)).decode()
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
                msg = restore(float(body.get("horas", HOURS)), body.get("numero"), body.get("conservar"))
            elif self.path == "/api/ajustes/aplicar":
                n = apply_settings(float(body.get("horas", HOURS)), body.get("numero"), body.get("ids") or [])
                msg = f"Aplicando {n} ajustes…"
            elif self.path == "/api/ajustes/deshacer":
                msg = f"Deshaciendo {undo_settings()} ajustes…"
            elif self.path == "/api/ajustes/descartar":
                KEPT_F.unlink(missing_ok=True)
                msg = "ok"
            elif self.path == "/api/crear":
                msg = create_point()
            elif self.path == "/api/temporizador":
                m = float(body.get("minutos", 0))
                if m and not 1 <= m <= 1440:
                    return self.send_body(400, json.dumps({"error": "minutos entre 1 y 1440"}))
                return self.send_body(200, json.dumps({"ok": True, "temporizador": set_timer(m, float(body.get("horas", HOURS)), body.get("numero"), body.get("conservar"))}))
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
<title>Retroceder PC</title><link rel=manifest href=/manifest.json><meta name=theme-color content="#0f172a"><body style="font:16px system-ui;background:#0f172a;color:#e2e8f0;padding:24px;max-width:420px;margin:auto">
<h2>⏪ Retroceder PC</h2><form method=post action=/login><input name=password type=password placeholder=Contraseña autofocus
style="width:100%;padding:14px;border-radius:8px;border:0;font-size:16px;box-sizing:border-box"><p style=color:#f87171>%ERR%</p>
<button style="width:100%;padding:14px;border:0;border-radius:10px;background:#0284c7;color:#fff;font-size:17px">Entrar</button></form><button id=inst hidden style="width:100%;padding:16px;margin:8px 0;border:0;border-radius:12px;font-size:17px;background:#16a34a;color:#fff;font-weight:700">⬇ Instalar app en este teléfono</button>
<script>let ip=null;const ib=document.getElementById("inst");
addEventListener("beforeinstallprompt",e=>{e.preventDefault();ip=e;ib.hidden=false});
ib.onclick=async()=>{ip.prompt();await ip.userChoice;ib.hidden=true};addEventListener("appinstalled",()=>ib.hidden=true);
if("serviceWorker"in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});</script></body></html>"""
PAGE = """<!doctype html><html lang=es><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Retroceder PC</title><link rel=manifest href=/manifest.json><meta name=theme-color content="#0f172a">
<style>body{margin:0;font:16px system-ui;background:#0f172a;color:#e2e8f0;padding:16px;max-width:480px;margin:auto}
h1{font-size:22px}button{width:100%;padding:16px;margin:8px 0;border:0;border-radius:12px;font-size:17px;background:#334155;color:#fff}
button.big{background:#dc2626;font-weight:700}button.ok{background:#0284c7}.card{background:#1e293b;border-radius:12px;padding:12px;margin:12px 0}
small{color:#94a3b8}input{padding:12px;border-radius:8px;border:0;width:90px;font-size:16px}#msg{white-space:pre-wrap;color:#fbbf24}
.pt{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid #334155;font-size:14px}
.pt button{width:auto;padding:8px 12px;margin:0;font-size:14px;background:#7f1d1d}#lista{max-height:240px;overflow:auto}
.pt .vb{background:#334155;margin-left:6px}.split{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.split img{width:100%;border-radius:6px;background:#000;min-height:40px;display:block}.split small{display:block;margin-bottom:2px}
.chg{border-top:1px solid #334155;padding:7px 0;font-size:13px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:3px}
.cols div{background:#0f172a;border-radius:6px;padding:4px 6px;word-break:break-word}.tag{font-size:11px;padding:2px 6px;border-radius:6px;margin-left:4px}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:8px 0}.seg button{margin:0;padding:10px;font-size:14px;background:#1e293b;border:2px solid #334155}
.seg button.on{border-color:#38bdf8;background:#0c4a6e}#modoTxt{font-size:13px;color:#94a3b8;margin-bottom:6px}.chg label{display:block;margin-top:4px;font-size:13px;color:#7dd3fc}
.chg input{width:auto;transform:scale(1.3);margin-right:8px}#job div{padding:3px 0;font-size:14px}button.go{background:#16a34a;font-weight:700}
.rv{background:#14532d}.nrv{background:#713f12}.cat{margin-top:10px;color:#38bdf8;font-weight:600}</style></head><body>
<div id=ov hidden style="position:fixed;inset:0;background:#0f172af2;z-index:9;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px"><h1 id=ovt></h1><p id=ovs></p></div>
<h1>⏪ Retroceder PC</h1>
<button id=inst hidden style="width:100%;padding:16px;margin:8px 0;border:0;border-radius:12px;font-size:17px;background:#16a34a;color:#fff;font-weight:700">⬇ Instalar app en este teléfono</button>
<script>let ip=null;const ib=document.getElementById("inst");
addEventListener("beforeinstallprompt",e=>{e.preventDefault();ip=e;ib.hidden=false});
ib.onclick=async()=>{ip.prompt();await ip.userChoice;ib.hidden=true};addEventListener("appinstalled",()=>ib.hidden=true);
</script>
<div class=card><b>¿Cuánto retroceder?</b><br><input id=horas type=number value=10 min=0.5 step=0.5> horas atrás
<div id=estado style="margin-top:8px">Cargando…</div></div>
<div class=card><b>Comparativa en tiempo real</b> <small id=cmpinfo></small>
<div class=seg><button id=mTodo class=on>Todo (Windows)</button><button id=mAj>Solo ajustes</button></div><div id=modoTxt></div>
<div class=split><div><small>AHORA (en vivo)</small><img id=imgA alt=""></div><div><small id=lblB>Antes</small><img id=imgB alt=""></div></div>
<div id=resumen style="margin-top:8px"></div><div id=cambios></div></div>
<div id=reap class=card hidden></div>
<div id=jobcard class=card hidden><b id=jobt></b><div id=job></div></div>
<button class=big id=ahora>Restaurar ahora</button>
<button class=go id=aplicar hidden>Aplicar ajustes</button>
<button id=deshacer hidden>↩ Deshacer los últimos ajustes aplicados</button>
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
 $("lista").innerHTML=d.puntos.slice().reverse().map(p=>`<div class=pt><span>${p.fecha.replace("T"," ")}</span><span><button class=vb data-v=${p.numero}>Ver</button><button data-n=${p.numero}>Ir aquí</button></span></div>`).join("")||"Ninguno todavía";
 tmp=d.temporizador;pintar()}
let sel=null,snapShown=null,mode="todo",D=null;const CH={todo:{},aj:{}};
const esc=t=>String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const TIPO={quitar:"Se quitará",volver:"Se volverá a poner",cambiar:"Cambiará"};
const checked=c=>c.id in CH[mode]?CH[mode][c.id]:true;
const marcables=()=>D&&D.cambios?D.cambios.filter(c=>c.ajuste&&c.soportado&&c.revierte):[];
const marcados=()=>marcables().filter(checked).map(c=>c.id);
function modoUI(){$("mTodo").className=mode=="todo"?"on":"";$("mAj").className=mode=="aj"?"on":"";
 $("modoTxt").textContent=mode=="todo"?"Restaura Windows completo (programas, controladores y ajustes del sistema) y reinicia. Marca «Conservar» en los ajustes que NO quieres perder: se vuelven a aplicar solos al volver.":"Solo revierte los ajustes que marques, sin tocar programas ni reiniciar. Cada cambio se ve en vivo y se puede deshacer.";
 $("ahora").hidden=mode!="todo";$("aplicar").hidden=mode!="aj";botones()}
function botones(){const n=marcados().length;$("aplicar").textContent=`Aplicar ${n} ajuste${n==1?"":"s"} ahora (sin reiniciar)`;$("aplicar").disabled=!n;
 $("ahora").textContent=mode=="todo"?(n?`Restaurar y reiniciar (conserva ${n} ajuste${n==1?"":"s"})`:"Restaurar y reiniciar"):"Restaurar ahora"}
function render(){const R=$("resumen"),C=$("cambios");if(!D||!D.cambios){return}
 const L=D.cambios.filter(c=>mode=="todo"||c.ajuste);const oc=D.cambios.length-L.length;
 R.innerHTML=D.total?(mode=="todo"?`<b>${D.total}</b> diferencias · <b>${D.ajustes}</b> son ajustes que perderías (puedes conservarlos) · el resto son programas, controladores y similares`
  :`<b>${L.filter(c=>c.revierte).length}</b> ajustes distintos a ese punto${oc?` · ${oc} de programas/controladores no se tocan en este modo`:""}`):"✅ Sin diferencias: la PC está igual que en ese punto.";
 let h="",last="";for(const c of L){if(c.label!==last){last=c.label;h+=`<div class=cat>${esc(c.label)}${c.ajuste?"":" (programas/sistema)"}</div>`}
  const op=c.ajuste&&c.soportado&&c.revierte;
  h+=`<div class=chg><b>${esc(c.item)}</b><span class="tag ${c.revierte?"rv":"nrv"}">${c.revierte?"Se revierte":"No se revierte"}</span> <small>${TIPO[c.tipo]}</small>
  <div class=cols><div><small>Ahora</small><br>${c.tipo=="volver"?"—":esc(c.ahora)||"—"}</div><div><small>Quedaría</small><br>${c.tipo=="quitar"?"— (no existe)":esc(c.antes)||"—"}</div></div>
  ${op?`<label><input type=checkbox data-id="${esc(c.id)}" ${checked(c)?"checked":""}>${mode=="todo"?"Conservar el valor de ahora":"Revertir este ajuste"}</label>`:(c.ajuste&&c.revierte?"<small>Este ajuste solo se revierte con el modo Todo.</small>":"")}</div>`}
 C.innerHTML=h;botones()}
async function comparar(){const d=await api("/api/comparar?"+(sel?"numero="+sel:"horas="+hrs()));
 const R=$("resumen"),C=$("cambios");
 if(d.error){R.textContent=d.error;return}
 if(d.sin_punto){D=null;R.textContent="Aún no hay un punto para comparar.";C.innerHTML="";$("imgB").removeAttribute("src");botones();return}
 $("lblB").textContent="Antes: "+d.punto.fecha.replace("T"," ");
 if(d.sin_datos){D=null;R.textContent="Ese punto no tiene foto del estado (lo creó Windows u otro programa). Se puede restaurar igual, pero no hay comparativa ni modo «Solo ajustes».";C.innerHTML="";$("imgB").removeAttribute("src");botones();return}
 if(snapShown!==d.snap){snapShown=d.snap;$("imgB").src="/api/captura/"+d.snap+".png"}
 if(d.calculando){R.textContent="Leyendo el estado actual de la PC…";return}
 if(jobRun)return;D=d;$("cmpinfo").textContent="· actualizado "+new Date(d.actualizado*1000).toLocaleTimeString();render()}
let jobRun=false,jt=null;
function pintarJob(j){$("jobcard").hidden=false;$("jobt").textContent=(j.terminado?"✔ ":"⏳ ")+j.titulo+(j.terminado?" — terminado":"…");
 $("job").innerHTML=j.items.map(i=>`<div>${{pendiente:"⚪",aplicando:"⏳",ok:"✅",error:"❌"}[i.estado]} ${esc(i.texto)}${i.msg?` <small style=color:#f87171>${esc(i.msg)}</small>`:""}</div>`).join("")}
function seguirJob(){jobRun=true;clearInterval(jt);jt=setInterval(async()=>{const d=await api("/api/ajustes/estado");if(!d.job)return;pintarJob(d.job);
 if(d.job.terminado){clearInterval(jt);jobRun=false;$("deshacer").hidden=!d.deshacer;setTimeout(comparar,1500)}},700)}
async function estadoAj(){const d=await api("/api/ajustes/estado");if(!d.job)return;$("deshacer").hidden=!d.deshacer||jobRun;
 if(!jobRun&&!d.job.terminado){seguirJob()}
 const r=d.reaplicado;if(r){$("reap").hidden=false;$("reap").innerHTML=`<b>♻ Tras el reinicio se volvieron a aplicar ${r.items.length} ajustes que querías conservar</b>`+r.items.map(i=>`<div style="font-size:13px">${i.ok?"✅":"❌"} ${esc(i.texto)}${i.ok?"":" <small>"+esc(i.msg)+"</small>"}</div>`).join("")+`<button id=okreap>Entendido</button>`;
  $("okreap").onclick=async()=>{await api("/api/ajustes/descartar","POST",{});$("reap").hidden=true}}else $("reap").hidden=true}
function pintar(){$("canc").hidden=!tmp;if(!tmp){$("cuenta").textContent="";return}
 const s=Math.max(0,tmp-Math.floor(Date.now()/1000));$("cuenta").textContent=`Restaura en ${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`}
async function ejecutar(b){const r=await api("/api/ejecutar","POST",b);
 if(r.error){$("msg").textContent=r.error;return}
 reiniciando(r.msg)}
function reiniciando(msg){const o=$("ov");o.hidden=false;$("ovt").textContent="🔄 PC reiniciando…";$("ovs").textContent=msg||"";
 let caido=false;const t=setInterval(async()=>{try{const c=new AbortController();setTimeout(()=>c.abort(),3000);
  const r=await fetch("/api/estado",{signal:c.signal,headers:H});if(caido&&r.ok){clearInterval(t);$("ovt").textContent="✅ PC en línea de nuevo";
  $("ovs").textContent="Restauración completada.";setTimeout(()=>{o.hidden=true;cargar()},2500)}}catch{caido=true;$("ovt").textContent="🔄 PC reiniciando…";$("ovs").textContent="Restaurando y reiniciando. Esta pantalla avisa cuando vuelva."}},3000)}
setInterval(pintar,1000);setInterval(cargar,5000);$("horas").oninput=()=>{sel=null;clearTimeout(hz);hz=setTimeout(()=>{cargar();comparar()},400)};
setInterval(()=>{if(!document.hidden)$("imgA").src="/api/captura/ahora.png?t="+Date.now()},2000);setInterval(()=>{if(!document.hidden)comparar()},8000);
$("ahora").onclick=()=>ejecutar({...(sel?{numero:sel}:{horas:hrs()}),conservar:marcados()});
$("mTodo").onclick=()=>{mode="todo";modoUI();render()};$("mAj").onclick=()=>{mode="aj";modoUI();render()};
$("cambios").onchange=e=>{const i=e.target.dataset.id;if(i!==undefined){CH[mode][i]=e.target.checked;botones()}};
$("aplicar").onclick=async()=>{const r=await api("/api/ajustes/aplicar","POST",{...(sel?{numero:sel}:{horas:hrs()}),ids:marcados()});
 if(r.error){$("msg").textContent=r.error;return}$("msg").textContent="";seguirJob()};
$("deshacer").onclick=async()=>{const r=await api("/api/ajustes/deshacer","POST",{});if(r.error){$("msg").textContent=r.error;return}seguirJob()};
$("lista").onclick=e=>{const n=e.target.dataset.n,v=e.target.dataset.v;if(n)ejecutar({numero:+n});if(v){sel=+v;$("msg").textContent="";comparar()}};
$("prog").onclick=async()=>{const r=await api("/api/temporizador","POST",{minutos:+$("min").value,...(sel?{numero:sel}:{horas:hrs()}),conservar:marcados()});tmp=r.temporizador;pintar()};
$("canc").onclick=async()=>{await api("/api/temporizador","POST",{minutos:0});tmp=null;pintar()};
$("crear").onclick=async()=>{const r=await api("/api/crear","POST");$("msg").textContent=r.msg||r.error||"";cargar()};
if("serviceWorker"in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});
modoUI();cargar();comparar();estadoAj();setInterval(estadoAj,5000);</script></body></html>"""

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
    if FAKE and not snapshot_ids():  # solo pruebas: foto del estado "de hace 11 h"
        save_snapshot(point_epoch(_fake_points[0]) + 30)
    threading.Thread(target=hourly_points, daemon=True).start()
    threading.Thread(target=apply_pending, daemon=True).start()
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
