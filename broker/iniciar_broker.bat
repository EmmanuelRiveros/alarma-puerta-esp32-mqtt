@echo off
rem Inicia Mosquitto con autenticacion y ACL. No guarda contrasenas: las pide al crear los usuarios.
cd /d "%~dp0"
set MOSQ=C:\Program Files\mosquitto
echo === IP de esta laptop (ponla en MQTT_HOST de secretos.h) ===
ipconfig | findstr /i "IPv4"
echo.
if not exist data mkdir data
if not exist passwd (
  echo === Primera vez: crea el usuario del ESP32 ===
  "%MOSQ%\mosquitto_passwd.exe" -c passwd esp_puerta01
  echo === Crea el usuario del servicio ===
  "%MOSQ%\mosquitto_passwd.exe" passwd registro_lab1
)
netsh advfirewall firewall add rule name="Mosquitto 1883" dir=in action=allow protocol=TCP localport=1883 >nul 2>&1
echo === Iniciando Mosquitto (Ctrl+C para detener) ===
"%MOSQ%\mosquitto.exe" -c mosquitto.conf -v
pause
