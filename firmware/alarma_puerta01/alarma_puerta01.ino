/*
  Alarma de apertura de puerta - ESP32 + MQTT (Actividad final)
  Libreria: "MQTT" de Joel Gaehwiler (256dpi)  -> Administrar bibliotecas -> buscar "MQTT"
  (PubSubClient NO sirve: solo publica con QoS 0 y el proyecto usa QoS 1)

  v2: la red corre en una tarea aparte (nucleo 0). La alarma local (contacto, buzzer, LED)
  corre en loop() y NO depende de Wi-Fi, del broker ni de internet.

  Pines:
    GPIO27 = contacto (puente a GND = puerta CERRADA, sin puente = ABIERTA)
    GPIO26 = buzzer activo (via 220 ohm)
    GPIO25 = LED (via 220 ohm)

  v3: el actuador responde a comandos MQTT en alarma/lab1/puerta01/comando:
      armar | desarmar | buzzer_on | buzzer_off | led_on | led_off | auto | alerta_on | alerta_off
      - los comandos buzzer_on/off y led_on/off fuerzan el actuador (prioridad sobre la logica local); "auto" devuelve el control local
      - alerta_on: escalada (el servicio la manda si la puerta pasa del umbral) -> buzzer intermitente hasta que se cierre la puerta
      El estado de los actuadores se publica (retenido, QoS 1) en .../actuadores
*/
#include <WiFi.h>
#include <MQTT.h>
#include <time.h>

// Credenciales en "secretos.h" (NO se sube al repositorio; copia secretos.h.ejemplo y llena tus datos)
#include "secretos.h"
const int MQTT_PORT = 1883;

#define PIN_CONTACTO 27
#define PIN_BUZZER   26
#define PIN_LED      25

const unsigned long DEBOUNCE_MS = 50;
const unsigned long TELEMETRIA_MS = 30000;
const unsigned long REINTENTO_ACK_MS = 5000;

#define BASE "alarma/lab1/puerta01/"
const char* T_ESTADO = BASE "estado";
const char* T_EVENTO = BASE "evento";
const char* T_DISP   = BASE "disponibilidad";
const char* T_ARMADO = BASE "armado";
const char* T_TELEM  = BASE "telemetria";
const char* T_CMD    = BASE "comando";
const char* T_RESP   = BASE "respuesta";
const char* T_ACK    = BASE "ack";
const char* T_ACT    = BASE "actuadores";

WiFiClient red;
MQTTClient mqtt(512);

volatile bool armada = true;
volatile bool puertaAbierta = false;     // estado confirmado (ya sin rebote)
volatile bool estadoPendiente = false;   // loop() pide publicar el estado
volatile int  buzzerForzado = -1;        // -1 = automatico, 0 = apagado, 1 = encendido (comando MQTT)
volatile int  ledForzado = -1;
volatile bool escalada = false;          // alerta_on: buzzer intermitente mientras la puerta siga abierta
volatile bool actPendiente = false;      // hay que publicar el estado de los actuadores
bool buzzerAct = false, ledAct = false;
int  lecturaPrev = -1;
int  estadoEstable = -1;
unsigned long tCambio = 0;
unsigned long contadorEv = 0;

// Eventos pendientes de ack (buffer de hasta 10). Lo comparten loop() y la tarea de red.
struct Pendiente { bool usado; bool enviado; String id; String tipo; bool armada; unsigned long ms; unsigned long long ts; unsigned long ultimoEnvio; };
Pendiente pend[10];
SemaphoreHandle_t mtx;

// ---------- utilidades ----------
unsigned long long ahoraEpoch() {
  time_t t; time(&t);
  return (t > 1700000000) ? (unsigned long long)t : 0;  // 0 si aun no hay hora NTP
}

void actuadores() {
  // Logica local (borde): no depende de la red. Un comando MQTT puede forzar el actuador.
  bool sonar = armada && puertaAbierta;
  if (escalada && puertaAbierta && sonar) sonar = ((millis() / 250) % 2) == 0;   // intermitente
  bool b = (buzzerForzado >= 0) ? (buzzerForzado == 1) : sonar;
  bool l = (ledForzado >= 0)    ? (ledForzado == 1)    : puertaAbierta;
  digitalWrite(PIN_BUZZER, b ? HIGH : LOW);
  digitalWrite(PIN_LED, l ? HIGH : LOW);
  if (b != buzzerAct || l != ledAct) {
    // se publica solo si cambia de verdad (la intermitencia no se reporta cada 250 ms)
    if (!(escalada && buzzerForzado < 0)) actPendiente = true;
    buzzerAct = b; ledAct = l;
  }
}

void publicaActuadores() {
  String m = String("{\"buzzer\":\"") + (buzzerAct ? "on" : "off") + "\",\"led\":\"" + (ledAct ? "on" : "off") +
             "\",\"modo\":\"" + ((buzzerForzado >= 0 || ledForzado >= 0) ? "manual" : (escalada ? "escalada" : "auto")) + "\"}";
  mqtt.publish(T_ACT, m, true, 1);   // retenido
}

void publicaEstado() {
  mqtt.publish(T_ESTADO, puertaAbierta ? "abierta" : "cerrada", true, 1);   // retenido
}
void publicaArmado() {
  mqtt.publish(T_ARMADO, armada ? "armado" : "desarmado", true, 1);         // retenido
}

void enviaEvento(Pendiente& p) {
  // El payload se arma al enviar: "hace_ms" = cuanto tiempo paso desde el evento (reloj interno, no necesita internet).
  // El servicio resta eso a su hora de recepcion y asi recupera la hora real aunque el evento haya esperado en el buffer.
  String payload = String("{\"event_id\":\"") + p.id + "\",\"tipo\":\"" + p.tipo +
                   "\",\"ts\":" + String((unsigned long)p.ts) +
                   ",\"hace_ms\":" + String(millis() - p.ms) +
                   ",\"armada\":" + (p.armada ? "true" : "false") + "}";
  mqtt.publish(T_EVENTO, payload, false, 1);   // QoS 1
  p.enviado = true;
  p.ultimoEnvio = millis();
}

// Se llama desde loop(): solo GUARDA el evento. La tarea de red lo envia cuando hay conexion.
void nuevoEvento(const char* tipo) {
  String id = String((uint32_t)ESP.getEfuseMac(), HEX) + "-" + String(++contadorEv) + "-" + String(millis());
  xSemaphoreTakeRecursive(mtx, portMAX_DELAY);
  bool guardado = false;
  for (auto& p : pend) {
    if (!p.usado) {
      p.usado = true; p.enviado = false; p.id = id; p.tipo = tipo; p.armada = armada;
      p.ms = millis(); p.ts = ahoraEpoch(); p.ultimoEnvio = 0; guardado = true; break;
    }
  }
  xSemaphoreGiveRecursive(mtx);
  if (!guardado) Serial.println("Buffer de eventos lleno, evento descartado");
}

// ---------- MQTT (corre en la tarea de red) ----------
void mensaje(String &topic, String &payload) {
  if (topic == T_CMD) {
    String c = payload; c.trim(); c.toLowerCase();
    bool conocido = true;
    if      (c == "desarmar")   armada = false;
    else if (c == "armar")      armada = true;
    else if (c == "buzzer_on")  buzzerForzado = 1;
    else if (c == "buzzer_off") buzzerForzado = 0;
    else if (c == "led_on")     ledForzado = 1;
    else if (c == "led_off")    ledForzado = 0;
    else if (c == "auto")     { buzzerForzado = -1; ledForzado = -1; escalada = false; }
    else if (c == "alerta_on")  escalada = puertaAbierta;
    else if (c == "alerta_off") escalada = false;
    else conocido = false;
    if (conocido) {
      actuadores();
      publicaArmado();
      publicaActuadores();
      mqtt.publish(T_RESP, "ok:" + c, false, 1);
      Serial.println("COMANDO " + c);
    } else {
      mqtt.publish(T_RESP, "error:comando_desconocido", false, 1);
    }
  } else if (topic == T_ACK) {
    xSemaphoreTakeRecursive(mtx, portMAX_DELAY);
    for (auto& p : pend) {
      if (p.usado && payload.indexOf(p.id) >= 0) { p.usado = false; Serial.println("ACK " + p.id); }
    }
    xSemaphoreGiveRecursive(mtx);
  }
}

void conectaMqtt() {
  Serial.print("MQTT...");
  if (mqtt.connect("esp_puerta01", MQTT_USER, MQTT_PASS)) {
    Serial.println(" OK");
    mqtt.subscribe(T_CMD, 1);
    mqtt.subscribe(T_ACK, 1);
    mqtt.publish(T_DISP, "online", true, 1);
    publicaEstado();
    publicaArmado();
    publicaActuadores();
  } else {
    Serial.println(" fallo");
  }
}

void tareaRed(void*) {
  unsigned long tMqtt = 0, tTelem = 0, tWifiMsg = 0;
  bool wifiAntes = false;
  for (;;) {
    bool wifiOk = (WiFi.status() == WL_CONNECTED);
    if (wifiOk != wifiAntes) {
      wifiAntes = wifiOk;
      if (wifiOk) { Serial.print("WiFi OK, IP "); Serial.println(WiFi.localIP()); }
      else        { Serial.println("WiFi perdido (la alarma local sigue funcionando)"); }
    }
    if (!wifiOk && millis() - tWifiMsg > 10000) {
      tWifiMsg = millis();
      Serial.println("Esperando WiFi...");
    }

    if (wifiOk) {
      if (mqtt.connected()) {
        mqtt.loop();

        if (estadoPendiente) { estadoPendiente = false; publicaEstado(); }
        if (actPendiente)    { actPendiente = false;    publicaActuadores(); }

        // enviar eventos nuevos y reintentar los que no tienen ack
        xSemaphoreTakeRecursive(mtx, portMAX_DELAY);
        for (auto& p : pend) {
          if (p.usado && (!p.enviado || millis() - p.ultimoEnvio > REINTENTO_ACK_MS)) enviaEvento(p);
        }
        xSemaphoreGiveRecursive(mtx);

        if (millis() - tTelem > TELEMETRIA_MS) {
          tTelem = millis();
          mqtt.publish(T_TELEM, String("{\"rssi\":") + WiFi.RSSI() + ",\"uptime\":" + (millis() / 1000) + "}", false, 0);
        }
      } else if (millis() - tMqtt > 3000) {
        tMqtt = millis();
        // al reconectar, todo lo pendiente se vuelve a enviar
        xSemaphoreTakeRecursive(mtx, portMAX_DELAY);
        for (auto& p : pend) p.enviado = false;
        xSemaphoreGiveRecursive(mtx);
        conectaMqtt();
      }
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// ---------- setup / loop ----------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_CONTACTO, INPUT_PULLUP);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  digitalWrite(PIN_LED, LOW);
  mtx = xSemaphoreCreateRecursiveMutex();

  // estado inicial (antes de tocar la red)
  lecturaPrev = digitalRead(PIN_CONTACTO);
  estadoEstable = lecturaPrev;
  puertaAbierta = (estadoEstable == HIGH);
  actuadores();

  // Wi-Fi sin bloquear: se reconecta solo si se cae
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTime(0, 0, "pool.ntp.org");            // epoch en UTC (si no hay internet, ts=0 y el servicio usa su hora)

  mqtt.begin(MQTT_HOST, MQTT_PORT, red);
  mqtt.setCleanSession(true);
  mqtt.setKeepAlive(30);
  mqtt.setTimeout(2000);
  mqtt.setWill(T_DISP, "offline", true, 1);   // LWT retenido
  mqtt.onMessage(mensaje);

  xTaskCreatePinnedToCore(tareaRed, "red", 8192, nullptr, 1, nullptr, 0);   // red en el nucleo 0
}

void loop() {
  // --- antirrebote ---
  int lectura = digitalRead(PIN_CONTACTO);
  if (lectura != lecturaPrev) { tCambio = millis(); lecturaPrev = lectura; }
  if ((millis() - tCambio) > DEBOUNCE_MS && lectura != estadoEstable) {
    estadoEstable = lectura;
    puertaAbierta = (estadoEstable == HIGH);       // HIGH = sin puente = abierta
    if (!puertaAbierta) escalada = false;          // al cerrar termina la escalada
    actuadores();                                  // alarma local inmediata
    nuevoEvento(puertaAbierta ? "apertura" : "cierre");
    estadoPendiente = true;
    Serial.println(puertaAbierta ? "ABIERTA" : "CERRADA");
  }
  if (escalada) actuadores();                      // mantiene el parpadeo del buzzer
  delay(2);
}
