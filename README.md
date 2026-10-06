# Escritorio remoto por URL (HTTPS + Tailscale)

Ve y controla tu escritorio (mouse, clics, teclado, scroll) desde cualquier navegador
dentro de tu tailnet, solo por HTTPS.

## Ejecutable (sin Python)
En GitHub → Actions → "Build ejecutables" descarga `escritorio-remoto-windows-latest` (.exe).
Ábrelo en tu PC: entra sin contraseña (la autenticación es tu cuenta de Tailscale) y
publica solo la URL HTTPS con `tailscale serve`. Solo necesitas Tailscale instalado.

## Con Python (alternativa)
```
pip install -r requirements.txt
# Windows (PowerShell):  $env:RD_PASSWORD="una-clave-larga"
# export RD_PASSWORD="una-clave-larga"   # opcional; sin ella entras solo con tu cuenta de Tailscale
export RD_ALLOWED_LOGIN="yaxtunfacturamaya.com.mx@hotmail.com"   # opcional: solo tu cuenta Tailscale
python server.py
```
En otra terminal, publica con HTTPS (certificado automático de Tailscale):
```
tailscale serve --bg --https=8443 http://127.0.0.1:8765
```
Abre `https://<tu-pc>.<tu-tailnet>.ts.net:8443/` (puerto 8443 para no chocar con otro servicio en el 443; cámbialo con `RD_HTTPS_PORT`) desde cualquier dispositivo de tu tailnet, ingresa la contraseña y listo.
(Activa HTTPS en la consola de Tailscale → DNS → "Enable HTTPS" si no lo está.)

## Seguridad
- El servidor solo escucha en 127.0.0.1; la única entrada es `tailscale serve` (no `funnel`, así no es público).
- Sin contraseña por defecto: solo entran dispositivos/usuarios de tu tailnet (header de identidad de Tailscale obligatorio). Si defines `RD_PASSWORD`, se pide además.
- Con contraseña: cookie firmada (Secure, HttpOnly), límite de intentos, y filtro opcional por cuenta de Tailscale.
- Quien entre controla tu PC: usa contraseña larga.

## Notas
- Muestra el monitor principal. Ajustes: `RD_FPS`, `RD_QUALITY`, `RD_MAX_WIDTH`.
- En Linux requiere X11 (no Wayland). En macOS da permisos de grabación de pantalla y accesibilidad a la terminal.
- No funciona en pantalla de bloqueo/UAC de Windows (limitación de procesos de usuario).
