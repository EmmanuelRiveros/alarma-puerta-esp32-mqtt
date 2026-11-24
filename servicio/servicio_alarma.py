#!/usr/bin/env python3
"""
Servicio de registro - Alarma de apertura de puerta (actividad final)

- Se suscribe a los topicos de la puerta, guarda los eventos en SQLite (sin duplicar por event_id)
  y responde con un ack de aplicacion por cada evento recibido.
- Calcula la DURACION de cada apertura (hora de cierre - hora de apertura).
- Metricas (promedio, minimo, maximo, desv. estandar) sobre una VENTANA DECLARADA (VENTANA_MIN, 60 min por defecto).
- Alerta por umbral: si una puerta armada pasa mas de UMBRAL_S segundos abierta, el dashboard avisa y el servicio
  publica el comando "alerta_on" (lazo cerrado: sensor -> MQTT -> servicio -> comando -> buzzer intermitente).
- Dashboard web: lectura en tiempo real, grafica historica con eje de tiempo, estado y control del actuador
  (buzzer/LED), estado de conexion (broker, dispositivo) y alerta.

Requisitos:  pip install paho-mqtt        (el resto es libreria estandar)
Ejecutar:    definir MQTT_PASS (contrasena del usuario del servicio) y correr  python servicio_alarma.py
             luego abrir http://localhost:8080
"""
import json
import os
import sqlite3
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

# ---------------- CONFIGURACION ----------------
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "registro_lab1")
MQTT_PASS = os.environ.get("MQTT_PASS", "")          # obligatoria: no hay contrasena en el codigo
VENTANA_MIN = float(os.environ.get("VENTANA_MIN", "60"))   # ventana de calculo de las metricas, en minutos
UMBRAL_S = float(os.environ.get("UMBRAL_S", "10"))         # alerta si la puerta pasa este tiempo abierta, en segundos
CLIENT_ID = "registro_lab1"          # fijo: la sesion persistente depende de este id
HTTP_HOST = os.environ.get("HTTP_HOST", "127.0.0.1")   # "0.0.0.0" para verlo desde otro equipo
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "alarma.db"))

BASE = "alarma/lab1/puerta01/"
T_ESTADO, T_EVENTO, T_DISP = BASE + "estado", BASE + "evento", BASE + "disponibilidad"
T_ARMADO, T_TELEM, T_CMD = BASE + "armado", BASE + "telemetria", BASE + "comando"
T_RESP, T_ACK, T_ACT = BASE + "respuesta", BASE + "ack", BASE + "actuadores"
# ------------------------------------------------

# Ultimo valor conocido de los topicos retenidos / telemetria (en memoria)
vivo = {"estado": "desconocido", "armado": "desconocido", "disponibilidad": "desconocido",
        "telemetria": None, "ultimo_mensaje": None, "actuadores": None}
alertadas = set()   # id de aperturas para las que ya se mando alerta_on
vivo_lock = threading.Lock()


def db_conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS eventos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            tipo TEXT NOT NULL,
            ts REAL NOT NULL,
            armada INTEGER,
            recibido REAL NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS aperturas(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            t_apertura REAL NOT NULL,
            t_cierre REAL,
            duracion REAL)""")


def procesar_evento(payload: bytes):
    """Guarda el evento. Devuelve el event_id (para el ack) o None si el mensaje es invalido.
    Un evento repetido (mismo event_id) NO se vuelve a guardar, pero igual se confirma."""
    try:
        d = json.loads(payload.decode("utf-8"))
        event_id = str(d["event_id"])
        tipo = str(d["tipo"])
        if tipo not in ("apertura", "cierre"):
            return None
    except Exception as e:
        print("Evento invalido:", payload[:80], e)
        return None

    ts = float(d.get("ts") or 0)
    ahora = time.time()
    if ts < 1_700_000_000:
        # El ESP32 no tenia hora (sin internet/NTP). "hace_ms" = tiempo desde el evento hasta el envio,
        # medido con el reloj interno del ESP32: hora del evento = hora de recepcion - hace_ms.
        hace = float(d.get("hace_ms") or 0) / 1000.0
        ts = ahora - hace
    armada = 1 if d.get("armada") else 0

    with db_conn() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO eventos(event_id,tipo,ts,armada,recibido) VALUES(?,?,?,?,?)",
            (event_id, tipo, ts, armada, ahora))
        if cur.rowcount == 0:
            print("Duplicado ignorado:", event_id)
            return event_id          # ya estaba guardado: solo reenviar el ack
        if tipo == "apertura":
            con.execute("INSERT INTO aperturas(t_apertura) VALUES(?)", (ts,))
        else:  # cierre: cerrar la apertura abierta mas reciente
            fila = con.execute(
                "SELECT id,t_apertura FROM aperturas WHERE t_cierre IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            if fila:
                dur = max(0.0, ts - fila["t_apertura"])
                con.execute("UPDATE aperturas SET t_cierre=?, duracion=? WHERE id=?", (ts, dur, fila["id"]))
                print(f"Apertura cerrada: {dur:.1f} s")
    print("Evento guardado:", tipo, event_id)
    return event_id


def apertura_en_curso():
    """Devuelve (id, segundos abierta) de la apertura sin cerrar mas reciente, o None."""
    with db_conn() as con:
        f = con.execute("SELECT id,t_apertura FROM aperturas WHERE t_cierre IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    return (f["id"], time.time() - f["t_apertura"]) if f else None


def metricas():
    desde = time.time() - VENTANA_MIN * 60
    with db_conn() as con:
        filas = [dict(r) for r in con.execute(
            "SELECT id,t_apertura,duracion FROM aperturas "
            "WHERE duracion IS NOT NULL AND t_apertura>=? ORDER BY t_apertura", (desde,))]
        por_hora = {f"{h:02d}": 0 for h in range(24)}
        for r in con.execute("SELECT strftime('%H', t_apertura,'unixepoch','localtime') h, COUNT(*) n "
                             "FROM aperturas WHERE t_apertura>=? GROUP BY h", (desde,)):
            por_hora[r["h"]] = r["n"]
        eventos = [dict(r) for r in con.execute(
            "SELECT tipo,ts,armada FROM eventos ORDER BY id DESC LIMIT 10")]
        total = con.execute("SELECT COUNT(*) FROM aperturas").fetchone()[0]
    durs = [r["duracion"] for r in filas]
    n = len(durs)
    serie = [{"t": r["t_apertura"], "d": round(r["duracion"], 2)} for r in filas[-200:]]   # historico con eje de tiempo
    en_curso = apertura_en_curso()
    largas = [x for x in durs if x > UMBRAL_S]
    alerta = None
    with vivo_lock:
        en_vivo = dict(vivo)
    if en_curso and en_curso[1] > UMBRAL_S and en_vivo["estado"] == "abierta":
        alerta = f"Puerta abierta hace {en_curso[1]:.0f} s (umbral {UMBRAL_S:g} s)"
    elif largas and filas and filas[-1]["duracion"] > UMBRAL_S:
        alerta = f"Ultima apertura duro {filas[-1]['duracion']:.1f} s (umbral {UMBRAL_S:g} s)"
    en_vivo["broker"] = "conectado" if cliente.is_connected() else "desconectado"
    return {
        "en_vivo": en_vivo,
        "ventana_min": VENTANA_MIN,
        "umbral_s": UMBRAL_S,
        "n": n,
        "total_aperturas": total,
        "promedio": round(statistics.mean(durs), 2) if n else None,
        "minimo": round(min(durs), 2) if n else None,
        "maximo": round(max(durs), 2) if n else None,
        "desviacion": round(statistics.stdev(durs), 2) if n >= 2 else (0.0 if n == 1 else None),
        "serie": serie,
        "por_hora": por_hora,
        "eventos": eventos,
        "alerta": alerta,
        "abierta_hace": round(en_curso[1], 1) if en_curso else None,
    }


def vigila_umbral():
    """Lazo cerrado: si la puerta sigue abierta pasado el umbral, el servicio MANDA el comando alerta_on."""
    while True:
        time.sleep(1)
        try:
            c = apertura_en_curso()
            with vivo_lock:
                armado, estado = vivo["armado"], vivo["estado"]
            if c and estado == "abierta" and armado == "armado" and c[1] > UMBRAL_S and c[0] not in alertadas:
                alertadas.add(c[0])
                print(f"ALERTA: puerta abierta {c[1]:.0f} s > {UMBRAL_S:g} s -> comando alerta_on")
                cliente.publish(T_CMD, "alerta_on", qos=1)
        except Exception as e:
            print("vigila_umbral:", e)


# ---------------- MQTT ----------------
cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                      protocol=mqtt.MQTTv311, clean_session=False)   # sesion persistente
cliente.username_pw_set(MQTT_USER, MQTT_PASS)
cliente.reconnect_delay_set(min_delay=1, max_delay=15)


def on_connect(c, userdata, flags, reason_code, properties):
    if reason_code != 0:
        print("Conexion MQTT rechazada:", reason_code)
        return
    print("MQTT conectado (sesion previa:", flags.session_present, ")")
    c.subscribe([(T_EVENTO, 1), (T_ESTADO, 1), (T_ARMADO, 1), (T_DISP, 1), (T_RESP, 1), (T_ACT, 1), (T_TELEM, 0)])


def on_message(c, userdata, msg):
    texto = msg.payload.decode("utf-8", errors="replace")
    with vivo_lock:
        vivo["ultimo_mensaje"] = time.time()
        if msg.topic == T_ESTADO:
            vivo["estado"] = texto
        elif msg.topic == T_ARMADO:
            vivo["armado"] = texto
        elif msg.topic == T_DISP:
            vivo["disponibilidad"] = texto
        elif msg.topic == T_ACT:
            try:
                vivo["actuadores"] = json.loads(texto)
            except ValueError:
                pass
        elif msg.topic == T_TELEM:
            try:
                vivo["telemetria"] = json.loads(texto)
            except ValueError:
                pass
    if msg.topic == T_EVENTO:
        event_id = procesar_evento(msg.payload)
        if event_id:
            # ack de APLICACION: "guarde este evento". No es lo mismo que el PUBACK de MQTT.
            c.publish(T_ACK, json.dumps({"event_id": event_id}), qos=1)


def on_disconnect(c, userdata, flags, reason_code, properties):
    print("MQTT desconectado:", reason_code, "(reintentando...)")


cliente.on_connect = on_connect
cliente.on_message = on_message
cliente.on_disconnect = on_disconnect

# ---------------- WEB ----------------
PAGINA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alarma de puerta - Dashboard</title>
<style>
:root{--bg:#081b33;--card:#10294a;--tx:#fff;--mut:#9fb4cc;--ac:#26b6d4;--ok:#2ecc71;--bad:#ff5a5f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,Segoe UI,Arial,sans-serif;padding:20px}
h1{font-size:22px;margin:0 0 4px}small{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border-radius:12px;padding:14px}
.card h3{margin:0 0 6px;font-size:13px;color:var(--mut);font-weight:600}
.big{font-size:30px;font-weight:700}.u{font-size:14px;color:var(--mut);font-weight:400}
.badge{display:inline-block;padding:4px 12px;border-radius:20px;font-weight:700;font-size:22px}
.abierta{background:var(--bad)}.cerrada{background:var(--ok);color:#06210f}.desconocido{background:#556}
button{background:var(--ac);color:#04202a;border:0;border-radius:8px;padding:8px 14px;font-weight:700;cursor:pointer;margin-right:6px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
svg{width:100%;height:190px;background:#0b2240;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:4px 6px;border-bottom:1px solid #1d3b66}
.alert{color:var(--bad)}
.banner{display:none;background:var(--bad);color:#fff;border-radius:10px;padding:12px 16px;font-weight:700;font-size:18px;margin:12px 0}
.banner.on{display:block;animation:p 1s infinite}@keyframes p{50%{opacity:.6}}
.okc{color:var(--ok)}button.sec{background:#1d3b66;color:#fff}
</style></head><body>
<h1>Alarma de apertura de puerta &middot; lab1/puerta01</h1>
<small id="sub">Conectando...</small>
<div id="banner" class="banner"></div>
<div class="grid">
 <div class="card"><h3>PUERTA (tiempo real)</h3><span id="estado" class="badge desconocido">--</span></div>
 <div class="card"><h3>ALARMA</h3><div class="big" id="armado" style="font-size:22px">--</div>
   <div style="margin-top:8px"><button onclick="cmd('armar')">Armar</button><button onclick="cmd('desarmar')">Desarmar</button></div></div>
 <div class="card"><h3>CONEXION</h3><div style="font-size:16px">Dispositivo: <b id="disp">--</b></div>
   <div style="font-size:16px">Broker: <b id="brk">--</b></div><small id="rssi"></small></div>
 <div class="card"><h3>ACTUADOR (buzzer / LED)</h3><div style="font-size:16px">Buzzer: <b id="abz">--</b> &middot; LED: <b id="aled">--</b></div>
   <small id="amodo"></small>
   <div style="margin-top:8px"><button onclick="cmd('buzzer_on')">Buzzer ON</button><button class="sec" onclick="cmd('buzzer_off')">OFF</button>
   <button onclick="cmd('led_on')">LED ON</button><button class="sec" onclick="cmd('led_off')">OFF</button><button class="sec" onclick="cmd('auto')">Auto</button></div></div>
 <div class="card"><h3>PROMEDIO</h3><div class="big" id="prom">--</div><span class="u">s por apertura</span><br><small id="vent"></small></div>
 <div class="card"><h3>MINIMO</h3><div class="big" id="min">--</div><span class="u">s</span></div>
 <div class="card"><h3>MAXIMO</h3><div class="big alert" id="max">--</div><span class="u">s</span></div>
 <div class="card"><h3>DESV. ESTANDAR</h3><div class="big" id="sd">--</div><span class="u">s</span></div>
 <div class="card"><h3>APERTURAS</h3><div class="big" id="tot">--</div><span class="u">registradas</span></div>
</div>
<div class="charts">
 <div class="card"><h3 id="t1">HISTORICO: DURACION DE CADA APERTURA VS HORA</h3><svg id="g1" viewBox="0 0 400 190"></svg></div>
 <div class="card"><h3>DURACION POR APERTURA (barras, s)</h3><svg id="g2" viewBox="0 0 400 190"></svg></div>
 <div class="card"><h3>APERTURAS POR HORA (en la ventana)</h3><svg id="g3" viewBox="0 0 400 190"></svg></div>
 <div class="card"><h3>ULTIMOS EVENTOS</h3><table id="ev"></table></div>
</div>
<script>
const f=(v)=>v===null||v===undefined?'--':v;
function hm(t){const d=new Date(t*1000);return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}
function lineaT(id,serie,ventMin,umbral){const el=document.getElementById(id);
 const W=400,H=190,pl=34,pr=10,pt=14,pb=26,ahora=Date.now()/1000,t0=ahora-ventMin*60;
 const mx=Math.max(umbral*1.3,...serie.map(p=>p.d),1);
 const X=t=>pl+(t-t0)/(ahora-t0)*(W-pl-pr),Y=v=>H-pb-(v/mx)*(H-pt-pb);
 let h='<line x1="'+pl+'" y1="'+(H-pb)+'" x2="'+(W-pr)+'" y2="'+(H-pb)+'" stroke="#35557f"/><line x1="'+pl+'" y1="'+pt+'" x2="'+pl+'" y2="'+(H-pb)+'" stroke="#35557f"/>';
 for(let i=0;i<=4;i++){const t=t0+i*(ahora-t0)/4;h+='<text x="'+X(t)+'" y="'+(H-8)+'" fill="#9fb4cc" font-size="10" text-anchor="middle">'+hm(t)+'</text>'}
 for(let i=0;i<=2;i++){const v=mx*i/2;h+='<text x="'+(pl-4)+'" y="'+(Y(v)+3)+'" fill="#9fb4cc" font-size="10" text-anchor="end">'+v.toFixed(0)+'</text>'}
 h+='<line x1="'+pl+'" y1="'+Y(umbral)+'" x2="'+(W-pr)+'" y2="'+Y(umbral)+'" stroke="#ff5a5f" stroke-dasharray="5 4"/><text x="'+(W-pr)+'" y="'+(Y(umbral)-3)+'" fill="#ff5a5f" font-size="10" text-anchor="end">umbral '+umbral+' s</text>';
 const pts=serie.filter(p=>p.t>=t0).map(p=>[X(p.t),Y(p.d),p.d]);
 if(pts.length>1)h+='<polyline fill="none" stroke="#26b6d4" stroke-width="2" points="'+pts.map(q=>q[0]+','+q[1]).join(' ')+'"/>';
 h+=pts.map(q=>'<circle cx="'+q[0]+'" cy="'+q[1]+'" r="4" fill="'+(q[2]>umbral?'#ff5a5f':'#26b6d4')+'"/>').join('');
 h+='<text x="'+(pl+4)+'" y="10" fill="#9fb4cc" font-size="10">s</text>';
 el.innerHTML=h}
function barras(id,vals,labels,marca){const s=document.getElementById(id);if(!vals.length){s.innerHTML='';return}
 const W=400,H=190,p=24,mx=Math.max(...vals)||1,bw=(W-2*p)/vals.length;let h='';
 vals.forEach((v,i)=>{const bh=(v/mx)*(H-2*p-8);const c=marca&&v===mx&&v>0?'#ff5a5f':'#26b6d4';
  h+='<rect x="'+(p+i*bw+1)+'" y="'+(H-p-bh)+'" width="'+Math.max(bw-2,1)+'" height="'+bh+'" fill="'+c+'"/>';
  if(labels&&(vals.length<=12||i%2==0))h+='<text x="'+(p+i*bw+bw/2)+'" y="'+(H-8)+'" fill="#9fb4cc" font-size="9" text-anchor="middle">'+labels[i]+'</text>'});
 s.innerHTML=h}
async function act(){try{const m=await (await fetch('/api/metricas')).json();const v=m.en_vivo;
 const e=document.getElementById('estado');e.textContent=v.estado.toUpperCase();e.className='badge '+(v.estado==='abierta'||v.estado==='cerrada'?v.estado:'desconocido');
 document.getElementById('armado').textContent=v.armado.toUpperCase();
 document.getElementById('disp').textContent=v.disponibilidad.toUpperCase();
 const br=document.getElementById('brk');br.textContent=v.broker.toUpperCase();br.className=v.broker==='conectado'?'okc':'alert';
 const ac=v.actuadores;document.getElementById('abz').textContent=ac?ac.buzzer.toUpperCase():'--';
 document.getElementById('aled').textContent=ac?ac.led.toUpperCase():'--';
 document.getElementById('amodo').textContent=ac?('modo: '+ac.modo):'';
 const bn=document.getElementById('banner');if(m.alerta){bn.textContent='ALERTA: '+m.alerta;bn.className='banner on'}else{bn.className='banner'}
 document.getElementById('vent').textContent='ventana: ultimos '+m.ventana_min+' min';
 document.getElementById('rssi').textContent=v.telemetria?('RSSI '+v.telemetria.rssi+' dBm'):'';
 document.getElementById('prom').textContent=f(m.promedio);document.getElementById('min').textContent=f(m.minimo);
 document.getElementById('max').textContent=f(m.maximo);document.getElementById('sd').textContent=f(m.desviacion);
 document.getElementById('tot').textContent=m.total_aperturas;
 lineaT('g1',m.serie,m.ventana_min,m.umbral_s);barras('g2',m.serie.slice(-20).map(u=>u.d),null,true);
 const hs=Object.keys(m.por_hora);barras('g3',hs.map(h=>m.por_hora[h]),hs,false);
 document.getElementById('ev').innerHTML='<tr><th>Hora</th><th>Evento</th><th>Armada</th></tr>'+m.eventos.map(x=>
  '<tr><td>'+new Date(x.ts*1000).toLocaleTimeString()+'</td><td>'+x.tipo+'</td><td>'+(x.armada?'si':'no')+'</td></tr>').join('');
 document.getElementById('sub').textContent='Actualizado '+new Date().toLocaleTimeString()+' - n='+m.n+' aperturas en la ventana de '+m.ventana_min+' min';
}catch(e){document.getElementById('sub').textContent='Sin conexion con el servicio'}}
async function cmd(a){await fetch('/api/comando',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({accion:a})});setTimeout(act,500)}
act();setInterval(act,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _enviar(self, codigo, tipo, cuerpo: bytes):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        if self.path == "/api/metricas":
            self._enviar(200, "application/json", json.dumps(metricas()).encode())
        elif self.path in ("/", "/index.html"):
            self._enviar(200, "text/html; charset=utf-8", PAGINA.encode())
        else:
            self._enviar(404, "text/plain", b"no encontrado")

    def do_POST(self):
        if self.path != "/api/comando":
            return self._enviar(404, "text/plain", b"no encontrado")
        try:
            largo = int(self.headers.get("Content-Length", 0))
            accion = json.loads(self.rfile.read(largo))["accion"]
            if accion not in ("armar", "desarmar", "buzzer_on", "buzzer_off", "led_on", "led_off", "auto"):
                raise ValueError
        except Exception:
            return self._enviar(400, "application/json", b'{"ok":false}')
        cliente.publish(T_CMD, accion, qos=1)
        self._enviar(200, "application/json", b'{"ok":true}')

    def log_message(self, *a):   # sin ruido en consola
        pass


def main():
    init_db()
    if not MQTT_PASS:
        raise SystemExit("Falta la variable de entorno MQTT_PASS (contrasena del usuario %s)" % MQTT_USER)
    threading.Thread(target=vigila_umbral, daemon=True).start()
    cliente.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    cliente.loop_start()
    srv = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    print(f"Dashboard en http://{HTTP_HOST}:{HTTP_PORT}  |  broker {MQTT_HOST}:{MQTT_PORT}  |  ventana {VENTANA_MIN:g} min  |  umbral {UMBRAL_S:g} s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cliente.loop_stop()
        cliente.disconnect()


if __name__ == "__main__":
    main()
