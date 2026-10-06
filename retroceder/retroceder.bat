@echo off
rem Pide permisos de administrador y abre el menu de retroceder.ps1
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0retroceder.ps1" %*
pause
