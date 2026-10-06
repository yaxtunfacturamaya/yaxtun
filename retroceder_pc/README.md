# Retroceder PC (nativo, HTTPS propio, sin servicios externos)

Un solo `.exe` para Windows. Sin Tailscale ni cuentas: genera su propia autoridad de certificados y sirve el panel por **HTTPS** en tu WiFi.

## Cómo funciona el retroceso
Usa **Restaurar sistema** de Windows. El programa crea un *punto de restauración* cada hora (guarda ajustes, controladores,
registro y programas instalados). Al pedir «retroceder 10 h» busca el punto más reciente de hace 10 h o más, crea antes un
punto de seguridad (para poder deshacer) y le dice a Windows que restaure; la PC se reinicia y todo lo cambiado después vuelve a como estaba.
No toca documentos ni archivos personales. Solo puede volver a puntos que ya existan (hay que esperar las horas la primera vez).
En el panel eliges **cuántas horas atrás**, o tocas «Ir aquí» en un punto exacto, y puedes programar un **temporizador**.

## Instalar (una sola vez)
1. En la PC abre `RetrocederPC.exe` (pide Administrador). Se configura solo: Restaurar sistema, firewall (puertos 8780-8781, redes privadas), inicio con Windows.
2. En el Android (mismo WiFi) abre `http://<ip-de-la-pc>:8781` (la ventana la muestra). Descarga el certificado e instálalo:
   Ajustes → Seguridad → Más seguridad → Cifrado y credenciales → Instalar un certificado → Certificado de CA.
3. Abre `https://<ip-de-la-pc>:8780`, escribe la contraseña y en Chrome ⋮ → **Instalar app**. Queda como app en tu pantalla de inicio.

El .exe: GitHub → Actions → «Build Retroceder PC» → `RetrocederPC-windows`. Con Python: `pip install -r retroceder_pc/requirements.txt` y `python retroceder_pc/retroceder_pc.py`.
Quitar inicio automático/firewall: `RetrocederPC.exe --desinstalar`. Variables: `RETRO_HORAS` (10), `RETRO_PUERTO` (8780).

## Seguridad
HTTPS (TLS 1.2+), contraseña aleatoria en `C:\ProgramData\RetrocederPC\config.json`, límite de intentos, cookie Secure/HttpOnly, solo responde a IPs de red local.
La clave de la autoridad (`ca.key`) vive solo en la PC; no la compartas. Si cambia la IP de la PC, el certificado se renueva solo (la autoridad ya instalada sigue valiendo).
No abras estos puertos en el router.
