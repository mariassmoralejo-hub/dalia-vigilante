#!/usr/bin/env python3
"""Vigilante de emergencias de Dalia. Solo biblioteca estandar; independiente de n8n.

Cada ejecucion (programada cada minuto):
  1) llama a emergencia_vigilar_lab() (detecta y para automaticamente si procede),
  2) lee avisos pendientes y los envia: graves (salon/plataforma) -> correo+SMS a Maria y aviso a la
     propietaria del salon detenido; resto -> correo a Maria,
  3) marca como avisados SOLO los que se han enviado de verdad (si un canal falla, se reintenta).
Configuracion (fuera del codigo, permisos 600; en la nube, como variables de entorno/secretos; HC_PING_URL opcional):
  ~/.dalia_secretos/vigilante_<env>.env : VIG_HOST, VIG_USER, VIG_PASSWORD, VIG_ESQUEMA
  ~/.dalia_secretos/avisos.env : SMTP_USER, SMTP_APP_PASSWORD, TELNYX_ALERT_KEY, TELNYX_ALERT_FROM, TELNYX_ALERT_PROFILE
Uso: vigilante_emergencias.py [--env test|prod] [--simular]   (--simular: imprime, no envia ni marca)
"""
import os, sys, json, ssl, smtplib, urllib.request, urllib.error
from email.message import EmailMessage

ENV = "prod" if "prod" in sys.argv else "test"
SIMULAR = "--simular" in sys.argv
PUBLICO = bool(os.environ.get("GITHUB_ACTIONS"))  # logs publicos: no escribir nada que identifique salones, clientas ni numeros


def det(*a, **k):
    if not PUBLICO:
        print(*a, **k)


def err(msg, e):
    print("   FALLO %s: %s" % (msg, type(e).__name__ if PUBLICO else e), file=sys.stderr)
D = os.path.expanduser("~/.dalia_secretos/")


def leer_env(nombre):
    d = {}
    try:
        for l in open(D + nombre):
            if "=" in l and not l.startswith("#"):
                k, v = l.rstrip("\n").split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


def _con_entorno(d, claves):
    for k in claves:  # en la nube las credenciales llegan como variables de entorno (secretos del servicio)
        if os.environ.get(k):
            d[k] = os.environ[k]
    return d


DB = _con_entorno(leer_env("vigilante_%s.env" % ENV), ["VIG_HOST", "VIG_USER", "VIG_PASSWORD", "VIG_ESQUEMA"])
AV = _con_entorno(leer_env("avisos.env"), ["SMTP_USER", "SMTP_APP_PASSWORD", "TELNYX_ALERT_KEY", "TELNYX_ALERT_FROM", "TELNYX_ALERT_PROFILE", "HC_PING_URL"])
ESQ = DB.get("VIG_ESQUEMA", "dalia_test")


def sql(query, params=None):
    url = "postgres://%s:%s@%s/neondb?sslmode=require" % (DB["VIG_USER"], DB["VIG_PASSWORD"], DB["VIG_HOST"])
    req = urllib.request.Request("https://%s/sql" % DB["VIG_HOST"], data=json.dumps({"query": query, "params": params or []}).encode(),
                                 headers={"Neon-Connection-String": url, "Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))["rows"]


def enviar_correo(destino, asunto, texto):
    if not (AV.get("SMTP_USER") and AV.get("SMTP_APP_PASSWORD")):
        raise RuntimeError("correo sin configurar")
    m = EmailMessage(); m["From"] = AV["SMTP_USER"]; m["To"] = destino; m["Subject"] = asunto; m.set_content(texto)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(AV["SMTP_USER"], AV["SMTP_APP_PASSWORD"]); s.send_message(m)


def enviar_sms(destino, texto):
    if not (AV.get("TELNYX_ALERT_KEY") and AV.get("TELNYX_ALERT_FROM")):
        raise RuntimeError("SMS sin configurar")
    cuerpo = {"from": AV["TELNYX_ALERT_FROM"], "to": destino, "text": texto[:300]}
    if AV.get("TELNYX_ALERT_PROFILE"):
        cuerpo["messaging_profile_id"] = AV["TELNYX_ALERT_PROFILE"]
    req = urllib.request.Request("https://api.telnyx.com/v2/messages", data=json.dumps(cuerpo).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + AV["TELNYX_ALERT_KEY"], "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def textos(ev, salon):
    t, n, neg, det = ev["tipo"], ev["nivel"], ev["negocio_id"], ev["detalle"]
    if isinstance(det, str):
        det = json.loads(det)
    nombre = salon or neg or "plataforma"
    if n == "plataforma":
        return ("Dalia: EMERGENCIA, plataforma detenida (%s)" % t, "EMERGENCIA Dalia: toda la plataforma esta detenida (%s). Requiere tu autorizacion para reactivar. Detalle: %s" % (t, json.dumps(det)), None)
    if n == "salon":
        maria = "EMERGENCIA Dalia: salon %s detenido (%s). Los demas salones siguen activos. Requiere tu autorizacion para reactivar. Detalle: %s" % (nombre, t, json.dumps(det))
        prop = "Hola. Por seguridad, Dalia ha quedado en pausa en %s por un motivo tecnico. Tus clientas pueden seguir escribiendote directamente y tu equipo puede atenderlas con normalidad. Ya estamos revisandolo y te avisaremos al reactivarla." % nombre
        return ("Dalia: EMERGENCIA, salon %s detenido (%s)" % (nombre, t), maria, prop)
    if t == "PARADA_LEVANTADA":
        prop = "Dalia vuelve a estar activa en %s. Gracias por tu paciencia." % nombre
        return ("Dalia: reactivada en %s" % nombre, "Parada levantada en %s. %s" % (nombre, json.dumps(det)), prop)
    if n == "numero":
        return ("Dalia: numero bloqueado en %s" % nombre, "Aviso Dalia: numero %s bloqueado 30 min en %s (%s). %s" % (ev["numero"], nombre, t, json.dumps(det)), None)
    return ("Dalia: aviso %s" % t, "Aviso Dalia (%s) %s: %s" % (t, nombre, json.dumps(det)), None)


def enviar_codigos():
    """Envia los codigos de verificacion de contactos de emergencia de salones nuevos."""
    n = 0
    for v in sql("select id,canal,destino,codigo,salon from %s.emergencia_verificaciones_pendientes_lab()" % ESQ):
        texto = "Dalia: tu codigo de verificacion para %s es %s. Caduca en 15 minutos. Si no lo has pedido, ignora este mensaje." % (v["salon"] or "tu salon", v["codigo"])
        det("codigo de verificacion ->", v["canal"], "(%s)" % v["salon"])
        if SIMULAR:
            continue
        try:
            if v["canal"] == "sms":
                enviar_sms(v["destino"], texto)
            else:
                enviar_correo(v["destino"], "Dalia: codigo de verificacion", texto)
            sql("select %s.emergencia_verificacion_enviada_lab($1::bigint)" % ESQ, [v["id"]]); n += 1
        except Exception as e:
            err("codigo " + v["canal"], e)
    return n


def main():
    res = sql("select %s.emergencia_vigilar_lab() as r" % ESQ)
    det("vigilar:", res[0]["r"])
    print("paradas nuevas:", len(res[0]["r"].get("paradas", [])))
    if not SIMULAR:
        sql("select %s.emergencia_latido_lab()" % ESQ)
    enviar_codigos()
    pend = sql("select id,negocio_id,numero,tipo,nivel,detalle from %s.emergencia_avisos_pendientes_lab()" % ESQ)
    ok_ids = []
    for ev in pend:
        grave = ev["nivel"] in ("salon", "plataforma") or ev["tipo"] == "PARADA_LEVANTADA"
        dest = sql("select destinatario,canal,destino,salon from %s.emergencia_destinos_lab($1)" % ESQ, [ev["negocio_id"]])
        asunto, para_maria, para_prop = textos(ev, dest[0]["salon"] if dest else None)
        fallos = 0; enviados = 0
        for d in dest:
            if d["destinatario"] == "maria":
                if d["canal"] == "sms" and not grave:
                    continue
                texto = para_maria
            else:
                if not para_prop:
                    continue
                texto = para_prop
            det("%s -> %s/%s: %s" % (ev["tipo"], d["destinatario"], d["canal"], texto[:90]))
            if SIMULAR:
                continue
            try:
                (enviar_sms if d["canal"] == "sms" else lambda a, b: enviar_correo(a, asunto, b))(d["destino"], texto)
                enviados += 1
            except Exception as e:
                fallos += 1
                err("%s a %s" % (d["canal"], d["destinatario"]), e)
        if not SIMULAR and fallos == 0:
            ok_ids.append(ev["id"])
    if ok_ids:
        sql("select %s.emergencia_marcar_avisado_lab($1::bigint[])" % ESQ, ["{%s}" % ",".join(map(str, ok_ids))])
    print("pendientes:", len(pend), "marcados:", len(ok_ids))


def ping(sufijo=""):
    """Latido hacia el servicio independiente que avisa si el vigilante deja de ejecutarse (dead-man)."""
    url = AV.get("HC_PING_URL")
    if url and not SIMULAR:
        try:
            urllib.request.urlopen(url + sufijo, timeout=15).read()
        except Exception as e:
            err("latido", e)


if __name__ == "__main__":
    try:
        main()
        ping()
    except Exception:
        ping("/fail")
        raise
