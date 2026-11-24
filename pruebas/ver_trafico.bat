@echo off
rem Muestra el trafico MQTT en vivo: topico, QoS, bandera retenido y contenido.
set MOSQ=C:\Program Files\mosquitto
set /p PW=Contrasena de registro_lab1: 
"%MOSQ%\mosquitto_sub.exe" -h 127.0.0.1 -u registro_lab1 -P %PW% -t "alarma/lab1/puerta01/#" -q 1 -F "%%t | QoS %%q | retenido=%%r | %%p"
