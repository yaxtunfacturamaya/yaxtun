# Retroceder PC (nativo, sin servicios externos)

Un solo `.exe` para Windows. Sin Tailscale, sin cuentas, sin nada que instalar: solo la biblioteca estándar de Python.

**Qué hace al abrirlo** (pide Administrador solo): activa Restaurar sistema, abre el puerto 8780 en el firewall (solo redes privadas),
se pone para iniciar con Windows y crea un punto de restauración **cada hora** por su cuenta.

**Desde el Android** (mismo WiFi que la PC): abre la dirección y la contraseña que muestra la ventana
(ej. `http://192.168.1.50:8780`). Botones: restaurar a hace 10 h, temporizador que restaura solo, crear punto ahora.
Chrome → ⋮ → «Añadir a pantalla de inicio» para tener el icono.

- Obtener el .exe: GitHub → Actions → «Build Retroceder PC» → artefacto `RetrocederPC-windows`.
- O con Python: `python retroceder_pc/retroceder_pc.py` (Windows).
- Quitar inicio automático y firewall: `RetrocederPC.exe --desinstalar`.
- Variables: `RETRO_HORAS` (10), `RETRO_PUERTO` (8780).

Seguridad: contraseña aleatoria (guardada en `C:\ProgramData\RetrocederPC\config.json`), límite de intentos, y solo responde a IPs de red local.
Va por HTTP dentro de tu WiFi (sin cifrar); no abras el puerto en el router.
No restaura archivos personales: devuelve ajustes, controladores y programas. Solo puede volver a puntos que existan: tras la primera vez hay que esperar 10 h.
