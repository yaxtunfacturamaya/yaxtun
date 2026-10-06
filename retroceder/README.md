# Retroceder la configuración de Windows 10 horas

Usa Restaurar sistema de Windows para devolver ajustes del sistema, controladores y
programas instalados al estado de hace N horas (10 por defecto). No toca documentos ni archivos personales.

1. Copia esta carpeta a la PC y ejecuta `retroceder.bat` (pide administrador).
2. Opción **4 (Instalar)** una sola vez: activa Restaurar sistema y crea un punto cada hora.
3. Cuando quieras volver atrás: opción **1**. Busca el punto más reciente de hace ≥10 h, crea antes un punto de seguridad y reinicia la PC.

Sin menú: `retroceder.bat -Accion Retroceder -Horas 10 -Si`

Solo puede retroceder hasta donde exista un punto: tras instalar, hay que esperar 10 horas para el primero.
