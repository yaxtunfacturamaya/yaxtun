# Retroceder PC (nativo, HTTPS propio, sin servicios externos)

Un solo `.exe` para Windows. Sin Tailscale ni cuentas: genera su propia autoridad de certificados y sirve el panel por **HTTPS** en tu WiFi.

## Cómo funciona el retroceso
Usa **Restaurar sistema** de Windows. El programa crea un *punto de restauración* cada hora (guarda ajustes, controladores,
registro y programas instalados). Al pedir «retroceder 10 h» busca el punto más reciente de hace 10 h o más, crea antes un
punto de seguridad (para poder deshacer) y le dice a Windows que restaure; la PC se reinicia y todo lo cambiado después vuelve a como estaba.
No toca documentos ni archivos personales. El botón restaura y reinicia al instante, sin pedir confirmación; la pantalla muestra «PC reiniciando…» y avisa cuando vuelve (requiere que Windows inicie sesión solo para que el programa arranque otra vez). Solo puede volver a puntos que ya existan (hay que esperar las horas la primera vez).
En el panel eliges **cuántas horas atrás**, o tocas «Ir aquí» en un punto exacto, y puedes programar un **temporizador**.

## Instalar (una sola vez)
1. En la PC abre `RetrocederPC.exe` (pide Administrador). Se configura solo: Restaurar sistema, firewall (puertos 8780-8781, redes privadas), inicio con Windows.
2. En el Android (mismo WiFi) abre `http://<ip-de-la-pc>:8781` (la ventana la muestra). Descarga el certificado e instálalo:
   Ajustes → Seguridad → Más seguridad → Cifrado y credenciales → Instalar un certificado → Certificado de CA.
3. Abre `https://<ip-de-la-pc>:8780`, escribe la contraseña y en Chrome ⋮ → **Instalar app**. Queda como app en tu pantalla de inicio.

**Descarga directa del .exe (PC):** https://github.com/yaxtunfacturamaya/yaxtun/releases/download/descarga/RetrocederPC.exe (se actualiza solo con cada cambio). Con Python: `pip install -r retroceder_pc/requirements.txt` y `python retroceder_pc/retroceder_pc.py`.
Quitar inicio automático/firewall: `RetrocederPC.exe --desinstalar`. Variables: `RETRO_HORAS` (10), `RETRO_PUERTO` (8780).

## Seguridad
HTTPS (TLS 1.2+), contraseña aleatoria en `C:\ProgramData\RetrocederPC\config.json`, límite de intentos, cookie Secure/HttpOnly, solo responde a IPs de red local.
La clave de la autoridad (`ca.key`) vive solo en la PC; no la compartas. Si cambia la IP de la PC, el certificado se renueva solo (la autoridad ya instalada sigue valiendo).
No abras estos puertos en el router.

## Comparativa en pantalla dividida
Cada hora, junto con el punto de restauración, el programa guarda una **foto del estado de la PC** (programas, actualizaciones, controladores,
servicios, tareas, inicio con Windows, firewall y ajustes) y una **captura de pantalla**. En el panel, izquierda = tu PC en vivo,
derecha = cómo estaba en ese punto, y debajo la lista de diferencias (se quitará / se volverá a poner / cambiará). Cada diferencia dice si
Restaurar sistema **la revierte** o no: devuelve el sistema, programas, controladores y servicios; **no** revierte la configuración de tu usuario
(tema, fondo, proxy…) ni tus archivos, y esos se muestran solo como informativos. Los puntos que creó Windows (no este programa) no tienen foto.

## No perder ajustes: «Conservar» y modo «Solo ajustes»
- **Modo Todo (Windows):** la lista marca los ajustes que perderías. Los que marques con «Conservar» se guardan con su valor actual, Windows restaura y reinicia, y al volver el programa los **reaplica solo** (aparece un aviso con el resultado de cada uno).
- **Modo Solo ajustes:** revierte únicamente los ajustes que marques (servicios, tareas, inicio con Windows, firewall, plan de energía, zona horaria, PATH, tema, fondo, proxy), **sin reiniciar ni tocar programas, controladores o actualizaciones**. Se aplican uno por uno con progreso en vivo (✅/❌) y hay botón **Deshacer**.
- No se pueden revertir por separado: instalar/quitar programas, controladores, actualizaciones, nombre del equipo, navegador predeterminado y resolución; esos solo cambian con el modo Todo.
- Los ajustes se leen de la foto del estado que se guarda cada hora, así que solo se comparan puntos creados por este programa.
