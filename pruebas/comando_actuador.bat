@echo off
rem Manda un comando MQTT al actuador sin usar el dashboard. Ejemplos:
rem   comando_actuador.bat buzzer_on    comando_actuador.bat led_off    comando_actuador.bat auto
rem Comandos: armar desarmar buzzer_on buzzer_off led_on led_off auto alerta_on alerta_off
set MOSQ=C:\Program Files\mosquitto
if "%1"=="" (echo Uso: comando_actuador.bat ^<comando^> & exit /b 1)
set /p PW=Contrasena de registro_lab1: 
"%MOSQ%\mosquitto_pub.exe" -h 127.0.0.1 -u registro_lab1 -P %PW% -t alarma/lab1/puerta01/comando -q 1 -m %1
echo Comando %1 enviado (QoS 1). Mira la respuesta en .../respuesta y el estado en .../actuadores
