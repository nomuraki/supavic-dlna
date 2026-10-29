#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dashboard de SupaVic DLNA — proceso INDEPENDIENTE del servidor.

Junta dos fuentes:
  1. datos/eventos.jsonl  — lo que escribe main.py: navegación, transferencias (formato, bytes,
                            velocidad, completas o cortadas), descubrimientos, reinicios.
  2. UPnP de los reproductores (WiiM) — lo que REALMENTE está sonando: pista, posición,
                            estado y volumen. Con eso arma el historial de escucha
                            (datos/historial.jsonl).

Se puede detener, reiniciar o modificar sin tocar el servidor. Solo usa la librería estándar.

Uso:
  python dashboard.py            # panel en http://<ip>:<dashboard.puerto>/  (8202 por defecto)
  python dashboard.py -v         # log detallado
  python dashboard.py --ping     # sale con 0 si responde
"""
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import threading
import time
import http.client
from collections import Counter, OrderedDict, defaultdict, deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _esc

import i18n
from i18n import T

APP = "SupaVic DLNA"


def _dir_app():
    """Misma carpeta de datos que main.py (junto al código, o %LOCALAPPDATA%\\SupaVic DLNA con el ejecutable)."""
    if os.environ.get("SUPAVIC_HOME"):
        return Path(os.environ["SUPAVIC_HOME"]).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / APP if base else Path.home() / ".supavic-dlna"
    return Path(__file__).resolve().parent


DIR_BASE = _dir_app()
DIR_DATOS = DIR_BASE / "datos"
RUTA_CONFIG = DIR_BASE / "config.json"
RUTA_EVENTOS = DIR_DATOS / "eventos.jsonl"
RUTA_HIST = DIR_DATOS / "historial.jsonl"
RUTA_LOG = DIR_DATOS / "dashboard.log"
ES_WINDOWS = os.name == "nt"
VERSION = "1.3.0"
log = logging.getLogger("dashboard")

NS_AVT = "urn:schemas-upnp-org:service:AVTransport:1"
NS_RC = "urn:schemas-upnp-org:service:RenderingControl:1"
ST_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
MIN_ESCUCHA = 10            # segundos mínimos reproducidos para contar en el historial


def leer_config():
    try:
        c = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        c = {}
    s, d = c.get("servidor", {}), c.get("dashboard", {})
    return {
        "nombre": s.get("nombre") or APP,
        "idioma": s.get("idioma", "auto"),
        "puerto_servidor": int(s.get("puerto", 8200)),
        "puerto": int(d.get("puerto", 8202)),
        "renderers": list(d.get("renderers", []) or []),
    }


def hms_a_seg(t):
    m = re.match(r"\s*(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", t or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def ips_locales():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def dia(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def formato_txt(ext, meta):
    ext = (ext or "").upper()
    sr, bits = meta.get("sr"), meta.get("bits")
    if sr and bits:
        return f"{ext} {bits}/{sr / 1000:g}"
    if sr:
        br = meta.get("br")
        return f"{ext} {round(br / 1000)}k" if br else f"{ext} {sr / 1000:g} kHz"
    return ext or "?"


# ═══════════════════════════════════════════════════════════════════════════ eventos del servidor

class Actividad:
    """Lee eventos.jsonl de forma incremental (abre/lee/cierra: no bloquea la rotación en Windows)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pos = 0
        self.resto = b""
        self.activos = OrderedDict()                       # id -> stream_inicio
        self.metas = {}                                    # (key, rel) -> {meta, ext, tam, raiz}
        self.servidas = deque(maxlen=3000)                 # transferencias agrupadas por pista
        self.feed = deque(maxlen=400)
        self.bytes_dia = defaultdict(int)
        self.servidas_dia = defaultdict(int)
        self.nav_dia = defaultdict(int)
        self.clientes = {}
        self.cargar_inicial()

    def cargar_inicial(self):
        archivos = [RUTA_EVENTOS.with_name(f"{RUTA_EVENTOS.name}.{i}") for i in range(5, 0, -1)] + [RUTA_EVENTOS]
        n = 0
        for a in archivos:
            try:
                with open(a, "rb") as fh:
                    datos = fh.read()
            except OSError:
                continue
            for linea in datos.splitlines():
                n += self._linea(linea)
            if a == RUTA_EVENTOS:
                self.pos = len(datos)
        # transferencias "activas" viejas del arranque no son reales
        limite = time.time() - 120
        for k in [k for k, v in self.activos.items() if v["t"] < limite]:
            self.activos.pop(k)
        log.info("Historical events loaded: %d", n)

    def _linea(self, linea):
        linea = linea.strip()
        if not linea:
            return 0
        try:
            ev = json.loads(linea.decode("utf-8", "replace"))
        except ValueError:
            return 0
        with self.lock:
            self.procesar(ev)
        return 1

    def leer_nuevos(self):
        try:
            tam = RUTA_EVENTOS.stat().st_size
        except OSError:
            return
        if tam < self.pos:                                 # rotó: termina de leer el .1 y empieza de cero
            rot = RUTA_EVENTOS.with_name(RUTA_EVENTOS.name + ".1")
            try:
                with open(rot, "rb") as fh:
                    fh.seek(self.pos)
                    for linea in (self.resto + fh.read()).splitlines():
                        self._linea(linea)
            except OSError:
                pass
            self.pos, self.resto = 0, b""
        if tam == self.pos:
            return
        try:
            with open(RUTA_EVENTOS, "rb") as fh:
                fh.seek(self.pos)
                datos = fh.read()
        except OSError:
            return
        self.pos += len(datos)
        datos = self.resto + datos
        lineas = datos.split(b"\n")
        self.resto = lineas.pop()                          # línea incompleta (se está escribiendo)
        for linea in lineas:
            self._linea(linea)

    def procesar(self, ev):
        t, tipo = ev.get("t", 0), ev.get("tipo")
        ip = ev.get("ip", "")
        if ip:
            c = self.clientes.setdefault(ip, {"ip": ip, "ua": "", "ultimo": 0})
            c["ultimo"] = max(c["ultimo"], t)
            if ev.get("ua"):
                c["ua"] = ev["ua"]
        if tipo == "stream_inicio":
            self.activos[ev["id"]] = ev
            self.metas[(ev["key"], ev["rel"])] = {"meta": ev.get("meta", {}), "ext": ev.get("ext"),
                                                  "tam": ev.get("tam"), "raiz": ev.get("raiz")}
            prev = self.feed[-1] if self.feed else None
            dup = prev and prev["tipo"] == "stream" and prev.get("rel") == ev["rel"] and prev["ip"] == ip and t - prev["t"] < 120
            if ev.get("ini", 0) == 0 and not dup:
                m = ev.get("meta", {})
                self.feed.append({"rel": ev["rel"], **{"t": t, "tipo": "stream", "ip": ip,
                                  "txt": f"{m.get('artist') or '?'} — {m.get('title') or ev['rel'].rsplit('/', 1)[-1]}",
                                  "det": formato_txt(ev.get("ext"), m)}})
        elif tipo == "stream":
            ini = self.activos.pop(ev.get("id"), None)
            info = self.metas.get((ev.get("key"), ev.get("rel")), {})
            env = ev.get("enviados", 0)
            self.bytes_dia[dia(t)] += env
            ultimo = self.servidas[-1] if self.servidas else None
            clave = (ip, ev.get("key"), ev.get("rel"))
            if ultimo and ultimo["clave"] == clave and t - ultimo["t_fin"] < 900:
                ultimo["t_fin"] = t
                ultimo["bytes"] += env
                ultimo["peticiones"] += 1
                ultimo["seg"] += ev.get("seg", 0)
                ultimo["estado"] = ev.get("estado")
            else:
                self.servidas_dia[dia(t)] += 1
                self.servidas.append({
                    "clave": clave, "t": ini["t"] if ini else t, "t_fin": t, "ip": ip, "raiz": ev.get("raiz"),
                    "rel": ev.get("rel"), "ext": ev.get("ext"), "tam": ev.get("tam"), "bytes": env,
                    "seg": ev.get("seg", 0), "peticiones": 1, "estado": ev.get("estado"),
                    "meta": info.get("meta", {})})
            if ev.get("estado") != "completo":
                self.feed.append({"t": t, "tipo": "corte", "ip": ip, "txt": ev.get("rel", "").rsplit("/", 1)[-1],
                                  "det": T("d.corte", estado=T("d." + str(ev.get("estado"))), mb=f"{env / 1048576:.1f}")})
        elif tipo == "navegacion":
            self.nav_dia[dia(t)] += 1
            prev = self.feed[-1] if self.feed else None
            if prev and prev["tipo"] == "nav" and prev["ip"] == ip and prev["txt"] == ev.get("carpeta") and t - prev["t"] < 30:
                prev["t"] = t                              # paginado de la misma carpeta: una sola línea
            else:
                self.feed.append({"t": t, "tipo": "nav", "ip": ip, "txt": ev.get("carpeta", ""),
                                  "det": T("d.elementos", n=ev.get("total", 0), ms=f"{ev.get('ms', 0):.0f}")})
        elif tipo == "descubrimiento":
            self.feed.append({"t": t, "tipo": "desc", "ip": ip, "txt": T("d.descubrimiento"),
                              "det": (ev.get("ua") or "")[:60]})
        elif tipo == "inicio":
            self.activos.clear()
            self.feed.append({"t": t, "tipo": "inicio", "ip": "", "txt": T("d.inicio", v=ev.get("version")),
                              "det": T("d.puerto", p=ev.get("puerto"))})
        elif tipo == "precarga":
            a = ev.get("accion")
            txt = T("d.pre." + a) if a in ("inicio", "pausa", "fin") else T("d.pre")
            det = "" if a == "inicio" else T("d.pre.det", p=ev.get("pistas", 0), c=ev.get("portadas", 0))
            if a == "fin":
                det += T("d.pre.det_fin", n=ev.get("carpetas", 0), m=ev.get("seg", 0) // 60)
            self.feed.append({"t": t, "tipo": "precarga", "ip": "", "txt": txt, "det": det})
        elif tipo == "config":
            self.feed.append({"t": t, "tipo": "config", "ip": "", "txt": T("d.config"),
                              "det": T("d.reinicio") if ev.get("reiniciar") else ""})

    def info_uri(self, uri, puerto_servidor):
        """Si la URI del reproductor es de nuestro servidor, devuelve su meta/formato."""
        try:
            u = urlparse(uri)
        except ValueError:
            return None
        m = re.match(r"/m/([0-9a-f]{8})/(.*)", u.path or "")
        if not m or u.port != puerto_servidor:
            return None
        key, rel = m.group(1), unquote(m.group(2))
        with self.lock:
            info = self.metas.get((key, rel))
        return {"key": key, "rel": rel, **(info or {})}


# ═══════════════════════════════════════════════════════════════════════════ reproductores (UPnP)

def soap(url, servicio, accion, args=(), timeout=3):
    cuerpo = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
              's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
              f'<u:{accion} xmlns:u="{servicio}">' + "".join(f"<{k}>{_esc(str(v))}</{k}>" for k, v in args)
              + f"</u:{accion}></s:Body></s:Envelope>").encode("utf-8")
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        c.request("POST", u.path + (f"?{u.query}" if u.query else ""), cuerpo, {
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{servicio}#{accion}"'})
        r = c.getresponse()
        datos = r.read()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
    finally:
        c.close()
    root = ET.fromstring(datos)
    out = {}
    for e in root.iter():
        nombre = e.tag.rsplit("}", 1)[-1]
        if len(e) == 0:
            out[nombre] = e.text or ""
    return out


def parse_didl(texto):
    if not texto or texto.strip() in ("", "NOT_IMPLEMENTED"):
        return {}
    try:
        root = ET.fromstring(texto)
    except ET.ParseError:
        return {}
    item = next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "item"), None)
    if item is None:
        return {}
    m = {}
    for e in item:
        n = e.tag.rsplit("}", 1)[-1]
        if n == "title":
            m["title"] = e.text
        elif n == "artist" and not e.get("role") and "artist" not in m:
            m["artist"] = e.text
        elif n == "creator" and "artist" not in m:
            m["artist"] = e.text
        elif n == "album":
            m["album"] = e.text
        elif n == "albumArtURI" and "art" not in m:
            m["art"] = e.text
        elif n == "res":
            m["res"] = dict(e.attrib)
            m["res_uri"] = (e.text or "").strip()
    return m


class Reproductor:
    def __init__(self, location):
        self.location = location
        self.nombre = urlparse(location).hostname
        self.modelo = ""
        self.udn = location
        self.ip = urlparse(location).hostname
        self.avt = self.rc = None
        self.estado = "DESCONOCIDO"
        self.uri = ""
        self.meta = {}
        self.pos = 0.0
        self.dur = 0.0
        self.vol = None
        self.mute = False
        self.actualizado = 0
        self.fallos = 0
        self.sesion = None                                 # pista en curso para el historial

    def describir(self):
        u = urlparse(self.location)
        c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=4)
        try:
            c.request("GET", u.path or "/")
            datos = c.getresponse().read()
        finally:
            c.close()
        root = ET.fromstring(datos)

        def txt(tag):
            e = next((x for x in root.iter() if x.tag.rsplit("}", 1)[-1] == tag), None)
            return (e.text or "").strip() if e is not None else ""
        base = txt("URLBase") or self.location
        self.nombre = txt("friendlyName") or self.nombre
        self.modelo = " ".join(x for x in (txt("manufacturer"), txt("modelName")) if x)
        self.udn = txt("UDN") or self.location
        for s in root.iter():
            if s.tag.rsplit("}", 1)[-1] != "service":
                continue
            d = {x.tag.rsplit("}", 1)[-1]: (x.text or "").strip() for x in s}
            st = d.get("serviceType", "")
            if st.startswith("urn:schemas-upnp-org:service:AVTransport:"):
                self.avt = (urljoin(base, d.get("controlURL", "")), st)
            elif st.startswith("urn:schemas-upnp-org:service:RenderingControl:"):
                self.rc = (urljoin(base, d.get("controlURL", "")), st)
        return self.avt is not None

    def sondear(self, con_volumen):
        url, st = self.avt
        ti = soap(url, st, "GetTransportInfo", [("InstanceID", 0)])
        pi = soap(url, st, "GetPositionInfo", [("InstanceID", 0)])
        self.estado = ti.get("CurrentTransportState", "DESCONOCIDO")
        self.uri = pi.get("TrackURI", "") or ""
        self.meta = parse_didl(pi.get("TrackMetaData", ""))
        self.pos = hms_a_seg(pi.get("RelTime"))
        self.dur = hms_a_seg(pi.get("TrackDuration")) or hms_a_seg((self.meta.get("res") or {}).get("duration"))
        if con_volumen and self.rc:
            try:
                v = soap(self.rc[0], self.rc[1], "GetVolume", [("InstanceID", 0), ("Channel", "Master")])
                self.vol = int(v.get("CurrentVolume", 0))
                m = soap(self.rc[0], self.rc[1], "GetMute", [("InstanceID", 0), ("Channel", "Master")])
                self.mute = m.get("CurrentMute") in ("1", "true")
            except Exception:
                pass
        self.actualizado = time.time()
        self.fallos = 0


class Reproductores:
    def __init__(self, dash):
        self.dash = dash
        self.lock = threading.Lock()
        self.items = OrderedDict()                         # location -> Reproductor
        self.hist = deque(maxlen=20000)
        self.hist_lock = threading.Lock()
        self.cargar_historial()

    def cargar_historial(self):
        try:
            with open(RUTA_HIST, "r", encoding="utf-8") as fh:
                for linea in fh:
                    try:
                        self.hist.append(json.loads(linea))
                    except ValueError:
                        pass
        except OSError:
            pass
        log.info("Listening history: %d records", len(self.hist))

    # ---- descubrimiento
    def descubrir(self):
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 2\r\nST: {ST_RENDERER}\r\n\r\n").encode()
        encontrados = set(self.dash.cfg["renderers"])
        socks = []
        for ip in ips_locales() or ["0.0.0.0"]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                s.bind((ip, 0))
                s.settimeout(0.3)
                s.sendto(msg, ("239.255.255.250", 1900))
                socks.append(s)
            except OSError:
                pass
        fin = time.time() + 3
        while time.time() < fin and socks:
            for s in socks:
                try:
                    datos, _ = s.recvfrom(4096)
                except OSError:
                    continue
                m = re.search(r"(?im)^location:\s*(\S+)", datos.decode("utf-8", "replace"))
                if m:
                    encontrados.add(m.group(1))
        for s in socks:
            s.close()
        for loc in encontrados:
            with self.lock:
                if loc in self.items:
                    continue
            r = Reproductor(loc)
            try:
                if not r.describir():
                    continue
            except Exception as e:
                log.debug("Could not describe %s: %s", loc, e)
                continue
            with self.lock:
                if any(x.udn == r.udn for x in self.items.values()):     # mismo equipo, otra IP/URL
                    continue
                self.items[loc] = r
            log.info("Player: %s (%s) %s", r.nombre, r.modelo, loc)

    def bucle_descubrir(self):
        while True:
            try:
                self.descubrir()
            except Exception:
                log.exception("Discovery")
            time.sleep(60 if self.items else 15)

    # ---- sondeo
    def bucle_sondeo(self):
        n = 0
        while True:
            t0 = time.time()
            with self.lock:
                lista = list(self.items.items())
            for loc, r in lista:
                if r.fallos > 20 and n % 15:                 # caído: reintenta cada ~15 s
                    continue
                try:
                    r.sondear(n % 5 == 0)
                    self.seguir_sesion(r)
                except Exception as e:
                    r.fallos += 1
                    if r.fallos == 21:
                        log.info("%s is not responding", r.nombre)
                        self.cerrar_sesion(r)
                    if r.fallos > 600:                       # ~10 min sin responder: se olvida
                        with self.lock:
                            self.items.pop(loc, None)
            n += 1
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    def seguir_sesion(self, r):
        s = r.sesion
        ahora = time.time()
        sonando = r.estado == "PLAYING"
        if s and (r.uri != s["uri"] or r.estado in ("STOPPED", "NO_MEDIA_PRESENT")):
            self.cerrar_sesion(r)
            s = None
        if not s and r.uri and r.estado in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"):
            s = r.sesion = {"uri": r.uri, "inicio": ahora, "ultimo": ahora, "escuchado": 0.0}
        if s:
            if sonando:
                s["escuchado"] += min(ahora - s["ultimo"], 3.0)
            s["ultimo"] = ahora
            s["meta"] = r.meta or s.get("meta", {})
            s["dur"] = r.dur or s.get("dur", 0)

    def cerrar_sesion(self, r):
        s, r.sesion = r.sesion, None
        if not s or s["escuchado"] < MIN_ESCUCHA:
            return
        m = s.get("meta", {})
        info = self.dash.act.info_uri(s["uri"], self.dash.cfg["puerto_servidor"])
        mm = (info or {}).get("meta", {})
        reg = {
            "t": round(s["inicio"], 1), "t_fin": round(s["ultimo"], 1), "reproductor": r.nombre,
            "titulo": m.get("title") or mm.get("title") or s["uri"].rsplit("/", 1)[-1],
            "artista": m.get("artist") or mm.get("artist") or "", "album": m.get("album") or mm.get("album") or "",
            "art": m.get("art", ""), "fuente": "servidor" if info else "otra",
            "rel": (info or {}).get("rel", ""), "raiz": (info or {}).get("raiz", ""),
            "escuchado": round(s["escuchado"]), "dur": round(s.get("dur") or mm.get("dur") or 0),
            "formato": formato_txt((info or {}).get("ext"), mm) if info else "",
        }
        with self.hist_lock:
            self.hist.append(reg)
            try:
                with open(RUTA_HIST, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(reg, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError as e:
                log.warning("Could not write history: %s", e)

    def estado(self):
        out = []
        with self.lock:
            lista = list(self.items.values())
        for r in lista:
            info = self.dash.act.info_uri(r.uri, self.dash.cfg["puerto_servidor"]) if r.uri else None
            mm = (info or {}).get("meta", {})
            res = r.meta.get("res") or {}
            out.append({
                "nombre": r.nombre, "modelo": r.modelo, "ip": r.ip, "online": r.fallos <= 20,
                "estado": r.estado, "pos": r.pos, "dur": r.dur or mm.get("dur", 0), "vol": r.vol, "mute": r.mute,
                "titulo": r.meta.get("title") or mm.get("title") or "", "artista": r.meta.get("artist") or mm.get("artist") or "",
                "album": r.meta.get("album") or mm.get("album") or "", "art": r.meta.get("art", ""),
                "fuente": "servidor" if info else ("otra" if r.uri else ""),
                "rel": (info or {}).get("rel", ""), "raiz": (info or {}).get("raiz", ""),
                "ext": ((info or {}).get("ext") or "").upper(),
                "sr": mm.get("sr") or int(res.get("sampleFrequency") or 0) or None,
                "bits": mm.get("bits") or int(res.get("bitsPerSample") or 0) or None,
                "br": mm.get("br") or (int(res.get("bitrate") or 0) * 8) or None,
                "tam": (info or {}).get("tam"), "genero": mm.get("genre", ""), "anio": mm.get("year"),
                "escuchado": round(r.sesion["escuchado"]) if r.sesion else 0,
                "uri_otra": "" if info else (r.uri[:120] if r.uri else ""),
            })
        orden = {"PLAYING": 0, "PAUSED_PLAYBACK": 1, "TRANSITIONING": 1}
        out.sort(key=lambda x: (not x["online"], orden.get(x["estado"], 2), x["nombre"]))
        return out


# ═══════════════════════════════════════════════════════════════════════════ dashboard

class Dashboard:
    def __init__(self):
        self.cfg = leer_config()
        self.inicio = time.time()
        self.act = Actividad()
        self.rep = Reproductores(self)
        self.servidor = {"online": False}

    def bucle_eventos(self):
        while True:
            try:
                self.act.leer_nuevos()
            except Exception:
                log.exception("Reading events")
            time.sleep(0.5)

    def bucle_servidor(self):
        n = 0
        while True:
            if n % 6 == 0:
                self.cfg = leer_config()
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.cfg["puerto_servidor"], timeout=3)
                c.request("GET", "/api/estado")
                d = json.loads(c.getresponse().read())
                c.close()
                d["online"] = True
                self.servidor = d
            except Exception:
                self.servidor = {"online": False, "nombre": self.cfg["nombre"]}
            n += 1
            time.sleep(5)

    def estadisticas(self):
        ahora = time.time()
        hoy = dia(ahora)
        with self.rep.hist_lock:
            hist = list(self.rep.hist)
        dias = [dia(ahora - 86400 * i) for i in range(13, -1, -1)]
        min_dia = defaultdict(float)
        n_dia = defaultdict(int)
        art, alb, fmt, rep = Counter(), Counter(), Counter(), Counter()
        lim30 = ahora - 30 * 86400
        for h in hist:
            d = dia(h["t"])
            min_dia[d] += h.get("escuchado", 0) / 60
            n_dia[d] += 1
            if h["t"] >= lim30:
                if h.get("artista"):
                    art[h["artista"]] += 1
                if h.get("album"):
                    alb[(h["album"], h.get("artista", ""))] += 1
                fmt[h.get("formato") or (T("d.otra") if h.get("fuente") == "otra" else "?")] += 1
                rep[h.get("reproductor", "?")] += 1
        with self.act.lock:
            bytes_dia = dict(self.act.bytes_dia)
            serv_dia = dict(self.act.servidas_dia)
            nav_dia = dict(self.act.nav_dia)
        return {
            "hoy": {"escuchadas": n_dia.get(hoy, 0), "minutos": round(min_dia.get(hoy, 0)),
                    "bytes": bytes_dia.get(hoy, 0), "servidas": serv_dia.get(hoy, 0), "navegaciones": nav_dia.get(hoy, 0)},
            "total": {"escuchadas": len(hist), "minutos": round(sum(h.get("escuchado", 0) for h in hist) / 60)},
            "dias": [{"dia": d, "minutos": round(min_dia.get(d, 0), 1), "escuchadas": n_dia.get(d, 0),
                      "mb": round(bytes_dia.get(d, 0) / 1048576)} for d in dias],
            "top_artistas": [{"k": k, "n": v} for k, v in art.most_common(8)],
            "top_albumes": [{"k": k[0], "sub": k[1], "n": v} for k, v in alb.most_common(8)],
            "formatos": [{"k": k, "n": v} for k, v in fmt.most_common(8)],
        }

    def estado(self):
        ahora = time.time()
        with self.act.lock:
            activos = [{"t": v["t"], "ip": v["ip"], "rel": v["rel"], "raiz": v.get("raiz"), "tam": v.get("tam"),
                        "ini": v.get("ini", 0), "fin": v.get("fin"), "formato": formato_txt(v.get("ext"), v.get("meta", {})),
                        "titulo": v.get("meta", {}).get("title", "")} for v in self.act.activos.values()
                       if ahora - v["t"] < 3600]
            servidas = [{k: v for k, v in s.items() if k != "clave"} for s in list(self.act.servidas)[-60:]][::-1]
            for s in servidas:
                s["formato"] = formato_txt(s.get("ext"), s.get("meta", {}))
                s["titulo"] = s.get("meta", {}).get("title") or s["rel"].rsplit("/", 1)[-1]
                s["artista"] = s.get("meta", {}).get("artist", "")
                s.pop("meta", None)
            feed = list(self.act.feed)[-80:][::-1]
            clientes = sorted(self.act.clientes.values(), key=lambda c: -c["ultimo"])[:10]
        with self.rep.hist_lock:
            hist = list(self.rep.hist)[-60:][::-1]
        return {
            "ahora": ahora, "nombre": self.cfg["nombre"], "puerto_servidor": self.cfg["puerto_servidor"],
            "dashboard": {"version": VERSION, "uptime": round(ahora - self.inicio)},
            "servidor": self.servidor, "reproductores": self.rep.estado(), "activos": activos,
            "servidas": servidas, "feed": feed, "historial": hist, "clientes": clientes,
            "stats": self.estadisticas(),
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ruta = urlparse(self.path).path
        d = self.server.dash
        if ruta in ("/", "/index.html"):
            return self._send(200, DASH_HTML.replace("__LANG__", i18n.LANG), "text/html; charset=utf-8")
        if ruta == "/api/estado":
            return self._send(200, json.dumps(d.estado(), ensure_ascii=False), "application/json; charset=utf-8")
        if ruta == "/api/ping":
            return self._send(200, str(d.cfg["puerto"]), "text/plain")
        return self._send(404, "", "text/plain")


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not ES_WINDOWS

    def server_bind(self):
        if ES_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ═══════════════════════════════════════════════════════════════════════════ HTML

DASH_HTML = r"""<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard · SupaVic DLNA</title>
<style>
:root{--bg:#f4f2ee;--card:#fff;--tx:#1d1c1a;--tx2:#4a4843;--mut:#6d6a64;--line:#e3dfd8;--acc:#c2410c;--acc2:#fff3ea;--bar:#c2410c;--track:#ece8e1;--ok:#15803d;--bad:#b91c1c;--warn:#a16207}
@media(prefers-color-scheme:dark){:root{--bg:#121110;--card:#1c1b19;--tx:#eceae6;--tx2:#c9c5bd;--mut:#9b978f;--line:#2e2c29;--acc:#fb923c;--acc2:#2a1d14;--bar:#f08a4b;--track:#2a2825;--ok:#4ade80;--bad:#f87171;--warn:#facc15}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:1280px;margin:auto;padding:18px 16px 50px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:19px;margin:0;font-weight:650}
.pill{font-size:12px;padding:4px 10px;border-radius:99px;background:var(--card);border:1px solid var(--line);color:var(--tx2);display:inline-flex;gap:6px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
header .sp{flex:1}
a{color:var(--acc)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:26px 0 10px;font-weight:650}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px}
.np-grid{display:grid;gap:12px}
.np{display:grid;grid-template-columns:200px 1fr;gap:20px;padding:16px}
.np.idle{grid-template-columns:56px 1fr;padding:10px 14px;gap:14px;align-items:center}
.np .art{width:100%;aspect-ratio:1;border-radius:10px;background:var(--track) center/cover no-repeat;display:grid;place-items:center;color:var(--mut);font-size:40px;overflow:hidden}
.np.idle .art{font-size:20px;border-radius:8px}
.np h3{font-size:24px;margin:2px 0 2px;font-weight:700;letter-spacing:-.01em;line-height:1.2}
.np.idle h3{font-size:15px}
.np .sub{color:var(--tx2);font-size:15px}
.np .rmeta{color:var(--mut);font-size:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
.badge{font-size:12px;font-weight:650;padding:3px 9px;border-radius:6px;background:var(--acc2);color:var(--acc);font-variant-numeric:tabular-nums}
.badge.n{background:var(--track);color:var(--tx2)}
.prog{margin-top:10px}
.bar{height:6px;background:var(--track);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--bar);border-radius:99px;width:0}
.times{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;margin-top:4px;font-variant-numeric:tabular-nums}
.path{font:12px ui-monospace,Consolas,monospace;color:var(--mut);word-break:break-all;margin-top:10px}
@media(max-width:640px){.np{grid-template-columns:96px 1fr;gap:14px}.np h3{font-size:18px}}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:12px}
.kpi{padding:14px 16px}.kpi .k{font-size:12px;color:var(--mut)}.kpi .v{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}.kpi .s{font-size:12px;color:var(--mut)}
.cols{display:grid;grid-template-columns:3fr 2fr;gap:16px}@media(max-width:900px){.cols{grid-template-columns:1fr}}
.cols3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}@media(max-width:900px){.cols3{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
th{font-size:11px;color:var(--mut);font-weight:650;text-transform:uppercase;letter-spacing:.04em}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.mut{color:var(--mut)}.small{font-size:12px}
.t1{font-weight:600}.trunc{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scroll{max-height:460px;overflow:auto}
.feed{list-style:none;margin:0;padding:4px 0}
.feed li{display:grid;grid-template-columns:56px 18px 1fr;gap:8px;padding:7px 14px;border-bottom:1px solid var(--line)}
.feed li:last-child{border-bottom:0}
.feed .h{color:var(--mut);font-variant-numeric:tabular-nums;font-size:12px}
.feed .ic{color:var(--mut);text-align:center}
.feed .stream .ic{color:var(--acc)}.feed .corte .ic{color:var(--warn)}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:var(--track);color:var(--tx2);white-space:nowrap}
.tag.w{color:var(--warn)}.tag.ok{color:var(--ok)}
.chart{padding:14px 16px}
.chart h4{margin:0 0 10px;font-size:14px}
.hb{display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:4px 8px;align-items:center}
.hb .l{font-size:12px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hb .l small{color:var(--mut)}
.hb .b{grid-column:1/2;height:8px;background:var(--track);border-radius:4px;margin-bottom:6px;position:relative}
.hb .b i{position:absolute;inset:0 auto 0 0;background:var(--bar);border-radius:4px}
.hb .n{grid-row:span 2;text-align:right;font-size:12px;color:var(--tx2);font-variant-numeric:tabular-nums}
.cb{display:flex;align-items:flex-end;gap:2px;height:120px;padding-top:6px}
.cb div{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;cursor:default}
.cb i{display:block;background:var(--bar);border-radius:4px 4px 0 0;min-height:0}
.cb i.z{background:var(--track);min-height:2px}
.cbx{display:flex;gap:2px;margin-top:4px}.cbx span{flex:1;text-align:center;font-size:10px;color:var(--mut)}
.empty{padding:20px 16px;color:var(--mut)}
.cols>*,.cols3>*,.np>*{min-width:0}
@media(max-width:640px){.hm{display:none}.trunc{max-width:48vw}td,th{padding:8px 7px}}
#tip{position:fixed;pointer-events:none;background:var(--tx);color:var(--bg);font-size:12px;padding:5px 8px;border-radius:6px;display:none;z-index:9;white-space:nowrap}
</style>
</head><body><div class="wrap">
<header>
  <h1 id="titulo">Dashboard</h1>
  <span class="pill" id="p-srv"><span class="dot"></span></span>
  <span class="pill" id="p-pre" style="display:none"></span>
  <span class="pill" id="p-dash"></span>
  <span class="sp"></span>
  <a id="l-panel" class="small" href="#" data-i="panel"></a>
</header>

<div class="np-grid" id="np"></div>

<div class="kpis">
  <div class="card kpi"><div class="k" data-i="k.esc"></div><div class="v" id="k-esc">–</div><div class="s" id="k-esc-s"></div></div>
  <div class="card kpi"><div class="k" data-i="k.min"></div><div class="v" id="k-min">–</div><div class="s" id="k-min-s"></div></div>
  <div class="card kpi"><div class="k" data-i="k.mb"></div><div class="v" id="k-mb">–</div><div class="s" id="k-mb-s"></div></div>
  <div class="card kpi"><div class="k" data-i="k.act"></div><div class="v" id="k-act">–</div><div class="s" id="k-act-s"></div></div>
</div>

<div class="cols">
  <div><h2 data-i="h.hist"></h2><div class="card scroll"><table><thead><tr><th data-i="th.hora"></th><th data-i="th.pista"></th><th class="hm" data-i="th.formato"></th><th class="num" data-i="th.escuchado"></th></tr></thead><tbody id="t-hist"></tbody></table></div></div>
  <div><h2 data-i="h.feed"></h2><div class="card scroll"><ul class="feed" id="feed"></ul></div></div>
</div>

<h2 data-i="h.serv"></h2>
<div class="card scroll"><table><thead><tr><th data-i="th.hora"></th><th data-i="th.archivo"></th><th class="hm" data-i="th.cliente"></th><th class="hm" data-i="th.formato"></th><th class="num" data-i="th.enviado"></th><th class="num hm" data-i="th.vel"></th><th data-i="th.estado"></th></tr></thead><tbody id="t-serv"></tbody></table></div>

<h2 data-i="h.stats"></h2>
<div class="card chart"><h4><span data-i="c.dias"></span> <span class="mut small" data-i="c.dias14"></span></h4><div class="cb" id="c-dias"></div><div class="cbx" id="c-dias-x"></div></div>
<div class="cols3" style="margin-top:16px">
  <div class="card chart"><h4><span data-i="c.art"></span> <span class="mut small" data-i="c.30"></span></h4><div class="hb" id="c-art"></div></div>
  <div class="card chart"><h4><span data-i="c.alb"></span> <span class="mut small" data-i="c.30"></span></h4><div class="hb" id="c-alb"></div></div>
  <div class="card chart"><h4><span data-i="c.fmt"></span> <span class="mut small" data-i="c.30"></span></h4><div class="hb" id="c-fmt"></div></div>
</div>
</div>
<div id="tip"></div>
<script>
const LANG="__LANG__";
const TXT={
es:{"panel":"Panel de configuración ↗","k.esc":"Escuchadas hoy","k.min":"Tiempo escuchado hoy","k.mb":"Servido hoy","k.act":"Transferencias activas",
"h.hist":"Historial de escucha","h.feed":"Actividad en vivo","h.serv":"Transferencias del servidor","h.stats":"Estadísticas",
"th.hora":"Hora","th.pista":"Pista","th.formato":"Formato","th.escuchado":"Escuchado","th.archivo":"Archivo","th.cliente":"Cliente","th.enviado":"Enviado","th.vel":"Velocidad","th.estado":"Estado",
"c.dias":"Minutos escuchados por día","c.dias14":"· últimos 14 días","c.art":"Artistas","c.alb":"Álbumes","c.fmt":"Formatos escuchados","c.30":"· 30 días",
"st.PLAYING":"Reproduciendo","st.PAUSED_PLAYBACK":"En pausa","st.STOPPED":"Detenido","st.TRANSITIONING":"Cargando…","st.NO_MEDIA_PRESENT":"Sin pista",
"original":"{n} · original, sin transcodificar","otra":"Otra fuente","buscando":"Buscando reproductores en la red… (tu reproductor debe estar encendido)",
"sinresp":"sin respuesta","silencio":" (silencio)","srv.on":"Servidor en línea · {up} · {n} pistas en caché","srv.off":"Servidor sin respuesta",
"pre.escaneando":"Precarga {pct}% · {p} pistas · {c} portadas","pre.pausada":"Precarga en pausa ({pct}%)","pre.completa":"Caché completa","pre.esperando":"Precarga en espera","pre.desactivada":"Precarga desactivada",
"entotal":"{x} en total","servidas":"{a} pistas · {b} navegaciones","ninguna":"ninguna",
"hist.vacio":"Aún no hay reproducciones registradas. Se cuentan las pistas escuchadas más de 10 s.","feed.vacio":"Sin actividad todavía.",
"serv.vacio":"El servidor aún no ha transferido pistas.","pet":"pet.","otrafuente":"otra fuente","sindatos":"Sin datos todavía",
"tip.dia":"{d}: {m} min · {n} pistas · {mb} MB servidos","sinconexion":"Dashboard sin conexión","e.completo":"completo","e.cortado":"cortado","e.error":"error"},
en:{"panel":"Control panel ↗","k.esc":"Played today","k.min":"Listening time today","k.mb":"Served today","k.act":"Active transfers",
"h.hist":"Listening history","h.feed":"Live activity","h.serv":"Server transfers","h.stats":"Statistics",
"th.hora":"Time","th.pista":"Track","th.formato":"Format","th.escuchado":"Played","th.archivo":"File","th.cliente":"Client","th.enviado":"Sent","th.vel":"Speed","th.estado":"Status",
"c.dias":"Minutes listened per day","c.dias14":"· last 14 days","c.art":"Artists","c.alb":"Albums","c.fmt":"Formats played","c.30":"· 30 days",
"st.PLAYING":"Playing","st.PAUSED_PLAYBACK":"Paused","st.STOPPED":"Stopped","st.TRANSITIONING":"Loading…","st.NO_MEDIA_PRESENT":"No track",
"original":"{n} · original file, not transcoded","otra":"Other source","buscando":"Looking for players on the network… (your player must be on)",
"sinresp":"not responding","silencio":" (muted)","srv.on":"Server online · {up} · {n} tracks cached","srv.off":"Server not responding",
"pre.escaneando":"Prefetch {pct}% · {p} tracks · {c} covers","pre.pausada":"Prefetch paused ({pct}%)","pre.completa":"Cache complete","pre.esperando":"Prefetch waiting","pre.desactivada":"Prefetch disabled",
"entotal":"{x} in total","servidas":"{a} tracks · {b} browses","ninguna":"none",
"hist.vacio":"No plays recorded yet. Tracks played for more than 10 s are counted.","feed.vacio":"No activity yet.",
"serv.vacio":"The server hasn't transferred any tracks yet.","pet":"req.","otrafuente":"other source","sindatos":"No data yet",
"tip.dia":"{d}: {m} min · {n} tracks · {mb} MB served","sinconexion":"Dashboard offline","e.completo":"complete","e.cortado":"cut off","e.error":"error"}}[LANG]||{};
const t=(k,o)=>{let s=TXT[k]??k;if(o)for(const x in o)s=s.split("{"+x+"}").join(o[x]);return s};
const LOC=(()=>{const d=LANG==="es"?"es-MX":"en-US",l=navigator.language||"";try{if(l.toLowerCase().startsWith(LANG)){new Intl.NumberFormat(l);return l}}catch(e){}return d})();
const nf=n=>Number(n||0).toLocaleString(LOC);
document.querySelectorAll("[data-i]").forEach(e=>e.innerHTML=t(e.dataset.i));
const $=s=>document.querySelector(s);
const h=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mmss=s=>{s=Math.max(0,Math.round(s||0));const hh=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (hh?hh+":"+String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0")};
const hora=t=>new Date(t*1000).toLocaleTimeString(LOC,{hour:"2-digit",minute:"2-digit"});
const horaDia=t=>{const d=new Date(t*1000),hoy=new Date();return d.toDateString()===hoy.toDateString()?hora(t):d.toLocaleDateString(LOC,{day:"numeric",month:"short"})+" "+hora(t)};
const mb=b=>b>=1073741824?(b/1073741824).toFixed(2)+" GB":(b/1048576).toFixed(b>=104857600?0:1)+" MB";
const durTxt=s=>{s=Math.round(s||0);const d=Math.floor(s/86400),hh=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${hh}h`:hh?`${hh}h ${m}m`:`${m}m`};
let ultimo=null, recibido=0;

function fmtBadges(r,nombre){
  const b=[];
  if(r.ext)b.push(r.ext);
  if(r.bits&&r.sr)b.push(`${r.bits}-bit / ${(r.sr/1000).toLocaleString(LOC)} kHz`);else if(r.sr)b.push(`${(r.sr/1000).toLocaleString(LOC)} kHz`);
  if(r.br)b.push(`${Math.round(r.br/1000).toLocaleString(LOC)} kbps`);
  return b.map(x=>`<span class="badge">${h(x)}</span>`).join("")+
    (r.tam?`<span class="badge n">${mb(r.tam)}</span>`:"")+
    (r.fuente==="servidor"?`<span class="badge n">${h(t("original",{n:nombre}))}</span>`:r.fuente==="otra"?`<span class="badge n">${t("otra")}</span>`:"");
}
function pintarNP(d,nombre){
  const reps=d.reproductores;
  if(!reps.length){$("#np").innerHTML=`<div class="card empty">${t("buscando")}</div>`;return}
  $("#np").innerHTML=reps.map((r,i)=>{
    const activo=r.online&&["PLAYING","PAUSED_PLAYBACK","TRANSITIONING"].includes(r.estado)&&(r.titulo||r.rel);
    const art=r.art?`style="background-image:url('${h(r.art)}')"`:"";
    const meta=`<div class="rmeta"><span><span class="dot ${r.online?"ok":"bad"}" style="display:inline-block;margin-right:5px"></span>${h(r.nombre)}</span>${r.modelo?`<span>${h(r.modelo)}</span>`:""}<span>${r.online?h(t("st."+r.estado)):t("sinresp")}</span>${r.vol!=null?`<span>Vol ${r.vol}${r.mute?t("silencio"):""}</span>`:""}</div>`;
    if(!activo)return `<div class="card np idle"><div class="art">♪</div><div>${meta}</div></div>`;
    return `<div class="card np" data-n="${i}" data-k="${h(r.nombre)}"><div class="art" ${art}>${r.art?"":"♪"}</div><div style="min-width:0">${meta}
      <h3>${h(r.titulo||r.rel.split("/").pop())}</h3><div class="sub">${h(r.artista)}${r.album?" · "+h(r.album):""}${r.anio?" · "+r.anio:""}</div>
      <div class="badges">${fmtBadges(r,nombre)}</div>
      <div class="prog"><div class="bar"><i id="pb${i}"></i></div><div class="times"><span id="pt${i}">${mmss(r.pos)}</span><span>${r.dur?mmss(r.dur):"–"}</span></div></div>
      ${r.rel?`<div class="path">${h(r.raiz)}/${h(r.rel)}</div>`:r.uri_otra?`<div class="path">${h(r.uri_otra)}</div>`:""}
    </div></div>`}).join("");
}
function tick(){
  requestAnimationFrame(tick);
  if(!ultimo)return;
  const dt=(Date.now()-recibido)/1000;
  ultimo.reproductores.forEach((r,i)=>{
    const pb=document.getElementById("pb"+i),pt=document.getElementById("pt"+i);if(!pb)return;
    const pos=r.estado==="PLAYING"?Math.min(r.pos+dt,r.dur||1e9):r.pos;
    pb.style.width=r.dur?(100*pos/r.dur).toFixed(2)+"%":"0";pt.textContent=mmss(pos);
  });
}
const icon={stream:"▶",nav:"📁",desc:"⌕",inicio:"⏻",config:"⚙",corte:"!",precarga:"⟳"};
function pintar(d){
  const s=d.servidor,nombre=s.nombre||d.nombre;
  const sonando=d.reproductores.find(r=>r.estado==="PLAYING");
  document.title=`${sonando&&sonando.titulo?"▶ "+sonando.titulo+" · ":""}${nombre} · Dashboard`;
  $("#titulo").textContent=`Dashboard · ${nombre}`;
  $("#p-srv").innerHTML=`<span class="dot ${s.online?"ok":"bad"}"></span>${s.online?h(t("srv.on",{up:durTxt(s.uptime),n:nf(s.pistas_cache)})):t("srv.off")}`;
  $("#p-dash").textContent=`Dashboard ${d.dashboard.version} · ${durTxt(d.dashboard.uptime)}`;
  const pr=s.online&&s.precarga;
  if(pr){const pct=pr.total_carpetas?Math.round(100*pr.carpetas/pr.total_carpetas):0;
    $("#p-pre").style.display="";
    $("#p-pre").innerHTML=`<span class="dot ${pr.estado==="escaneando"||pr.estado==="completa"?"ok":""}"></span>`+h(t("pre."+pr.estado,{pct,p:nf(pr.pistas),c:nf(pr.portadas)}))}
  else $("#p-pre").style.display="none";
  $("#l-panel").href=`${location.protocol}//${location.hostname}:${d.puerto_servidor}/`;
  pintarNP(d,nombre);
  const st=d.stats;
  $("#k-esc").textContent=nf(st.hoy.escuchadas);$("#k-esc-s").textContent=t("entotal",{x:nf(st.total.escuchadas)});
  $("#k-min").textContent=durTxt(st.hoy.minutos*60);$("#k-min-s").textContent=t("entotal",{x:durTxt(st.total.minutos*60)});
  $("#k-mb").textContent=mb(st.hoy.bytes);$("#k-mb-s").textContent=t("servidas",{a:nf(st.hoy.servidas),b:nf(st.hoy.navegaciones)});
  $("#k-act").textContent=d.activos.length;$("#k-act-s").textContent=d.activos.length?d.activos.map(a=>a.titulo||a.rel.split("/").pop()).join(", ").slice(0,60):t("ninguna");
  $("#t-hist").innerHTML=d.historial.length?d.historial.map(x=>`<tr><td class="mut">${horaDia(x.t)}</td><td><div class="t1 trunc">${h(x.titulo)}</div><div class="mut small trunc">${h(x.artista)}${x.album?" · "+h(x.album):""}</div></td><td class="hm">${x.formato?`<span class="tag">${h(x.formato)}</span>`:x.fuente==="otra"?`<span class="tag">${t("otrafuente")}</span>`:""}</td><td class="num">${mmss(x.escuchado)}${x.dur?`<span class="mut"> / ${mmss(x.dur)}</span>`:""}</td></tr>`).join(""):`<tr><td colspan="4" class="empty">${t("hist.vacio")}</td></tr>`;
  $("#feed").innerHTML=d.feed.length?d.feed.map(f=>`<li class="${f.tipo}"><span class="h">${hora(f.t)}</span><span class="ic">${icon[f.tipo]||"·"}</span><span><span class="trunc" style="display:block;max-width:100%">${h(f.txt)}</span><span class="mut small">${h(f.det)}${f.ip?" · "+h(f.ip):""}</span></span></li>`).join(""):`<li class="empty">${t("feed.vacio")}</li>`;
  $("#t-serv").innerHTML=d.servidas.length?d.servidas.map(x=>{const vel=x.seg>0.05?x.bytes/x.seg:0;const comp=x.tam?Math.min(100,100*x.bytes/x.tam):0;
    return `<tr><td class="mut">${horaDia(x.t)}</td><td><div class="t1 trunc">${h(x.titulo)}</div><div class="mut small trunc" title="${h(x.raiz+"/"+x.rel)}">${h(x.artista||x.rel)}</div></td><td class="mut hm">${h(x.ip)}</td><td class="hm"><span class="tag">${h(x.formato)}</span></td><td class="num">${mb(x.bytes)}<div class="mut small">${comp.toFixed(0)}% · ${x.peticiones} ${t("pet")}</div></td><td class="num hm">${vel?mb(vel)+"/s":"–"}</td><td><span class="tag ${x.estado==="completo"?"ok":"w"}">${h(t("e."+x.estado))}</span></td></tr>`}).join(""):`<tr><td colspan="7" class="empty">${t("serv.vacio")}</td></tr>`;
  const dias=st.dias,maxD=Math.max(1,...dias.map(x=>x.minutos));
  $("#c-dias").innerHTML=dias.map(x=>`<div data-tip="${h(t("tip.dia",{d:new Date(x.dia+"T12:00").toLocaleDateString(LOC,{weekday:"short",day:"numeric",month:"short"}),m:x.minutos,n:x.escuchadas,mb:x.mb}))}"><i class="${x.minutos?"":"z"}" style="height:${x.minutos?Math.max(3,100*x.minutos/maxD):0}%"></i></div>`).join("");
  $("#c-dias-x").innerHTML=dias.map((x,i)=>`<span>${i%2?"":new Date(x.dia+"T12:00").getDate()}</span>`).join("");
  const hb=(id,arr,sub)=>{const m=Math.max(1,...arr.map(x=>x.n));$(id).innerHTML=arr.length?arr.map(x=>`<div class="l" data-tip="${h(x.k)}${x.sub?" — "+h(x.sub):""}: ${x.n}">${h(x.k)}${sub&&x.sub?` <small>${h(x.sub)}</small>`:""}</div><div class="n">${x.n}</div><div class="b" data-tip="${h(x.k)}: ${x.n}"><i style="width:${100*x.n/m}%"></i></div>`).join(""):`<div class="mut small">${t("sindatos")}</div>`};
  hb("#c-art",st.top_artistas);hb("#c-alb",st.top_albumes,true);hb("#c-fmt",st.formatos);
}
async function cargar(){
  try{const r=await fetch("/api/estado");const d=await r.json();ultimo=d;recibido=Date.now();pintar(d)}
  catch(e){$("#p-srv").innerHTML=`<span class="dot bad"></span>${t("sinconexion")}`}
}
const tip=$("#tip");
document.addEventListener("mousemove",e=>{const el=e.target.closest("[data-tip]");if(!el){tip.style.display="none";return}
  tip.textContent=el.dataset.tip;tip.style.display="block";const w=tip.offsetWidth;tip.style.left=Math.min(innerWidth-w-8,e.clientX+12)+"px";tip.style.top=(e.clientY+14)+"px"});
cargar();setInterval(cargar,1500);requestAnimationFrame(tick);
</script></body></html>"""


# ═══════════════════════════════════════════════════════════════════════════ main

def ping():
    try:
        puerto = leer_config()["puerto"]
        c = http.client.HTTPConnection("127.0.0.1", puerto, timeout=2)
        c.request("GET", "/api/ping")
        if c.getresponse().status == 200:
            print(puerto)
            return 0
    except Exception:
        pass
    return 1


def main():
    if "--ping" in sys.argv:
        sys.exit(ping())
    DIR_DATOS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if ("-v" in sys.argv) else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(RUTA_LOG, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    i18n.fijar(leer_config()["idioma"])
    dash = Dashboard()
    httpd = None
    for intento in range(40):
        try:
            httpd = Servidor(("0.0.0.0", dash.cfg["puerto"]), Handler)
            break
        except OSError as e:
            if intento == 39:
                log.error("Could not open port %d: %s", dash.cfg["puerto"], e)
                sys.exit(1)
            time.sleep(0.5)
    httpd.dash = dash
    (DIR_DATOS / "dashboard.pid").write_text(str(os.getpid()), encoding="ascii")
    for fn in (dash.bucle_eventos, dash.bucle_servidor, dash.rep.bucle_descubrir, dash.rep.bucle_sondeo):
        threading.Thread(target=fn, daemon=True).start()
    log.info("─── %s dashboard %s at http://%s:%d/ ───", APP, VERSION, (ips_locales() or ["127.0.0.1"])[0], dash.cfg["puerto"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
