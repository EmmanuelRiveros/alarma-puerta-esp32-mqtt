@echo off
rem Prueba de seguridad: el broker debe NEGAR lo que la ACL no permite.
set MOSQ=C:\Program Files\mosquitto
echo.
echo === 1) Contrasena incorrecta: debe salir "Connection Refused: not authorised" ===
"%MOSQ%\mosquitto_sub.exe" -h 127.0.0.1 -u esp_puerta01 -P contrasena_equivocada -t alarma/lab1/puerta01/estado -d -W 3
echo.
set /p PW=Contrasena CORRECTA de esp_puerta01: 
echo.
echo === 2) esp_puerta01 intenta SUSCRIBIRSE a evento (la ACL no se lo permite): SUBACK debe traer 128 ===
"%MOSQ%\mosquitto_sub.exe" -h 127.0.0.1 -u esp_puerta01 -P %PW% -t alarma/lab1/puerta01/evento -q 1 -d -W 3
echo.
echo === 3) esp_puerta01 intenta PUBLICAR un comando (solo el servicio puede): el broker lo descarta ===
"%MOSQ%\mosquitto_pub.exe" -h 127.0.0.1 -u esp_puerta01 -P %PW% -t alarma/lab1/puerta01/comando -m buzzer_on -q 1 -d
echo (en la ventana del broker con -v aparece el rechazo; el buzzer NO debe cambiar)
pause
