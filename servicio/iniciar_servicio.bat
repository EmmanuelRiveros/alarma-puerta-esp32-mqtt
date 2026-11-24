@echo off
rem Servicio de registro + dashboard (http://localhost:8080). La contrasena se pide aqui, no esta en el codigo.
cd /d "%~dp0"
python -m pip install paho-mqtt
if not defined MQTT_PASS set /p MQTT_PASS=Contrasena MQTT del usuario registro_lab1: 
set MQTT_HOST=127.0.0.1
rem Ventana de calculo (min) y umbral de alerta (s). Cambialos si hace falta:
set VENTANA_MIN=60
set UMBRAL_S=10
python servicio_alarma.py
pause
