#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SupaVic DLNA — servidor DLNA/UPnP (MediaServer) solo para música.

- Navegación por carpetas en vivo: NO hay indexado previo obligatorio. Tags y portadas se leen
  al abrir una carpeta desde el control (WiiM Home, BubbleUPnP, etc.) y se guardan en caché;
  en reposo se precarga el resto. Además: vistas por Artistas, Álbumes, Géneros, Décadas…
- Configuración en config.json, editable desde el panel web: http://<ip>:<puerto>/
- Dependencias: mutagen (tags/portadas) y Pillow (portadas). Opcional: pystray (icono de bandeja, launcher.py).
"""
import base64
import fnmatch
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import logging
import logging.handlers
import os
import random
import re
import socket
import sqlite3
import string
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, OrderedDict, defaultdict
from functools import lru_cache
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _esc, unescape as html_unescape

import mutagen
import i18n
from i18n import T
from mutagen.flac import Picture

try:
    from PIL import Image
except ImportError:          # funciona sin Pillow, pero manda las portadas sin redimensionar
    Image = None

VERSION = "2.0.0"
APP = "SupaVic DLNA"


def _dir_app():
    """Carpeta de config.json y datos/: junto al código al correr desde el código fuente;
    en %LOCALAPPDATA%\\SupaVic DLNA con el ejecutable (Program Files no se puede escribir).
    SUPAVIC_HOME la fija a mano."""
    if os.environ.get("SUPAVIC_HOME"):
        return Path(os.environ["SUPAVIC_HOME"]).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / APP if base else Path.home() / ".supavic-dlna"
    return Path(__file__).resolve().parent


DIR_BASE = _dir_app()
DIR_DATOS = DIR_BASE / "datos"
RUTA_CONFIG = DIR_BASE / "config.json"
RUTA_LOG = DIR_DATOS / "servidor.log"
RUTA_PID = DIR_DATOS / "servidor.pid"
RUTA_EVENTOS = DIR_DATOS / "eventos.jsonl"      # lo lee dashboard.py (proceso independiente)
ES_WINDOWS = os.name == "nt"

SERVER_HDR = f"{'Windows' if ES_WINDOWS else sys.platform}/10 UPnP/1.0 DLNADOC/1.50 SupaVicDLNA/{VERSION}"
RELANZAR = None                 # comando para reiniciar el proceso; lo fija launcher.py (ejecutable / bandeja)
AL_SALIR = None                 # callback antes de reiniciar (launcher.py quita el icono de la bandeja)
log = logging.getLogger("dlna")

CONFIG_DEFECTO = {
    "servidor": {
        "nombre": "",                 # vacío = "SupaVic (<equipo>)"
        "puerto": 8200,
        "ip": "",
        "idioma": "auto",             # auto | es | en (WiiM, panel y dashboard)
        "abrir_navegador": True,      # abrir el panel al iniciar a mano (no al iniciar con Windows)
        "buscar_actualizaciones": True,  # el panel (el navegador, no el servidor) consulta GitHub por versiones nuevas
    },
    "musica": {
        "carpetas": [],
        "sin_indexar": [],             # carpetas (de "carpetas") que se navegan pero no entran en las vistas por tags
        "ignorar": ["*.part", "*.tmp", ".*", "$RECYCLE.BIN", "System Volume Information"],
        "ordenar_por_tags_hasta": 150,
        "agrupar_carpetas": False,     # True: las carpetas van dentro de una entrada "Carpetas" en la raíz
    },
    "portadas": {
        "tamano": 600,
        "recortar_cuadrado": True,
        "por_pista": True,             # cada pista muestra su portada embebida (si no, la del álbum/carpeta)
        "subcarpetas": True,           # carpeta sin imagen propia: usar la primera que haya en sus subcarpetas
        "artista": True,               # artistas: artist.jpg › foto de artista embebida › portada de su primer álbum
    },
    "precarga": {
        # Recorre la biblioteca y llena la caché (tags + portadas) cuando no hay navegación.
        "activa": True,
        "inactividad_min": 5,
        "portadas": True,
        "repetir_horas": 12,
    },
    "vistas": {
        # Navegación por etiquetas (estilo MinimServer/MusicBee) además de las carpetas.
        "activas": ["artistas", "albumes", "generos", "decadas", "recientes", "tocadas", "hires", "aleatorio", "playlists",
                    "favoritas"],
        "conocidas": None,           # vistas que ya existían al guardar: las nuevas de una versión se activan una vez
        "agrupar_letras": False,       # False = listas completas; True = las largas se dividen en A, B, C…
        "agrupar_desde": 200,          # con agrupar_letras: a partir de cuántos elementos
        "tipo_conteo": "pistas",       # {n}: pistas = canciones; elementos = álbumes de un artista, artistas de un género…
        # Cómo se ven los títulos. Campos: {nombre} {n} {pistas} {albumes} · {album} {año} {artista} {calidad}
        # {formato} {genero} y cualquier tag ({label}, {composer}…). Los () y [] que queden vacíos se quitan.
        "plantillas": {"raiz": "{nombre}", "contenedor": "{nombre} ({n})", "album": "{album} [{año}]",
                       "pista": "{titulo}"},  # pistas: {titulo} {numero} {disco} {artista} {album} {año} y cualquier tag
        "album_en_pistas": False,
        "discos": False,             # álbumes de varios discos: una carpeta ">> subtítulo" antes de cada disco
        "numeracion_seguida": False, # álbumes de varios discos: pistas 1…N seguidas (no 1… por disco)
        "carpeta_en_iguales": True,    # dos álbumes que se verían con el mismo título: " · <carpeta>" al segundo      # las pistas muestran el álbum con la plantilla "album" (BubbleUPnP, upplay)
        # Nombre de artista que se MUESTRA (el crédito tal como está impreso, p. ej. Picard "DISPLAY ARTIST"):
        # el primero de estos tags que tenga la pista; si ninguno, ARTIST. Los índices siguen usando ARTIST(S).
        "tags_artista": "display artist, artist display, artist label",
        "tags_artista_album": "display albumartist, albumartist display, albumartist label",
        # Vistas propias: {"id", "nombre", "niveles": ["composer", …], "filtro": [{"tag", "op", "valor"}],
        #                  "plantilla_album": ""}. Las que estén en "activas" aparecen en la raíz.
        "personalizadas": [],
    },
    "dashboard": {
        # Lo usa dashboard.py; aquí solo para que el panel no borre estas claves al guardar.
        "puerto": 8202,
        "renderers": [],          # URLs de description.xml extra si el descubrimiento no encuentra el WiiM
        "con_servidor": True,     # la bandeja lo inicia y lo detiene junto con el servidor
    },
    "cache": {
        # Tags (SQLite) y portadas. Vacío = datos/. En un SSD/NVMe la navegación en frío es más rápida.
        "directorio": "",
    },
}

AUDIO = {
    ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".aif": "audio/aiff",
    ".aiff": "audio/aiff", ".dsf": "audio/dsf", ".dff": "audio/dff",
    ".wv": "audio/x-wavpack", ".ape": "audio/x-ape",
}
IMAGENES = {".jpg", ".jpeg", ".png", ".webp"}
PORTADA_PREF = ["cover", "folder", "front", "album", "albumart", "portada", "frontal"]
# imágenes que casi nunca son la portada: solo se usan si no hay otra cosa (ni portada embebida)
NO_PORTADA = ("back", "contra", "inlay", "tray", "cd", "disc", "disco", "booklet", "inside", "interior", "artist",
              "artista", "logo", "scan", "matrix", "obi")
ARTISTA_IMG = ("artist", "artista", "band", "banda")
DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
NS_CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
NS_CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
DEV_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x2, 0x4


def esc(s):
    return _esc(str(s), {'"': "&quot;"})


def protocol_info(mime):
    pn = "DLNA.ORG_PN=MP3;" if mime == "audio/mpeg" else ""
    return f"http-get:*:{mime}:{pn}{DLNA_FLAGS}"


@lru_cache(maxsize=1 << 16)
def natkey(s):
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s.casefold()))


def _int(s):
    m = re.match(r"\s*(\d+)", str(s or ""))
    return int(m.group(1)) if m else 0


# ═══════════════════════════════════════════════════════════════════════════ configuración

def _merge(defecto, actual):
    out = {}
    for k, v in defecto.items():
        if isinstance(v, dict):
            out[k] = _merge(v, actual.get(k, {}) if isinstance(actual.get(k), dict) else {})
        else:
            out[k] = actual.get(k, v)
    return out


def obtener_udn():
    """ID único del servidor (UDN), guardado en datos/udn.txt para que no cambie nunca.
    Instalaciones existentes: el mismo que se calculaba antes (los reproductores no ven un servidor nuevo).
    Docker nuevo/sin guardar: aleatorio. Antes salía del hostname + /data, iguales en todos los contenedores,
    y dos instancias en la misma red se veían como un solo servidor."""
    ruta = DIR_DATOS / "udn.txt"
    try:
        u = ruta.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f-]{36}", u):
            return u
    except OSError:
        pass
    en_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")    # Docker / Podman
    u = str(uuid.uuid4() if en_docker else
            uuid.uuid5(uuid.NAMESPACE_DNS, f"servidorMusicaDLNA:{socket.gethostname()}:{DIR_BASE}"))
    try:
        DIR_DATOS.mkdir(parents=True, exist_ok=True)
        ruta.write_text(u, encoding="ascii")
    except OSError as e:
        log.warning("Could not save the server id (%s): %s", ruta, e)
    return u


ENV_CONFIG = {   # variable -> (sección, clave, conversión); se aplican al arrancar cuando su valor cambia
    "SUPAVIC_MUSICA": ("musica", "carpetas", lambda v: [c.strip() for c in re.split(r"[;,]", v) if c.strip()]),
    "SUPAVIC_NOMBRE": ("servidor", "nombre", lambda v: v.strip()[:64]),
    "SUPAVIC_IDIOMA": ("servidor", "idioma", lambda v: v.strip().lower()),
    "SUPAVIC_PUERTO": ("servidor", "puerto", lambda v: int(v) if v.strip().isdigit() else None),
}


def cargar_config():
    actual = {}
    if RUTA_CONFIG.exists():
        try:
            actual = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            log.error("Invalid config.json (%s); using defaults", e)
    # Variables de entorno (Docker, servicio): se aplican la primera vez y cada vez que cambian en el compose;
    # mientras no cambien, lo que se edite en el panel se respeta.
    ruta_env = DIR_DATOS / "entorno.json"
    try:
        visto = json.loads(ruta_env.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        visto = {}
    nuevo_visto, cambio_env = dict(visto), False
    for var, (sec, clave, conv) in ENV_CONFIG.items():
        v = os.environ.get(var)
        if not v or not v.strip():
            continue
        if RUTA_CONFIG.exists() and visto.get(var) == v:
            continue
        if RUTA_CONFIG.exists() and var not in visto and var == "SUPAVIC_MUSICA":
            # actualización desde una versión sin entorno.json: la imagen siempre trae SUPAVIC_MUSICA=/music, así
            # que no es una elección del usuario; aplicarla borraría las carpetas que agregó en el panel
            nuevo_visto[var], cambio_env = v, True
            continue
        val = conv(v)
        if val in (None, "", []):
            continue
        actual.setdefault(sec, {})[clave] = val
        nuevo_visto[var], cambio_env = v, True
        if RUTA_CONFIG.exists():
            log.info("%s changed: %s.%s = %r", var, sec, clave, val)
    cfg = _merge(CONFIG_DEFECTO, actual)
    aplicar_idioma(cfg)
    if not str(cfg["servidor"]["nombre"]).strip():
        cfg["servidor"]["nombre"] = T("nombre_defecto", host=socket.gethostname())[:64]
    # la misma limpieza que al guardar en el panel (config editado a mano, migraciones de formatos viejos…);
    # si algo no pasa (p. ej. una carpeta en una unidad desconectada), se usa tal cual y se avisa en el log
    try:
        limpia, errores, _ = validar_config(cfg)
        if errores:
            log.warning("config.json: %s", "; ".join(map(str, errores)))
        else:
            cfg = limpia
    except Exception:
        log.exception("Validating config.json")
    if cfg != actual or cambio_env:                    # lo que vino del entorno queda escrito antes de marcarlo como visto
        guardar_config(cfg)
    if cambio_env:
        try:
            DIR_DATOS.mkdir(parents=True, exist_ok=True)
            ruta_env.write_text(json.dumps(nuevo_visto, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return cfg


def aplicar_idioma(cfg):
    i18n.fijar(cfg["servidor"].get("idioma"))
    VISTAS.clear()
    VISTAS.update((k, T("vista." + k)) for k in CLAVES_VISTAS)


def guardar_config(cfg):
    RUTA_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUTA_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, RUTA_CONFIG)


def validar_config(nueva):
    """Devuelve (config_limpia, errores, avisos)."""
    errores, avisos = [], []
    cfg = _merge(CONFIG_DEFECTO, nueva if isinstance(nueva, dict) else {})
    s, m, p = cfg["servidor"], cfg["musica"], cfg["portadas"]
    s["nombre"] = str(s["nombre"]).strip()[:64]
    if not s["nombre"]:
        errores.append(T("e.nombre"))
    try:
        s["puerto"] = int(s["puerto"])
        if not 1024 <= s["puerto"] <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errores.append(T("e.puerto"))
    s["idioma"] = str(s["idioma"] or "auto").lower()
    if s["idioma"] not in ("auto",) + i18n.IDIOMAS:
        s["idioma"] = "auto"
    s["ip"] = str(s["ip"] or "").strip()
    if s["ip"]:
        try:
            ipaddress.IPv4Address(s["ip"])
        except ValueError:
            errores.append(T("e.ip"))
    carpetas = []
    for c in m["carpetas"] if isinstance(m["carpetas"], list) else []:
        c = str(c).strip().strip('"')
        if not c:
            continue
        c = os.path.normpath(c)
        if ES_WINDOWS and re.fullmatch(r"[A-Za-z]:", c):
            c += "\\"
        if c in carpetas:
            continue
        if not os.path.isdir(c):
            avisos.append(T("e.no_existe", ruta=c))
        carpetas.append(c)
    m["carpetas"] = carpetas
    sin_idx = []
    for c in m.get("sin_indexar") if isinstance(m.get("sin_indexar"), list) else []:
        c = os.path.normpath(str(c).strip().strip('"')) if str(c).strip() else ""
        if ES_WINDOWS and re.fullmatch(r"[A-Za-z]:", c):
            c += "\\"
        if c in carpetas and c not in sin_idx:          # solo carpetas que siguen en la lista
            sin_idx.append(c)
    m["sin_indexar"] = sin_idx
    m["ignorar"] = [str(x).strip() for x in (m["ignorar"] if isinstance(m["ignorar"], list) else []) if str(x).strip()]
    try:
        m["ordenar_por_tags_hasta"] = max(0, int(m["ordenar_por_tags_hasta"]))
    except (TypeError, ValueError):
        errores.append(T("e.orden"))
    try:
        p["tamano"] = int(p["tamano"])
        if p["tamano"] != 0 and not 100 <= p["tamano"] <= 2000:
            raise ValueError
    except (TypeError, ValueError):
        errores.append(T("e.portada"))
    for k in ("recortar_cuadrado", "por_pista", "subcarpetas", "artista"):
        p[k] = bool(p[k])
    s["abrir_navegador"] = bool(s["abrir_navegador"])
    s["buscar_actualizaciones"] = bool(s["buscar_actualizaciones"])
    pc = cfg["precarga"]
    pc["activa"], pc["portadas"] = bool(pc["activa"]), bool(pc["portadas"])
    try:
        pc["inactividad_min"] = round(float(pc["inactividad_min"]), 2)
        pc["repetir_horas"] = round(float(pc["repetir_horas"]), 2)
        if pc["inactividad_min"] < 0.1 or pc["repetir_horas"] < 0:
            raise ValueError
    except (TypeError, ValueError):
        errores.append(T("e.precarga"))
    v = cfg["vistas"]
    crudo = (nueva.get("vistas") if isinstance(nueva, dict) else None) or {}
    if not isinstance(crudo.get("plantillas"), dict) and any(k in crudo for k in ("mostrar_conteo", "mostrar_anio")):
        # config de la 1.1 o anterior (casillas de conteo y año): respetar cómo se veía
        conteo, anio = crudo.get("mostrar_conteo", True) is not False, crudo.get("mostrar_anio", True) is not False
        if crudo.get("tipo_conteo") == "no":
            conteo = False
        v["plantillas"] = {"raiz": "{nombre}", "contenedor": "{nombre} ({n})" if conteo else "{nombre}",
                           "album": ("{album} [{año}]" if anio else "{album}")
                           + (" ({pistas})" if conteo and crudo.get("tipo_conteo", "pistas") == "pistas" else "")}
    pl = v["plantillas"]
    for viejo, nuevo in (("{album} [{años}] • {version}", "{album} [{años}]< • {version}>"),   # predefinido de la 1.3:
                         ("{album} [{years}] • {version}", "{album} [{years}]< • {version}>")):  # dejaba un "•" suelto
        if pl.get("album") == viejo:
            pl["album"] = nuevo
    for k in ("raiz", "contenedor", "album", "pista"):
        t = str(pl.get(k) or "").strip()[:120]
        pl[k] = t or CONFIG_DEFECTO["vistas"]["plantillas"][k]
    if v["tipo_conteo"] not in ("pistas", "elementos"):
        v["tipo_conteo"] = "pistas"
    v["album_en_pistas"] = bool(v.get("album_en_pistas"))
    v["discos"] = bool(v.get("discos"))
    v["numeracion_seguida"] = bool(v.get("numeracion_seguida"))
    v["carpeta_en_iguales"] = bool(v.get("carpeta_en_iguales", True))
    for k in ("tags_artista", "tags_artista_album"):
        v[k] = ", ".join(lista_tags(v.get(k)))[:200]
    v["personalizadas"] = validar_personalizadas(v["personalizadas"])
    ids_x = {x["id"] for x in v["personalizadas"]}
    vistas = [x for x in (v["activas"] if isinstance(v["activas"], list) else [])
              if isinstance(x, str) and (x in VISTAS or x in ids_x)]
    # vistas nuevas de esta versión: aparecen una vez para quien actualiza (si luego las quita, no vuelven)
    conocidas = v.get("conocidas")
    if not isinstance(conocidas, list):
        conocidas = [k for k in CLAVES_VISTAS if k not in VISTAS_NUEVAS]
    for k in VISTAS_NUEVAS:
        if k not in conocidas:
            conocidas.append(k)
            if k not in vistas:
                vistas.append(k)
    v["conocidas"] = [k for k in CLAVES_VISTAS if k in conocidas]
    v["activas"] = list(dict.fromkeys(vistas))
    m["agrupar_carpetas"] = bool(m["agrupar_carpetas"])
    cfg["dashboard"]["con_servidor"] = bool(cfg["dashboard"]["con_servidor"])
    try:
        v["agrupar_desde"] = max(0, int(v["agrupar_desde"]))
        v["agrupar_letras"] = bool(v["agrupar_letras"]) and v["agrupar_desde"] > 0
    except (TypeError, ValueError):
        errores.append(T("e.agrupar"))
    d = str(cfg["cache"]["directorio"] or "").strip().strip('"')
    cfg["cache"]["directorio"] = os.path.normpath(d) if d else ""
    if d:
        try:
            os.makedirs(cfg["cache"]["directorio"], exist_ok=True)
        except OSError as e:
            errores.append(T("e.cache", err=e))
    return cfg, errores, avisos


OPS_FILTRO = ("es", "contiene", "no_contiene", "existe", "falta", "mayor", "menor")


def validar_personalizadas(lista):
    out, vistos = [], set()
    for x in lista if isinstance(lista, list) else []:
        if not isinstance(x, dict):
            continue
        vid = re.sub(r"[^a-z0-9]", "", str(x.get("id") or "").lower())[:12]
        if not vid or not vid.startswith("c"):
            vid = "c" + uuid.uuid4().hex[:7]
        if vid in vistos:
            continue
        niveles = [str(n).strip().casefold()[:40] for n in (x.get("niveles") or []) if str(n).strip()][:3]
        niveles = [n for n in niveles if n != "album"]
        if not niveles:
            continue
        filtro = []
        for f in (x.get("filtro") or [])[:4]:
            if isinstance(f, dict) and f.get("tag") and f.get("op") in OPS_FILTRO:
                filtro.append({"tag": str(f["tag"]).strip().casefold()[:40], "op": f["op"],
                               "valor": str(f.get("valor") or "").strip()[:80]})
        vistos.add(vid)
        out.append({"id": vid, "nombre": str(x.get("nombre") or "").strip()[:60] or niveles[0].capitalize(),
                    "niveles": niveles, "filtro": filtro, "plantilla_album": str(x.get("plantilla_album") or "").strip()[:120],
                    "sin_tag": bool(x.get("sin_tag"))})
    return out[:20]


_ALIAS_CAMPO = {"name": "nombre", "count": "n", "tracks": "pistas", "albums": "albumes", "álbumes": "albumes",
                "year": "año", "anio": "año", "artist": "artista", "quality": "calidad", "format": "formato",
                "genre": "genero", "género": "genero", "álbum": "album", "years": "años", "anios": "años",
                "release_year": "año_edicion", "anio_edicion": "año_edicion",
                "title": "titulo", "título": "titulo", "track": "numero", "tracknumber": "numero", "número": "numero",
                "disc": "disco", "discnumber": "disco", "date": "fecha", "originaldate": "fecha_original",
                "originalyear": "año", "original_date": "fecha_original"}
_RE_CAMPO = re.compile(r"\{([^{}]{1,40})\}")
_RE_VACIOS = re.compile(r"\s*(\(\s*\)|\[\s*\]|\{\s*\})")


_PLANT_CACHE = {}
_PISTA_DEFECTO = "{titulo}"


@lru_cache(maxsize=64)
def _lista_tags(texto):
    return tuple(dict.fromkeys(t.strip().casefold() for t in re.split(r"[,;\n]", texto or "") if t.strip()))[:8]


def lista_tags(v):
    """'display artist, artist label' -> ('display artist', 'artist label')"""
    return _lista_tags(v if isinstance(v, str) else ", ".join(map(str, v or ())))


def tag_mostrado(x, tags):
    """El primer tag de la lista que tenga valor en los tags extra de una pista (o '')."""
    if x and tags:
        for t in tags:
            v = x.get(t)
            if v:
                return v
    return ""


_CAMPOS_CONTEO = {"n", "pistas", "albumes"}            # solo estos llevan separador de miles (años no: 2017)


def _compilar_plantilla(tpl):
    """'{album} [{año}]' -> ['', 'album', ' [', 'año', ']'] (literales en posiciones pares, campos en impares)."""
    c = _PLANT_CACHE.get(tpl)
    if c is None:
        c = _RE_CAMPO.split(tpl)
        for j in range(1, len(c), 2):
            k = c[j].strip().casefold()
            c[j] = _ALIAS_CAMPO.get(k, k)
        if len(_PLANT_CACHE) > 256:
            _PLANT_CACHE.clear()
        _PLANT_CACHE[tpl] = c
    return c


_RE_GRUPO = re.compile(r"<([^<>]*\{[^{}]{1,40}\}[^<>]*)>")
_GRUPOS_CACHE = {}


def _compilar_grupos(tpl):
    """Grupos opcionales '<…>' (como los [] de foobar2000): '{album}< • {version}>' ->
    ('{album}{\x01g0}', [' • {version}']). Cada grupo se resuelve aparte y entra como un campo más, así que un
    valor con '{' o '<' no se interpreta. Solo cuenta como grupo si tiene al menos un campo."""
    c = _GRUPOS_CACHE.get(tpl)
    if c is None:
        grupos = []

        def sub(m):
            grupos.append(m.group(1))
            return "{\x01g%d}" % (len(grupos) - 1)
        c = (_RE_GRUPO.sub(sub, tpl), grupos) if "<" in tpl else (tpl, [])
        if len(_GRUPOS_CACHE) > 256:
            _GRUPOS_CACHE.clear()
        _GRUPOS_CACHE[tpl] = c
    return c


# Modificadores de campo/nivel: "{originaldate.decade}" -> "1960s", "{releasetime.year}" -> "1967".
# Sirven en plantillas y en los niveles de las vistas personalizadas, con cualquier tag que traiga una fecha.
_MODS = {"decade": "decada", "decada": "decada", "década": "decada", "year": "anio", "año": "anio", "anio": "anio"}
_RE_ANIO = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def partir_mod(tag):
    """'originaldate.decade' -> ('originaldate', 'decada'); sin modificador -> (tag, None)."""
    if "." in tag:
        b, _, m = tag.rpartition(".")
        m = _MODS.get(m.strip().casefold())
        if m and b:
            return b, m
    return tag, None


def aplicar_mod(mod, valor):
    """'1967-06-01' / 1967 -> '1960s' (decada) o '1967' (anio); sin año -> ''."""
    m = _RE_ANIO.search(str(valor or ""))
    if not m:
        return ""
    y = int(m.group(1))
    return f"{y // 10 * 10}s" if mod == "decada" else str(y)


def _rellenar(partes, campos, extra):
    """-> (texto, ¿algún campo con valor?, ¿algún campo vacío?)"""
    out, hay, vacio = [], False, False
    for j, x in enumerate(partes):
        if not j % 2:
            out.append(x)
            continue
        v = campos.get(x)
        if v is None and "." in x:
            b, mod = partir_mod(x)
            if mod:
                b = _ALIAS_CAMPO.get(b, b)
                bv = campos.get(b)
                v = aplicar_mod(mod, bv if bv is not None or not extra else extra(b))
        if v is None and extra:
            v = extra(x)
        if v in (None, "", 0):
            vacio = True
            continue
        hay = True
        out.append(f"{v:,}" if isinstance(v, int) and x in _CAMPOS_CONTEO else str(v))
    return "".join(out), hay, vacio


def plantilla(tpl, campos, extra=None):
    """'{album} [{año}]' + campos -> 'Kind of Blue [1959]'. Campos vacíos o 0 desaparecen con sus () / [].
    '<…>' es un grupo opcional: se muestra solo si alguno de sus campos tiene valor
    ('{album}< • {version}>< ({remasterdate} Remaster)>' -> 'Queen • Deluxe Edition (2011 Remaster)')."""
    principal, grupos = _compilar_grupos(tpl)
    if grupos:
        campos = dict(campos)
        for i, g in enumerate(grupos):
            t, hay, vacio = _rellenar(_compilar_plantilla(g), campos, extra)
            if hay and vacio:
                t = _RE_VACIOS.sub("", t)
            campos["\x01g%d" % i] = t if hay else ""
    s, _, vacio = _rellenar(_compilar_plantilla(principal), campos, extra)
    if vacio:
        s = " ".join(_RE_VACIOS.sub("", s).split()).strip(" -·•|,;/")
    return s or str(campos.get("nombre") or campos.get("album") or "")


LOSSY = {"mp3", "ogg", "oga", "opus", "aac"}


def calidad_pista(ext, bits, sr, br):
    """Grupo de calidad: dsd | hires | cd | lossy (como #AudioQuality de MinimServer, en simple)."""
    if ext in ("dsf", "dff"):
        return "dsd"
    if ext in LOSSY or (ext == "m4a" and br and br < 500000):
        return "lossy"
    return "hires" if (bits or 0) > 16 or (sr or 0) > 48000 else "cd"


def resolucion(ext, bits, sr, br):
    """Texto corto de resolución: '24/96', '16/44.1', 'DSD64', 'MP3 320k'."""
    c = calidad_pista(ext, bits, sr, br)
    if c == "dsd":
        return f"DSD{round((sr or 2822400) / 44100)}"
    if c == "lossy":
        return f"{ext.upper()} {round(br / 1000)}k" if br else ext.upper()
    if sr:
        return f"{bits or 16}/{sr / 1000:g}"
    return ext.upper()


def fecha_dc(v):
    """1975 / '2005-03' / '2005-03-01' -> '2005-03-01' (dc:date completa, como esperan los controles)."""
    t = str(v)
    partes = (t.split("-") + ["01", "01"])[:3]
    return "-".join([partes[0]] + [p.zfill(2) for p in partes[1:]])


def nombres_artistas(plural, singular):
    """Nombres para los ÍNDICES: ARTISTS/ALBUMARTISTS (un nombre por valor) si existen; si no, el tag singular
    partido por ';'. ("Gorillaz feat. Elton John & 6LACK" + ARTISTS -> Gorillaz, Elton John, 6LACK.)"""
    return _multi(plural) if plural else _multi(singular)


def mostrar_artistas(s):
    """Para MOSTRAR: "A; B; C" (varios valores) -> "A, B & C". Un texto con su propia frase ("feat.") no cambia."""
    if not s or ";" not in s:
        return s or ""
    p = _multi(s)
    return p[0] if len(p) == 1 else ", ".join(p[:-1]) + " & " + p[-1]


def nombre_tag(tag):
    b, mod = partir_mod(tag)
    if mod:
        return f"{nombre_tag(b)} ({T('mod.' + mod)})"
    t = T("tag." + tag)
    return tag.lstrip("#") if t == "tag." + tag else t


def calidad_album(a):
    return resolucion(a.get("ext", ""), a.get("bits", 0), a.get("sr", 0), a.get("br", 0)) if a.get("ext") else ""


def clave_raiz(ruta, existentes=()):
    """Id corto de una carpeta de música = hash de su NOMBRE (no de la ruta completa): cambiar la letra de la unidad
    o mover la biblioteca de lugar no cambia los IDs ni las URLs (los favoritos del reproductor siguen sirviendo).
    Dos carpetas con el mismo nombre: la segunda lleva un sufijo. Una unidad completa (H:\\) usa su ruta."""
    base = os.path.basename(os.path.abspath(ruta).rstrip("\\/")) or os.path.abspath(ruta)
    semilla = "raiz:" + base.casefold()
    for n in range(1, 100):
        key = hashlib.sha1((semilla if n == 1 else f"{semilla}#{n}").encode("utf-8")).hexdigest()[:8]
        if key not in existentes:
            return key
    return hashlib.sha1(os.path.normcase(os.path.abspath(ruta)).encode("utf-8")).hexdigest()[:8]


def clave_raiz_vieja(ruta):
    """Id de la 1.1 (hash de la ruta completa): solo para migrar el historial de reproducciones."""
    return hashlib.sha1(os.path.normcase(os.path.abspath(ruta)).encode("utf-8")).hexdigest()[:8]


def dir_cache(cfg):
    d = cfg["cache"]["directorio"]
    return Path(d) if d else DIR_DATOS


# ═══════════════════════════════════════════════════════════════════════════ tags

def _val(v):
    if v is None:
        return ""
    if hasattr(v, "text"):
        v = v.text
    if isinstance(v, (list, tuple)):
        if not v:
            return ""
        v = v[0]
    if isinstance(v, tuple):
        v = v[0]
    return str(v).strip()


def _first(tags, *keys):
    if tags is None:
        return ""
    for k in keys:
        try:
            s = _val(tags.get(k))
        except Exception:
            s = ""
        if s:
            return s
    return ""


def _unir(valores):
    """Varios valores de un tag (ARTIST= repetido, ID3 con varios textos…) -> "A; B" sin duplicados."""
    out = []
    for v in valores:
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    return "; ".join(out)


def _todos(tags, *keys):
    """Todos los valores del primer tag que exista (para artist, albumartist y genre multivalor)."""
    if tags is None:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v is None:
            continue
        if hasattr(v, "genres"):                              # ID3 TCON: resuelve "(17)" -> "Rock"
            v = v.genres
        elif hasattr(v, "text"):
            v = v.text
        if not isinstance(v, (list, tuple)):
            v = [v]
        s = _unir(x[0] if isinstance(x, tuple) else x for x in v)
        if s:
            return s
    return ""


def _plural(tags, *keys):
    """ARTISTS / ALBUMARTISTS (un nombre por valor) en cualquier formato -> "A; B; C"."""
    if tags is None:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        vals = _texto_tag(v) if v is not None else []
        if len(vals) == 1 and "\x00" in str(vals[0]):
            vals = str(vals[0]).split("\x00")
        if vals:
            return _unir(vals)
    return ""


def _bandera(tags, *keys):
    """True si un tag tipo COMPILATION / TCMP / cpil está activo."""
    s = _first(tags, *keys).lower()
    return s in ("1", "true", "yes")


def imagenes_embebidas(f):
    """[(tipo, bytes)] de las portadas embebidas en cualquier formato soportado por mutagen."""
    pics = []
    try:
        if getattr(f, "pictures", None):                       # FLAC
            pics += [(p.type, p.data) for p in f.pictures]
    except Exception:
        pass
    tags = getattr(f, "tags", None)
    if tags is None:
        return pics
    try:
        if hasattr(tags, "getall"):                            # ID3
            pics += [(fr.type, fr.data) for fr in tags.getall("APIC")]
    except Exception:
        pass
    try:
        if "covr" in tags:                                     # MP4
            pics += [(3, bytes(c)) for c in tags["covr"]]
    except Exception:
        pass
    try:
        if "metadata_block_picture" in tags:                   # Ogg / Opus
            for x in tags["metadata_block_picture"]:
                p = Picture(base64.b64decode(x))
                pics.append((p.type, p.data))
    except Exception:
        pass
    for key in ("Cover Art (Front)", "Cover Art (Other)"):    # APEv2
        try:
            if key in tags:
                raw = tags[key].value
                pics.append((3, raw.split(b"\x00", 1)[1] if b"\x00" in raw else raw))
        except Exception:
            pass
    return [p for p in pics if p[1]]


# Tipos de imagen embebida (FLAC RFC 9639 tabla 13 = ID3 APIC): 3 frontal, 6 etiqueta del disco, 0 otra,
# 18 ilustración, 4 contraportada… 1/2 son íconos de 32 px: nunca se usan como portada.
PRIO_IMG_ALBUM = (3, 6, 0, 18, 4, 5, 15, 14, 13, 16, 19, 20, 10, 8, 7, 9, 11, 12, 17)
PRIO_IMG_ARTISTA = (8, 7, 10, 19)          # artista, solista, banda, logotipo de la banda


def elegir_imagen(pics, prioridad=PRIO_IMG_ALBUM):
    """[(tipo, bytes)] -> bytes de la imagen con mejor prioridad (o None)."""
    for t in prioridad:
        for tp, d in pics:
            if tp == t:
                return d
    return None


def tipos_imagen(m):
    """Tipos de imagen embebida de una pista según la caché ("pt"; si solo hay "art", se asume frontal)."""
    return m.get("pt") or ([3] if m.get("art") else [])


# Tags extra que se guardan (además de los básicos) para las vistas personalizadas y las plantillas.
# Nombres normalizados en minúsculas; cualquier tag propio (TXXX, ----:com.apple.iTunes, Vorbis) entra con su nombre.
_ALIAS_TAG = {
    "tcom": "composer", "\xa9wrt": "composer", "tpe3": "conductor", "\xa9con": "conductor",
    "tpub": "label", "\xa9pub": "label", "publisher": "label", "organization": "label", "recordlabel": "label",
    "tmoo": "mood", "grp1": "grouping", "\xa9grp": "grouping", "tit1": "grouping", "contentgroup": "grouping",
    "tsst": "discsubtitle", "setsubtitle": "discsubtitle", "text": "lyricist", "tpe4": "remixer", "mixartist": "remixer",
    "tbpm": "bpm", "tmpo": "bpm", "tden": "encodingtime", "tmed": "media", "tlan": "language", "tkey": "initialkey",
    "\xa9wrk": "work", "mvnm": "movement", "tdrl": "releasetime", "tcop": "copyright", "popm": "rating",
}
_TAGS_BASICOS = {
    "title", "artist", "album", "albumartist", "album artist", "tracknumber", "discnumber", "date", "year",
    "originaldate", "originalyear", "genre", "albumsort", "compilation", "tit2", "tpe1", "talb", "tpe2", "trck",
    "tpos", "tdrc", "tyer", "tdor", "tory", "tcon", "tsoa", "tcmp", "\xa9nam", "\xa9art", "\xa9alb", "aart",
    "trkn", "disk", "\xa9day", "\xa9gen", "soal", "cpil", "track", "disc",
}
_TAGS_FUERA = ("musicbrainz", "replaygain", "itun", "acoustid", "lyrics", "unsyncedlyrics", "uslt", "sylt", "cuesheet",
               "comment", "comm", "description", "desc", "ldes", "encoder", "encodedby", "encoded", "encoding",
               "tenc", "tsse", "\xa9too", "tool", "log", "accuraterip", "ctdb", "isrc", "asin", "barcode", "upc",
               "catalognumber", "script", "artists", "albumartists", "album_artists", "tso", "soaa", "soar", "sonm", "soco", "sosn", "totaltracks", "tracktotal", "totaldiscs",
               "disctotal", "length", "tlen", "covr", "apic", "metadata_block_picture", "cover art", "priv", "geob",
               "ufid", "mcdi", "ncon", "pcnt", "rgad", "rva", "equ", "wxxx", "wcom", "woar", "tflt", "tdtg", "tsrc",
               "ownr", "tofn", "tsiz", "stik", "pgap", "purd", "apid", "cnid", "atid", "plid", "geid", "sfid", "cmid",
               "akid", "xid ", "rtng", "hdvd", "tvsh", "tven", "tves", "tvsn", "tvnn", "pcst", "purl", "egid", "keyw",
               "catg", "\xa9lyr", "\xa9cmt", "\xa9des", "\xa9enc", "\xa9st3", "\xa9xyz", "chpl", "ctoc", "etco",
               "id3v1", "id3", "tipl", "tmcl", "ipls")
MAX_TAGS_EXTRA, MAX_LARGO_TAG = 40, 120


def _clave_extra(k):
    """Nombre normalizado de un tag extra, o None si no se guarda."""
    k = str(k).strip()
    if k.upper().startswith("TXXX:"):
        k = k[5:]
    elif k.startswith("----:"):                              # MP4 freeform: ----:com.apple.iTunes:LABEL
        k = k.rsplit(":", 1)[-1]
    k = k.casefold().strip()
    if not k or len(k) > 40:
        return None
    if ":" in k and not k.startswith("\xa9"):              # performer:piano (Picard), POPM:correo -> performer, popm
        k = k.split(":", 1)[0].strip()
    k = _ALIAS_TAG.get(k, k)
    if k == "encodingtime":
        return k
    if k in _TAGS_BASICOS or k.endswith("sort") or any(k.startswith(p) for p in _TAGS_FUERA):
        return None
    return k or None


def _texto_tag(v):
    """Valor de un tag (mutagen o lista de str) -> lista de textos."""
    if v is None:
        return []
    if hasattr(v, "rating") and hasattr(v, "email"):         # ID3 POPM -> estrellas 1-5
        return [str(max(1, round(v.rating / 51)))] if v.rating else []
    if hasattr(v, "text"):
        v = v.text
    elif hasattr(v, "value") and not isinstance(v, (str, bytes)):
        v = v.value
    if not isinstance(v, (list, tuple)):
        v = [v]
    out = []
    for x in v:
        if isinstance(x, tuple):
            x = x[0]
        if isinstance(x, bytes):                               # MP4 freeform (MP4FreeForm es bytes)
            x = x.decode("utf-8", "replace")
        x = str(x).strip().strip("\x00")
        if x and len(x) <= MAX_LARGO_TAG:
            out.append(x)
    return out


def tags_extra(pares):
    """[(clave, valor)] -> {"composer": "A; B", "label": "…", …} (solo texto corto, máx. MAX_TAGS_EXTRA)."""
    x = {}
    for k, v in pares:
        ck = _clave_extra(k)
        if not ck:
            continue
        vals = _texto_tag(v)
        if not vals:
            continue
        if ck in x:
            vals = _multi(x[ck]) + vals
        elif len(x) >= MAX_TAGS_EXTRA:
            continue
        x[ck] = _unir(vals)
    return x


def fecha_tag(s):
    """ENCODINGTIME / TDEN en ISO 8601 de precisión variable (2020, 2020-06, 2020-06-27T10:30) -> epoch UTC."""
    m = re.match(r"\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?(?:[T ](\d{1,2})(?::(\d{2}))?(?::(\d{2}))?)?", s or "")
    if not m or not 1900 <= int(m.group(1)) <= 2100:
        return 0
    import calendar
    try:
        return calendar.timegm((int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1), int(m.group(4) or 0),
                                int(m.group(5) or 0), int(m.group(6) or 0), 0, 0, 0))
    except (ValueError, OverflowError):
        return 0


def _completar_extra(meta, x):
    """Aplica los tags extra al meta: 'x' (solo si hay) y 'add' (fecha de ENCODINGTIME)."""
    enc = x.pop("encodingtime", "")
    add = fecha_tag(_multi(enc)[0] if enc else "")
    if add:
        meta["add"] = add
    if x:
        meta["x"] = x


# Sube cuando cambia lo que se extrae de los tags: la caché se vacía una vez y se vuelve a leer.
# 2: todos los valores de artist/albumartist/genre, originaldate primero, albumsort y compilation.
# 3: tags extra ("x": composer, conductor, label, tags propios…), ENCODINGTIME ("add") y tipos de imagen ("pt").
# 4: ARTISTS / ALBUMARTISTS ("arts", "aarts") y fecha de la edición ("fecha", "ry") aparte del año original.
# 5: fecha original completa ("of": 1967-06-01) para ordenar álbumes del mismo año.
# Desde la 5 la caché NO se vacía: cada fila guarda con qué versión se leyó (columna v) y las viejas se siguen
# usando mientras se releen en segundo plano (Cache.actualizar_lote), así una biblioteca grande nunca queda vacía.
VERSION_LECTOR = 5


def _meta_de_tags(meta, get, todos):
    """get(k) -> primer valor; todos(k) -> todos los valores unidos con "; " (tags Vorbis)."""
    meta["title"] = get("title")
    meta["artist"] = todos("artist")
    meta["album"] = get("album")
    meta["aartist"] = todos("albumartist") or todos("album artist")
    meta["track"] = _int(get("tracknumber"))
    meta["disc"] = _int(get("discnumber"))
    _poner_original(meta, get("originaldate") or get("originalyear") or get("date") or get("year"))
    meta["genre"] = todos("genre")
    meta["asort"] = get("albumsort")
    meta["comp"] = 1 if get("compilation").lower() in ("1", "true", "yes") else 0
    meta["arts"] = todos("artists")                      # Picard: un artista por valor (para los índices)
    meta["aarts"] = todos("albumartists")
    _poner_fecha(meta, get("date") or get("year"))


_RE_FECHA = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")


def _poner_original(meta, txt):
    """Año original ("year", para ordenar y agrupar) y, si trae mes, la fecha completa ("of": 1967-06-01) para
    ordenar dos álbumes del mismo año por fecha y no por título (Sgt. Pepper antes que Magical Mystery Tour)."""
    m = _RE_FECHA.search(txt or "")
    meta["year"] = int(m.group(1)) if m else 0
    if m and m.group(2):
        meta["of"] = "-".join([m.group(1)] + [f"{int(x):02d}" for x in m.groups()[1:] if x])


def _poner_fecha(meta, txt):
    """Fecha de ESTA edición (date/TDRC): 'fecha' = 2005-03-01 / 2005-03 / 2005 y 'ry' = año. 'year' sigue siendo
    el año original (originaldate primero) para ordenar; 'fecha' va en dc:date y en el campo {años}."""
    m = _RE_FECHA.search(txt or "")
    if not m or not 1000 <= int(m.group(1)) <= 2999:
        return
    partes = [m.group(1)] + [f"{int(x):02d}" for x in m.groups()[1:] if x]
    meta["fecha"] = "-".join(partes)
    meta["ry"] = int(m.group(1))


def leer_flac_rapido(ruta):
    """Lee solo STREAMINFO + VORBIS_COMMENT y detecta PICTURE SIN cargar la imagen (que puede pesar MB).
    Unos KB por archivo en vez de todo el bloque de metadatos. None si el archivo no es FLAC "limpio"."""
    with open(ruta, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return None                                   # p. ej. ID3 delante: que lo lea mutagen
        info, tags, tipos = None, {}, set()
        while True:
            h = fh.read(4)
            if len(h) < 4:
                return None
            ultimo, tipo, largo = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], "big")
            if tipo == 0 and largo >= 18:                 # STREAMINFO
                d = fh.read(largo)
                sr = (d[10] << 12) | (d[11] << 4) | (d[12] >> 4)
                ch = ((d[12] >> 1) & 0x07) + 1
                bits = (((d[12] & 1) << 4) | (d[13] >> 4)) + 1
                total = ((d[13] & 0x0F) << 32) | int.from_bytes(d[14:18], "big")
                info = (sr, ch, bits, total)
            elif tipo == 4:                               # VORBIS_COMMENT
                d = fh.read(largo)
                p = 4 + int.from_bytes(d[0:4], "little")
                n = int.from_bytes(d[p:p + 4], "little")
                p += 4
                for _ in range(n):
                    l = int.from_bytes(d[p:p + 4], "little")
                    k, _, v = d[p + 4:p + 4 + l].decode("utf-8", "replace").partition("=")
                    tags.setdefault(k.lower(), []).append(v.strip())
                    p += 4 + l
            elif tipo == 6 and largo >= 4:                # PICTURE: solo el tipo, sin cargar la imagen
                tipos.add(int.from_bytes(fh.read(4), "big"))
                fh.seek(largo - 4, 1)
            else:
                fh.seek(largo, 1)
            if ultimo:
                break
        inicio_audio = fh.tell()
        tam = os.fstat(fh.fileno()).st_size
    if not info or not info[0]:
        return None
    sr, ch, bits, total = info
    dur = total / sr if total else 0
    meta = {"dur": round(dur, 3), "sr": sr, "ch": ch, "bits": bits,
            "br": int((tam - inicio_audio) * 8 / dur) if dur else 0}
    _meta_de_tags(meta, lambda k: next((v for v in tags.get(k, ()) if v), ""), lambda k: _unir(tags.get(k, ())))
    _completar_extra(meta, tags_extra((k, v) for k, vs in tags.items() for v in vs))
    _poner_tipos(meta, tipos)
    return {k: v for k, v in meta.items() if v}


def firmas_imagen_flac(ruta):
    """{tipo: (largo, muestra)} de las imágenes embebidas de un FLAC sin cargarlas: el largo exacto en bytes más
    dos trozos (2 KB del centro y 1 KB del final). Mismo tipo + largo + trozos = la misma imagen; dos imágenes
    distintas del mismo largo (p. ej. fondos lisos) difieren en sus datos."""
    out = {}
    with open(ruta, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return None
        while True:
            h = fh.read(4)
            if len(h) < 4:
                return None
            ultimo, tipo, largo = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], "big")
            if tipo == 6 and largo >= 32:
                ini = fh.tell()
                d = fh.read(8)
                t, lm = int.from_bytes(d[:4], "big"), int.from_bytes(d[4:8], "big")
                fh.seek(lm, 1)
                ld = int.from_bytes(fh.read(4), "big")
                fh.seek(ld + 16, 1)
                n = int.from_bytes(fh.read(4), "big")
                if t not in out and n > 0:
                    datos = fh.tell()
                    fh.seek(datos + n // 2)
                    centro = fh.read(min(2048, n))
                    fh.seek(datos + max(0, n - 1024))
                    out[t] = (n, centro + fh.read(1024))
                fh.seek(ini + largo)
            else:
                fh.seek(largo, 1)
            if ultimo:
                return out


def _elegida(firmas, prioridad=PRIO_IMG_ALBUM):
    return next(((t, firmas[t]) for t in prioridad if t in firmas), None)


def _poner_tipos(meta, tipos):
    """'art' = hay una imagen útil (no ícono); 'pt' = tipos, solo si no es únicamente la frontal."""
    utiles = sorted({int(t) for t in tipos} - {1, 2})
    meta["art"] = bool(utiles)
    if utiles and utiles != [3]:
        meta["pt"] = utiles


def leer_tags(ruta):
    if ruta[-5:].lower() == ".flac":
        try:
            m = leer_flac_rapido(ruta)
            if m is not None:
                return m
        except Exception as e:
            log.debug("Fast FLAC reader failed on %s (%s); falling back to mutagen", ruta, e)
    meta = {}
    try:
        f = mutagen.File(ruta)
    except Exception as e:
        log.debug("Could not read %s: %s", ruta, e)
        return meta
    if f is None:
        return meta
    info = getattr(f, "info", None)
    if info is not None:
        meta["dur"] = round(float(getattr(info, "length", 0) or 0), 3)
        meta["br"] = int(getattr(info, "bitrate", 0) or 0)
        meta["sr"] = int(getattr(info, "sample_rate", 0) or 0)
        meta["ch"] = int(getattr(info, "channels", 0) or 0)
        meta["bits"] = int(getattr(info, "bits_per_sample", 0) or 0)
    t = f.tags
    meta["title"] = _first(t, "title", "TIT2", "\xa9nam", "Title")
    meta["artist"] = _todos(t, "artist", "TPE1", "\xa9ART", "Artist")
    meta["album"] = _first(t, "album", "TALB", "\xa9alb", "Album")
    meta["aartist"] = _todos(t, "albumartist", "album artist", "TPE2", "aART", "Album Artist")
    meta["track"] = _int(_first(t, "tracknumber", "TRCK", "trkn", "Track"))
    meta["disc"] = _int(_first(t, "discnumber", "TPOS", "disk", "Disc"))
    _poner_original(meta, _first(t, "originaldate", "originalyear", "TDOR", "TXXX:originalyear", "date", "year",
                                 "TDRC", "TYER", "\xa9day", "Year"))
    meta["genre"] = _todos(t, "genre", "TCON", "\xa9gen", "Genre")
    meta["asort"] = _first(t, "albumsort", "TSOA", "soal", "ALBUMSORT")
    meta["comp"] = 1 if _bandera(t, "compilation", "TCMP", "cpil", "Compilation") else 0
    meta["arts"] = _plural(t, "artists", "TXXX:ARTISTS", "TXXX:Artists", "----:com.apple.iTunes:ARTISTS", "Artists")
    meta["aarts"] = _plural(t, "albumartists", "TXXX:ALBUMARTISTS", "TXXX:AlbumArtists", "TXXX:album_artists",
                            "----:com.apple.iTunes:ALBUMARTISTS", "Albumartists")
    _poner_fecha(meta, _first(t, "date", "TDRC", "TYER", "\xa9day", "year", "Year"))
    _poner_tipos(meta, [tp for tp, _ in imagenes_embebidas(f)])
    if t is not None:
        try:
            pares = []
            for k in list(t.keys()):
                try:
                    pares.append((k, t[k]))
                except Exception:
                    pass
            _completar_extra(meta, tags_extra(pares))
        except Exception as e:
            log.debug("Extra tags of %s: %s", ruta, e)
    return {k: v for k, v in meta.items() if v}


class Reproducciones:
    """Cuántas veces y cuándo se reprodujo cada pista (datos/reproducciones.db; no se borra al limpiar la caché).
    Una reproducción = el reproductor ya recibió la mitad del archivo (sumando sus pedidos y saltos dentro de la
    misma escucha). Así no cuentan las pistas que se saltan a los segundos ni la siguiente que el reproductor pide
    por adelantado para el gapless si al final no suena."""

    UMBRAL = 0.5
    SESION = 900          # s: pedidos de la misma pista y el mismo reproductor dentro de esto son la misma escucha

    def __init__(self, ruta):
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(ruta), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS rep (key TEXT, rel TEXT, n INTEGER, ultima REAL, PRIMARY KEY (key, rel))")
        self.lock = threading.Lock()
        self.escuchas = {}                    # (ip, key, rel) -> [bytes, contada, último]
        self.version = 0

    def avance(self, ip, key, rel, n, tam, desde_inicio=False):
        """Bytes enviados de una pista a un reproductor; cuenta la reproducción al llegar al umbral.
        Un pedido nuevo desde el inicio, cuando esa escucha ya contó, es otra escucha (repetir la canción)."""
        ahora = time.time()
        k = (ip, key, rel)
        with self.lock:
            e = self.escuchas.get(k)
            if e is None or ahora - e[2] > self.SESION or (desde_inicio and e[1]):
                e = self.escuchas[k] = [0, False, ahora]
                if len(self.escuchas) > 256:
                    self.escuchas = {x: v for x, v in self.escuchas.items() if ahora - v[2] < self.SESION}
                    self.escuchas[k] = e
            e[0] += n
            e[2] = ahora
            if e[1] or e[0] < tam * self.UMBRAL:
                return
            e[1] = True
        self.registrar(key, rel)

    def mover(self, viejo, nuevo):
        """Archivo movido o renombrado: su historial lo sigue. viejo/nuevo = (key, rel)."""
        with self.lock:
            self.db.execute("UPDATE OR IGNORE rep SET key=?, rel=? WHERE key=? AND rel=?", (*nuevo, *viejo))
            self.version += 1

    def registrar(self, key, rel):
        ahora = time.time()
        with self.lock:
            self.db.execute("INSERT INTO rep VALUES (?,?,1,?) ON CONFLICT(key, rel) DO UPDATE SET n=n+1, ultima=excluded.ultima",
                            (key, rel, ahora))
            self.version += 1

    def migrar_claves(self, mapa):
        """Ids de carpeta viejos -> nuevos (ruta completa -> nombre de carpeta)."""
        with self.lock:
            for viejo, nuevo in mapa.items():
                if viejo != nuevo:
                    self.db.execute("UPDATE OR REPLACE rep SET key=? WHERE key=?", (nuevo, viejo))

    def lista(self, recientes, limite=100):
        orden = "ultima DESC" if recientes else "n DESC, ultima DESC"
        with self.lock:
            return self.db.execute(f"SELECT key, rel, n FROM rep ORDER BY {orden} LIMIT ?", (limite,)).fetchall()


class CacheTags:
    """Tags leídos, persistidos en SQLite y validados por mtime + tamaño.
    Lecturas en paralelo; las que no terminan dentro del presupuesto siguen en segundo plano."""

    def __init__(self, ruta):
        from concurrent.futures import ThreadPoolExecutor
        self.db = sqlite3.connect(str(ruta), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS pistas (ruta TEXT PRIMARY KEY, mtime INTEGER, tam INTEGER, meta TEXT)")
        # identidad del archivo independiente de la ruta: mover un álbum no obliga a releerlo
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_identidad ON pistas (tam, mtime)")
        # portadas por contenido: fuente (archivo + fecha + tamaño + opciones) -> hash de la imagen ("" = no tiene)
        self.db.execute("CREATE TABLE IF NOT EXISTS portadas (clave TEXT PRIMARY KEY, h TEXT)")
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if "v" not in {c[1] for c in self.db.execute("PRAGMA table_info(pistas)")}:
            # versión del lector con que se leyó cada fila: las anteriores se releen poco a poco, sin vaciar la caché
            self.db.execute(f"ALTER TABLE pistas ADD COLUMN v INTEGER NOT NULL DEFAULT {min(v, VERSION_LECTOR)}")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_v ON pistas (v)")
        if v < VERSION_LECTOR:
            n = self.db.execute("SELECT COUNT(*) FROM pistas WHERE v < ?", (VERSION_LECTOR,)).fetchone()[0]
            if n:
                log.info("Tag reader updated (v%d -> v%d): %d cached tracks will be read again in the background",
                         v, VERSION_LECTOR, n)
            self.db.execute(f"PRAGMA user_version={VERSION_LECTOR}")
        self._cursor_v = 0                # rowid por donde va la relectura en segundo plano
        self.movidos, self.al_mover = [], None
        self.reubicados = 0
        self.version = 0                  # sube con cada cambio; el índice por etiquetas se reconstruye al cambiar
        self.lock = threading.Lock()
        self.leidos = 0
        self.pool = ThreadPoolExecutor(8, thread_name_prefix="tags")
        self.pool_fondo = ThreadPoolExecutor(1, thread_name_prefix="precarga")
        self.en_vuelo = {}
        self.gen_precarga = 0

    def _guardar(self, ruta, mt, tam, meta):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pistas (ruta, mtime, tam, meta, v) VALUES (?,?,?,?,?)",
                            (ruta, mt, tam, json.dumps(meta, ensure_ascii=False), VERSION_LECTOR))
            self.leidos += 1
            self.version += 1

    def _leer(self, ruta, mt, tam):
        try:
            m = leer_tags(ruta)
            self._guardar(ruta, mt, tam, m)
            return m
        finally:
            with self.lock:
                self.en_vuelo.pop(ruta, None)

    def _en_cache(self, archivos):
        """Busca por ruta; si no está (archivo movido/renombrado de carpeta), por identidad:
        mismo nombre de archivo + tamaño + mtime al nanosegundo. Lo encontrado así se re-registra en la ruta nueva."""
        res, faltan, reubicar = {}, [], []
        with self.lock:
            filas = {}
            todas = [a[0] for a in archivos]
            for k in range(0, len(todas), 800):          # por lotes: una consulta por cada 800 archivos, no una por archivo
                parte = todas[k:k + 800]
                for r, mt0, tam0, js in self.db.execute(
                        f"SELECT ruta, mtime, tam, meta FROM pistas WHERE ruta IN ({','.join('?' * len(parte))})", parte):
                    filas[r] = (mt0, tam0, js)
            for ruta, mt, tam in archivos:
                row = filas.get(ruta)
                if row and row[0] == mt and row[1] == tam:
                    res[ruta] = json.loads(row[2])
                    continue
                nombre = os.path.basename(ruta).casefold()
                # Solo es "movido" si la ruta vieja ya no existe: dos archivos distintos pueden coincidir en nombre,
                # tamaño y mtime (copias en lote, FAT/exFAT con mtime de 2 s) y no deben compartir tags.
                hit = next(((m, vl, r) for r, m, vl in self.db.execute(
                    "SELECT ruta, meta, v FROM pistas WHERE tam=? AND mtime=?", (tam, mt))
                    if r != ruta and os.path.basename(r).casefold() == nombre and not os.path.exists(r)), None)
                if hit is not None:
                    res[ruta] = json.loads(hit[0])
                    reubicar.append((ruta, mt, tam, hit[0], hit[1]))
                    self.movidos.append((hit[2], ruta))
                else:
                    faltan.append((ruta, mt, tam))
            if reubicar:
                self.db.executemany("INSERT OR REPLACE INTO pistas (ruta, mtime, tam, meta, v) VALUES (?,?,?,?,?)",
                                    reubicar)
                if self.al_mover:                         # el historial de reproducciones sigue al archivo
                    movidos, self.movidos = self.movidos, []
                    threading.Thread(target=self.al_mover, args=(movidos,), daemon=True).start()
                self.reubicados += len(reubicar)
                self.version += 1
        return res, faltan

    def tocar(self):
        """Sin pistas nuevas pero algo cambió en disco (borrados, movidos): que el índice se rehaga."""
        with self.lock:
            self.version += 1

    def pendientes_lector(self):
        """Pistas leídas con una versión anterior del lector (se releen en segundo plano)."""
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM pistas WHERE v < ?", (VERSION_LECTOR,)).fetchone()[0]

    def actualizar_lote(self, n=64, seguir=lambda: True):
        """Relee hasta n pistas guardadas con un lector anterior (las que siguen igual en disco). Mientras tanto
        sus datos viejos siguen sirviendo. seguir() -> False corta el lote (alguien empezó a navegar).
        Devuelve 0 = no queda nada, 1 = lote hecho, -1 = cortado, 2 = vuelta completa pero quedan pistas
        (cambiaron en disco; las relee la precarga o abrir su carpeta)."""
        with self.lock:
            filas = self.db.execute("SELECT rowid, ruta, mtime, tam FROM pistas WHERE v < ? AND rowid > ? "
                                    "ORDER BY rowid LIMIT ?", (VERSION_LECTOR, self._cursor_v, n)).fetchall()
        if not filas:
            if self._cursor_v:                        # vuelta completa; lo saltado (archivo cambiado) queda para
                self._cursor_v = 0                    # otra vuelta más tarde
                return 2 if self.pendientes_lector() else 0
            return 0
        for rid, ruta, mt, tam in filas:
            if not seguir():
                return -1
            self._cursor_v = rid
            try:
                st = os.stat(ruta)
            except OSError:
                continue                              # desapareció: la limpieza la quitará
            if (st.st_mtime_ns, st.st_size) != (mt, tam):
                continue                              # cambió en disco: se lee al abrirse o en la precarga
            with self.lock:
                if ruta in self.en_vuelo:
                    continue
            try:
                self._guardar(ruta, mt, tam, leer_tags(ruta))
            except Exception:
                log.debug("Re-reading %s", ruta, exc_info=True)
        return 1

    def podar(self, vigentes):
        """Borra de la caché las pistas que ya no existen (rutas fuera de 'vigentes'). Devuelve cuántas."""
        with self.lock:
            viejas = [(r,) for (r,) in self.db.execute("SELECT ruta FROM pistas") if r not in vigentes]
            if viejas:
                self.db.execute("BEGIN")
                self.db.executemany("DELETE FROM pistas WHERE ruta=?", viejas)
                self.db.execute("COMMIT")
                self.version += 1
        return len(viejas)

    def obtener(self, archivos, espera=None):
        """archivos: [(ruta, mtime_ns, tam)] -> {ruta: meta}.
        Con 'espera' (s), las pistas que no se leyeron a tiempo NO vienen en el resultado
        (el llamador usa datos de la ruta) y se terminan de leer en segundo plano."""
        from concurrent.futures import wait
        res, faltan = self._en_cache(archivos)
        if not faltan:
            return res
        futs = {}
        with self.lock:
            for ruta, mt, tam in faltan:
                f = self.en_vuelo.get(ruta)
                if f is None or (not f.running() and not f.done() and f.cancel()):
                    f = self.pool.submit(self._leer, ruta, mt, tam)
                    self.en_vuelo[ruta] = f
                futs[ruta] = f
        wait(list(futs.values()), timeout=espera)
        for ruta, f in futs.items():
            if f.done() and not f.cancelled():
                try:
                    res[ruta] = f.result()
                except Exception:
                    res[ruta] = {}
        return res

    def precargar(self, archivos):
        """Lee en segundo plano (un hilo, baja prioridad) lo que falte. Una precarga nueva cancela la anterior."""
        self.gen_precarga += 1
        gen = self.gen_precarga

        def tarea():
            if gen != self.gen_precarga:
                return
            _, faltan = self._en_cache(archivos)
            for ruta, mt, tam in faltan:
                if gen != self.gen_precarga:
                    return
                with self.lock:
                    if ruta in self.en_vuelo:
                        continue
                try:
                    self._guardar(ruta, mt, tam, leer_tags(ruta))
                except Exception:
                    pass
        self.pool_fondo.submit(tarea)

    def total(self):
        c = getattr(self, "_total", None)
        if c and c[1] == self.version:
            return c[0]
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM pistas").fetchone()[0]
        self._total = (n, self.version)
        return n

    def mapa_portada(self, clave):
        with self.lock:
            r = self.db.execute("SELECT h FROM portadas WHERE clave=?", (clave,)).fetchone()
        return r[0] if r else None

    def guardar_mapa_portada(self, clave, h):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO portadas VALUES (?,?)", (clave, h))

    def limpiar(self):
        with self.lock:
            self.db.execute("DELETE FROM portadas")
            self.db.execute("DELETE FROM pistas")
            self.db.execute("VACUUM")
            self.version += 1


# ═══════════════════════════════════════════════════════════════════════════ biblioteca

# ═══════════════ Playlists (.m3u / .m3u8 / .pls) ═══════════════
PLAYLISTS = {".m3u", ".m3u8", ".pls"}
C_PLAYLIST = "object.container.playlistContainer"
_RE_URL = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_RE_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class Playlists:
    """Playlists que están en las carpetas de música. Escalable:
    - registro liviano (ruta, fecha, tamaño, nº de entradas) en la base de la caché: lo llena cualquier listado de
      carpeta (navegar o la precarga), así la vista "Playlists" no recorre el disco;
    - cada playlist se resuelve solo al abrirla, y queda en memoria (LRU) mientras no cambien ni el archivo ni el
      índice; las pistas se buscan primero en el índice (sin tocar el disco) y solo las que no están se verifican;
    - rutas absolutas de otra máquina (una playlist hecha en Windows usada en Docker, otra letra de unidad) se
      traducen aprendiendo el prefijo una vez por playlist.
    OIDs: v:playlists · v:pl:<raíz>:<ruta> · v:pla:<raíz>:<ruta> (por álbum) · v:plal:<grupo>:<raíz>:<ruta>"""

    MAX_ENTRADAS = 20000
    MAX_BYTES = 8 << 20

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.cache = OrderedDict()                       # (ruta, mt, tam, id(índice)) -> resolución
        db = app.tags.db
        with app.tags.lock:
            self.nueva = not db.execute("SELECT 1 FROM sqlite_master WHERE name='playlists'").fetchone()
            db.execute("CREATE TABLE IF NOT EXISTS playlists (ruta TEXT PRIMARY KEY, mtime INTEGER, tam INTEGER, n INTEGER)")
            self.reg = {r: (mt, tam, n) for r, mt, tam, n in db.execute("SELECT ruta, mtime, tam, n FROM playlists")}
        self.gen = 0                                      # sube con cada alta/baja: invalida la vista "Playlists"
        self.por_dir = defaultdict(set)
        for r in self.reg:
            self.por_dir[os.path.dirname(r)].add(os.path.basename(r))

    # ---- registro (lo alimenta Biblioteca.listar)
    def vistas_en(self, carpeta, encontradas):
        """encontradas: [(nombre, mtime, tam)] de una carpeta recién listada: altas, cambios y bajas."""
        antes = self.por_dir.get(carpeta, set())
        if not encontradas and not antes:
            return
        nuevos, bajas = [], []
        for nombre, mt, tam in encontradas:
            r = os.path.join(carpeta, nombre)
            c = self.reg.get(r)
            if not c or c[:2] != (mt, tam):
                nuevos.append((r, mt, tam, sum(1 for e in self._entradas(r)
                                               if not _RE_URL.match(e) or e[:5].lower() == "file:")))
        ahora = {n for n, _, _ in encontradas}
        bajas = [os.path.join(carpeta, n) for n in antes - ahora]
        if nuevos:
            with self.app.tags.lock:
                self.app.tags.db.executemany("INSERT OR REPLACE INTO playlists VALUES (?,?,?,?)", nuevos)
            with self.lock:
                for r, mt, tam, n in nuevos:
                    self.reg[r] = (mt, tam, n)
                    self.por_dir[carpeta].add(os.path.basename(r))
                self.gen += 1
        if bajas:
            self._bajas(bajas)

    def _bajas(self, rutas):
        with self.app.tags.lock:
            self.app.tags.db.executemany("DELETE FROM playlists WHERE ruta=?", [(r,) for r in rutas])
        with self.lock:
            for r in rutas:
                self.reg.pop(r, None)
                c = os.path.dirname(r)
                self.por_dir[c].discard(os.path.basename(r))
                if not self.por_dir[c]:
                    self.por_dir.pop(c, None)
            self.gen += 1

    def _clave_rel(self, ruta):
        return self.app.bib.clave_rel(ruta)

    def todas(self):
        """[(raíz, rel, nombre, n)] de las playlists registradas que siguen en una carpeta de música."""
        out = []
        with self.lock:
            items = list(self.reg.items())
        verificar = time.monotonic() - getattr(self, "_t_verif", -1e9) > 60
        if verificar:                                    # carpetas borradas enteras: sus playlists se dan de baja
            self._t_verif = time.monotonic()
            idas = {r for r, _ in items if not os.path.isfile(r)}
            if idas:
                self._bajas(idas)
                items = [(r, v) for r, v in items if r not in idas]
        for r, (mt, tam, n) in items:
            kr = self._clave_rel(r)
            if kr and n:
                out.append((kr[0], kr[1], os.path.splitext(os.path.basename(r))[0], n))
        out.sort(key=lambda x: natkey(x[2]))
        return out

    # ---- lectura
    def _entradas(self, ruta):
        """Líneas con rutas (sin comentarios ni #EXTINF), en orden."""
        try:
            with open(ruta, "rb") as f:
                b = f.read(self.MAX_BYTES)
        except OSError:
            return []
        if b.startswith(b"\xef\xbb\xbf"):
            txt = b[3:].decode("utf-8", "replace")
        else:
            try:
                txt = b.decode("utf-8")
            except UnicodeDecodeError:                   # .m3u de Windows: ANSI
                txt = b.decode("cp1252", "replace")
        out = []
        if ruta.lower().endswith(".pls"):
            for ln in txt.splitlines():
                k, _, v = ln.partition("=")
                if k.strip().lower().startswith("file") and v.strip():
                    out.append(v.strip())
        else:
            for ln in txt.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    out.append(ln)
        return out[:self.MAX_ENTRADAS]

    def _a_local(self, entrada, base, trad):
        """Una entrada de la playlist -> ruta local (o None). trad: prefijos aprendidos [(n_componentes, raíz)]."""
        s = entrada
        if _RE_URL.match(s):
            if not s.lower().startswith("file:"):
                return None                              # radios / URLs: no son archivos de la biblioteca
            s = unquote(re.sub(r"^file:(//)?(localhost)?", "", s, flags=re.I))
            if re.match(r"^/[A-Za-z]:", s):
                s = s[1:]
        win = bool(_RE_WIN_ABS.match(s)) or ("\\" in s and "/" not in s)
        partes = [p for p in re.split(r"[\\/]" if (win or ES_WINDOWS) else r"/", s) if p and p != "."]
        if ES_WINDOWS or not win:
            cand = s if os.path.isabs(s) else os.path.join(base, s)
            yield os.path.normpath(cand)
        elif not _RE_WIN_ABS.match(s):                   # relativa con "\" (hecha en Windows) en Linux/macOS
            yield os.path.normpath(os.path.join(base, *partes))
        comps = [p for p in partes if not re.fullmatch(r"[A-Za-z]:", p) and p != ".."]
        for n, raiz in trad:                             # prefijo de otra máquina ya aprendido
            if len(comps) > n:
                yield os.path.join(raiz, *comps[n:])

    def _resolver(self, ruta):
        """Resuelve una playlist: [(raíz, rel, mtime, tam)] únicas y en orden."""
        base = os.path.dirname(ruta)
        idx = self.app.indice
        trad, intentos = [], 0
        vistos, out = set(), []
        raices = [r for r, _ in self.app.bib.raices.values()]
        entradas = self._entradas(ruta)
        # lo que no esté en el índice (carpetas sin indexar…) se busca primero en la caché de tags, en lote:
        # una playlist de miles de pistas en un NAS no hace miles de stat
        primeras = [c for c in (next(self._a_local(e, base, []), None) for e in entradas) if c]
        en_cache = {}
        tags = self.app.tags
        for k in range(0, len(primeras), 800):
            parte = primeras[k:k + 800]
            with tags.lock:
                en_cache.update((r, (mt, tam)) for r, mt, tam in tags.db.execute(
                    f"SELECT ruta, mtime, tam FROM pistas WHERE ruta IN ({','.join('?' * len(parte))})", parte))
        for e in entradas:
            hallado = None
            for cand in self._a_local(e, base, trad):
                kr = self._clave_rel(cand)
                if not kr:
                    continue
                p = idx.pista_por_ruta(*kr)
                if p is not None:                        # en el índice: sin tocar el disco
                    hallado = (p["key"], p["rel"], p["mt"], p["tam"])
                    break
                c = en_cache.get(cand)
                if c:
                    hallado = (kr[0], kr[1], c[0], c[1])
                    break
                try:
                    st = os.stat(cand)
                except OSError:
                    continue
                if os.path.splitext(cand)[1].lower() in AUDIO:
                    hallado = (kr[0], kr[1], st.st_mtime_ns, st.st_size)
                    break
            if hallado is None and intentos < 12 and not _RE_URL.match(e):
                intentos += 1                            # aprender el prefijo: probar la cola de la ruta en cada raíz
                comps = [p for p in re.split(r"[\\/]", e) if p and not re.fullmatch(r"[A-Za-z]:", p) and p not in (".", "..")]
                for n in range(1, min(len(comps), 9)):
                    for raiz in raices:
                        cand = os.path.join(raiz, *comps[n:])
                        if os.path.isfile(cand):
                            trad.append((n, raiz))
                            kr = self._clave_rel(cand)
                            if kr:
                                p = idx.pista_por_ruta(*kr)
                                st = None if p else os.stat(cand)
                                hallado = (p["key"], p["rel"], p["mt"], p["tam"]) if p else \
                                    (kr[0], kr[1], st.st_mtime_ns, st.st_size)
                            break
                    if hallado:
                        break
            if hallado and hallado[:2] not in vistos:
                vistos.add(hallado[:2])
                out.append(hallado)
        return out

    def pistas(self, key, rel):
        """Pistas resueltas de una playlist (cacheadas mientras no cambien el archivo ni el índice)."""
        ruta = self.app.bib.resolver(key, rel)
        if not ruta or os.path.splitext(ruta)[1].lower() not in PLAYLISTS:
            return None
        try:
            st = os.stat(ruta)
        except OSError:
            return None
        ck = (ruta, st.st_mtime_ns, st.st_size, self.app.indice.gen)
        with self.lock:
            c = self.cache.get(ck)
            if c is not None:
                self.cache.move_to_end(ck)
                return c
        t0 = time.monotonic()
        res = self._resolver(ruta)
        log.info("Playlist %s: %d tracks (%.0f ms)", os.path.basename(ruta), len(res), (time.monotonic() - t0) * 1000)
        with self.lock:
            c = self.reg.get(ruta)
            if c and c[:2] == (st.st_mtime_ns, st.st_size) and c[2] != len(res):
                self.reg[ruta] = (c[0], c[1], len(res))  # el conteo real (sin pistas que ya no existen)
                self.gen += 1
                with self.app.tags.lock:
                    self.app.tags.db.execute("UPDATE playlists SET n=? WHERE ruta=?", (len(res), ruta))
            self.cache[ck] = res
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
        return res

    # ---- navegación
    def _grupos(self, key, rel, pl):
        """Por álbum, en el orden en que aparecen: OrderedDict(grupo -> (álbum del índice o None, [pistas]))."""
        idx = self.app.indice
        g = OrderedDict()
        for t in pl:
            a = idx.album_de(t[0], t[1])
            gid = a["id"] if a else "f" + _hid(t[0], t[1].rsplit("/", 1)[0] if "/" in t[1] else "")
            g.setdefault(gid, (a, []))[1].append(t)
        return g

    def _c_grupo(self, gid, a, ts, key, rel):
        idx = self.app.indice
        oid = f"v:plal:{gid}:{key}:{rel}"
        if a:
            return ("v", oid, idx.na(a, len(ts)), len(ts), idx._art_album(a), C_ALBUM, idx.artista_album(a),
                    a.get("fecha") or a["anio"] or None)
        k, r = ts[0][0], ts[0][1]
        carpeta = r.rsplit("/", 1)[0] if "/" in r else ""
        nombre = carpeta.rsplit("/", 1)[-1] or self.app.bib.raices.get(k, ("", "?"))[1]
        q = quote(carpeta, safe="/")
        return ("v", oid, idx.nc(nombre, len(ts)), len(ts), f"/art/a/{k}/{q}{'/' if q else ''}cover.jpg",
                C_ALBUM, None, None)

    def descriptor(self, key, rel, n=None):
        if n is None:
            n = (self.reg.get(self.app.bib.resolver(key, rel) or "") or (0, 0, 0))[2]
        nombre = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
        return ("v", f"v:pl:{key}:{rel}", self.app.indice.nc(nombre, n), n + 1 if n else 0,
                f"/art/p/{key}/{quote(rel, safe='/')}.jpg", C_PLAYLIST, None, None)

    @staticmethod
    def partir(oid):
        """-> (tipo, grupo, raíz, rel) o None."""
        m = re.fullmatch(r"v:(pl|pla):([0-9a-f]{8}):(.+)", oid, re.S)
        if m:
            return m.group(1), None, m.group(2), m.group(3)
        m = re.fullmatch(r"v:plal:(f?[0-9a-f]{10}):([0-9a-f]{8}):(.+)", oid, re.S)
        if m:
            return "plal", m.group(1), m.group(2), m.group(3)
        return None

    def hijos(self, oid):
        if oid == "v:playlists":
            return [self.descriptor(k, r, n) for k, r, _, n in self.todas()]
        p = self.partir(oid)
        if not p:
            return None
        tipo, gid, key, rel = p
        pl = self.pistas(key, rel)
        if pl is None:
            return None
        if tipo == "pl":                                 # "» Por álbum" + las pistas en orden (Play = la playlist)
            out = [("t",) + t for t in pl]
            if pl:                                       # siempre (también con un solo álbum): childCount exacto
                g = self._grupos(key, rel, pl)           # sin tener que resolver cada playlist al listarlas
                out.insert(0, ("v", f"v:pla:{key}:{rel}", T("por_album", n=f"{len(g):,}"), len(g),
                               f"/art/p/{key}/{quote(rel, safe='/')}.jpg", C_PLAYLIST, None, None))
            return out
        g = self._grupos(key, rel, pl)
        if tipo == "pla":
            return [self._c_grupo(k, a, ts, key, rel) for k, (a, ts) in g.items()]
        if gid in g:
            return [("t",) + t for t in g[gid][1]]
        return None

    def metadata(self, oid):
        if oid == "v:playlists":
            return ("v", oid, VISTAS["playlists"], len(self.todas()), None, "object.container.storageFolder",
                    None, None), "0"
        p = self.partir(oid)
        if not p:
            return None, None
        tipo, gid, key, rel = p
        padre = f"v:pl:{key}:{rel}"
        if tipo == "pl":
            pl = self.pistas(key, rel)
            if pl is None:
                return None, None
            return self.descriptor(key, rel, len(pl)), "v:playlists"
        if tipo == "pla":
            return next((h for h in self.hijos(padre) or [] if h[0] == "v" and h[1] == oid), None), padre
        g = self.hijos(f"v:pla:{key}:{rel}") or []
        return next((h for h in g if h[1] == oid), None), f"v:pla:{key}:{rel}"

    def titulo(self, oid):
        p = self.partir(oid)
        if not p:
            return VISTAS.get("playlists", oid)
        return f"{VISTAS.get('playlists', 'Playlists')} / {os.path.splitext(p[3].rsplit('/', 1)[-1])[0]}"

    def fuente_portada(self, key, rel):
        """Portada de una playlist: imagen con su mismo nombre (Favoritos.m3u8 -> Favoritos.jpg) o la de su
        primera pista."""
        ruta = self.app.bib.resolver(key, rel)
        if not ruta:
            return None
        base = os.path.splitext(ruta)[0]
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".JPG", ".JPEG", ".PNG", ".WEBP"):
            if os.path.isfile(base + ext):
                return ("img", base + ext)
        pl = self.pistas(key, rel) or []
        if pl:
            r = self.app.bib.resolver(pl[0][0], pl[0][1])
            return self.app.bib.fuente_portada_pista(r) if r else None
        return None



class Biblioteca:
    """Árbol de carpetas en vivo. IDs de objeto estables y sin estado:
         d<raiz>:<ruta/relativa>   carpeta
         t<raiz>:<ruta/relativa>   pista
       <raiz> = hash corto de la ruta de la carpeta configurada."""

    def __init__(self, app):
        self.app = app
        self.dir_cache = OrderedDict()
        self.dir_lock = threading.Lock()
        self.art_src_cache = OrderedDict()
        self.configurar(app.cfg)

    def configurar(self, cfg):
        self.raices = OrderedDict()
        self.sin_indexar = set()                        # claves de raíz que no entran en las vistas por tags
        fuera = set(cfg["musica"].get("sin_indexar") or ())
        for c in cfg["musica"]["carpetas"]:
            nombre = os.path.basename(c.rstrip("\\/")) or c.rstrip("\\/") or c
            key = clave_raiz(c, self.raices)
            self.raices[key] = (os.path.abspath(c), nombre)
            if c in fuera:
                self.sin_indexar.add(key)
        self.ignorar = [p.casefold() for p in cfg["musica"]["ignorar"]]
        self.limite_tags = cfg["musica"]["ordenar_por_tags_hasta"]
        with self.dir_lock:
            self.dir_cache.clear()
            self.art_src_cache.clear()

    def raices_disponibles(self):
        return [(k, r, n) for k, (r, n) in self.raices.items() if os.path.isdir(r)]

    # ---- rutas
    def clave_rel(self, ruta):
        """Ruta absoluta -> (raíz, rel) si está dentro de una carpeta de música (o None)."""
        nc = os.path.normcase(ruta)
        for key, (raiz, _) in self.raices.items():
            pre = os.path.normcase(raiz.rstrip("\\/") + os.sep)
            if nc.startswith(pre):
                return key, ruta[len(pre):].replace(os.sep, "/")
        return None

    def resolver(self, key, rel):
        raiz = self.raices.get(key)
        if raiz is None:
            return None
        partes = [p for p in rel.split("/") if p]
        # ".." nunca (no salir de la carpeta de música). En Windows tampoco ":" ni "\\" (C:, rutas mezcladas);
        # en Linux/macOS son caracteres válidos en un nombre ("Hi-Res Masters: '80s Rock", "AC\\DC").
        if any(p in (".", "..") or "\x00" in p or (ES_WINDOWS and (":" in p or "\\" in p)) for p in partes):
            return None
        return os.path.join(raiz[0], *partes) if partes else raiz[0]

    @staticmethod
    def padre_rel(rel):
        return rel.rsplit("/", 1)[0] if "/" in rel else ""

    def _ignorado(self, nombre):
        n = nombre.casefold()
        return any(fnmatch.fnmatchcase(n, p) for p in self.ignorar)

    TTL_LISTADO = 20          # s; en Windows la fecha de la carpeta NO es fiable (mover carpetas adentro no la cambia)
    TTL_PORTADA = 300

    def listar(self, ruta, fresco=False, ttl=TTL_LISTADO, validar=True):
        """(carpetas, pistas[(nombre, mtime_ns, tam)], imagenes).
        fresco=True (la carpeta que se está navegando): siempre se lee del disco.
        Si no, se usa la caché solo si la fecha de la carpeta no cambió Y tiene menos de TTL_LISTADO segundos.
        Si el listado nuevo difiere del cacheado, se avisa a los controles (SystemUpdateID)."""
        if not ruta:                                     # ruta que no se pudo resolver: nada que listar
            return None
        if not validar and not fresco:                   # solo para números (childCount/conteos): sin tocar el disco
            with self.dir_lock:
                c = self.dir_cache.get(ruta)
                if c:
                    return c[2]
        try:
            mt = os.stat(ruta).st_mtime_ns
        except OSError:
            return None
        ahora = time.monotonic()
        with self.dir_lock:
            c = self.dir_cache.get(ruta)
            if c and not fresco and c[0] == mt and (ttl is None or ahora - c[1] < ttl):
                self.dir_cache.move_to_end(ruta)
                return c[2]
        dirs, pistas, imgs, pls = [], [], [], []
        try:
            with os.scandir(ruta) as it:
                for e in it:
                    if self._ignorado(e.name):
                        continue
                    try:
                        st = e.stat()
                        if ES_WINDOWS and getattr(st, "st_file_attributes", 0) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                            continue
                        if e.is_dir():
                            dirs.append(e.name)
                            continue
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in AUDIO:
                            pistas.append((e.name, st.st_mtime_ns, st.st_size))
                        elif ext in IMAGENES:
                            imgs.append(e.name)
                        elif ext in PLAYLISTS:
                            pls.append((e.name, st.st_mtime_ns, st.st_size))
                    except OSError:
                        continue
        except OSError as ex:
            log.warning("Could not list %s: %s", ruta, ex)
            return None
        dirs.sort(key=natkey)
        pistas.sort(key=lambda x: natkey(x[0]))
        pls.sort(key=lambda x: natkey(x[0]))
        res = (dirs, pistas, imgs, pls)
        reg = getattr(self.app, "playlists", None)
        if reg is not None:
            try:
                reg.vistas_en(ruta, pls)                 # registro de playlists (vista "Playlists")
            except Exception:
                log.exception("Playlist registry")
        with self.dir_lock:
            previo = self.dir_cache.get(ruta)
            self.dir_cache[ruta] = (mt, ahora, res)
            self.dir_cache.move_to_end(ruta)
            while len(self.dir_cache) > 8192:
                self.dir_cache.popitem(last=False)
        if previo and (previo[2][0] != dirs or [p[0] for p in previo[2][1]] != [p[0] for p in pistas]):
            log.info("Changes detected in %s", ruta)
            self.app.cambio_detectado()
        return res

    def meta_ruta(self, key, rel):
        """Metadatos deducidos de 'Artista/[Año] Álbum/NN. Título.ext' mientras se leen los tags reales."""
        partes = rel.split("/")
        nombre = os.path.splitext(partes[-1])[0]
        m = {}
        mm = re.match(r"^(?:(\d{1,2})-)?(\d{1,3})\s*[.\-_)]\s*(.+)$", nombre)
        if mm:
            m["disc"] = int(mm.group(1) or 0)
            m["track"] = int(mm.group(2))
            nombre = mm.group(3)
        elif " - " in nombre and len(partes) >= 1:
            art, _, tit = nombre.partition(" - ")
            m["artist"], nombre = art.strip(), tit
        m["title"] = nombre.strip()
        if len(partes) >= 2:
            alb = partes[-2]
            y = re.match(r"^\s*[\[(]?(\d{4})[\])]?\s*[-.]?\s*(.*)$", alb)
            if y and y.group(2):
                m["year"], alb = int(y.group(1)), y.group(2)
            m["album"] = alb
            if len(partes) >= 3 and "artist" not in m:
                m["artist"] = partes[-3]
        return {k: v for k, v in m.items() if v}

    def precargar_subcarpetas(self, ruta, dirs):
        """Al ver un artista, deja listos en caché los tags de sus álbumes (solo un nivel)."""
        archivos = []
        for d in dirs[:60]:
            l = self.listar(os.path.join(ruta, d), ttl=None)
            if l:
                archivos += [(os.path.join(ruta, d, n), mt, sz) for n, mt, sz in l[1]]
            if len(archivos) >= 600:
                break
        if archivos:
            self.app.tags.precargar(archivos[:600])

    def modo_album(self, ruta):
        """True si las pistas de la carpeta son de un solo álbum: entonces comparten la portada de la carpeta
        (una imagen por álbum en vez de una por pista). Carpetas mixtas (playlists) usan la de cada pista."""
        ck = ("album", ruta)                             # decisión cacheada 5 min: sin tocar el disco por cada pista
        with self.dir_lock:
            c = self.art_src_cache.get(ck)
            if c and time.monotonic() - c[0] < self.TTL_PORTADA:
                return c[1]
        l = self.listar(ruta, ttl=None, validar=False)
        if not l or len(l[1]) < 2:
            return True
        muestra = l[1][:60]
        metas, faltan = self.app.tags._en_cache([(os.path.join(ruta, n), m, s) for n, m, s in muestra])
        # un álbum vacío cuenta como distinto: "Pop Electronica" con 8 .m4a sin álbum + 1 FLAC con álbum NO es un álbum
        albums = {" ".join((m.get("album") or "").split()).casefold() for m in metas.values()}
        if len(albums) > 1:
            res = False
        elif albums == {""}:                             # sin tags de álbum: si traen portada propia, cada una la suya
            res = not any(m.get("art") for m in metas.values())
        else:
            res = True
        with self.dir_lock:                              # sin todos los tags: decisión provisional, 5 s
            self.art_src_cache[ck] = (time.monotonic() - (0 if not faltan else self.TTL_PORTADA - 5), res)
        return res

    def conteo(self, ruta):
        l = self.listar(ruta, ttl=None, validar=False)   # solo para childCount: un número viejo no hace daño
        return len(l[0]) + len(l[1]) + len(l[3]) if l else 0

    # ---- navegación
    # ---- caché de listas para la paginación: el WiiM pide una lista larga en páginas de 50–100, y cada página
    #      volvía a calcular la lista completa. Carpetas: 4 s (siguen siendo "en vivo"); vistas: hasta que cambie
    #      el índice o los ajustes. Vistas que cambian solas (tocadas, aleatorio): 4 s.
    TTL_HIJOS_CARPETA, TTL_HIJOS_VISTA = 4.0, 300.0
    VOLATILES = ("v:tocadas", "v:mas_tocadas", "v:aleatorio", "v:indexando", "v:pl:", "v:pla:", "v:plal:")

    def hijos(self, oid):
        if not hasattr(self, "_hijos_cache"):
            self._hijos_cache = OrderedDict()
        gen = (self.app.indice.gen, getattr(self.app, "cfg_gen", 0), self.app.update_id, self.app.playlists.gen)
        ahora = time.monotonic()
        vista = oid.startswith("v:") and not oid.startswith(self.VOLATILES) and not oid.startswith("v:carpeta")
        ttl = self.TTL_HIJOS_VISTA if vista else self.TTL_HIJOS_CARPETA
        c = self._hijos_cache.get(oid)
        if c and c[1] == gen and ahora - c[0] < ttl:
            return c[2]
        res = self._hijos_calc(oid)
        if res is not None and not (oid.startswith("v:") and self.app.indice.d is None):
            with self.dir_lock:
                self._hijos_cache[oid] = (ahora, gen, res)
                self._hijos_cache.move_to_end(oid)
                while len(self._hijos_cache) > 128:
                    self._hijos_cache.popitem(last=False)
        return res

    def _hijos_calc(self, oid):
        """Lista de descriptores ('raiz', key) | ('d', key, rel) | ('t', key, rel, mtime, tam) o None si no existe."""
        if oid == "0":
            base = self.hijos_raiz()
            if base and self.app.cfg["musica"]["agrupar_carpetas"]:          # todas las carpetas bajo "Carpetas"
                base = [("v", "v:carpetas", T("carpetas"), len(base), None, "object.container.storageFolder", None, None)]
            return base + self.app.indice.raiz()
        if oid == "v:carpetas":
            return self.hijos_raiz()
        if oid == "v:playlists" or oid.startswith(("v:pl:", "v:pla:", "v:plal:")):
            return self.app.playlists.hijos(oid)
        if oid.startswith("v:"):
            return self.app.indice.hijos(oid)
        m = re.fullmatch(r"d([0-9a-f]{8}):(.*)", oid, re.S)
        if not m:
            return None
        key, rel = m.group(1), m.group(2)
        ruta = self.resolver(key, rel)
        l = self.listar(ruta, fresco=True) if ruta else None      # on demand: lo que hay en disco AHORA
        if l is None:
            return None
        pre = f"{rel}/" if rel else ""
        out = [("d", key, pre + d) for d in l[0]]
        out += [self.app.playlists.descriptor(key, pre + n) for n, _, _ in l[3]]
        pistas = list(l[1])
        if l[0]:
            self.precargar_subcarpetas(ruta, l[0])
        if pistas and len(pistas) <= self.limite_tags:
            metas = self.app.tags.obtener([(os.path.join(ruta, n), mt, sz) for n, mt, sz in pistas], espera=0.8)

            def orden(p):
                mm = metas.get(os.path.join(ruta, p[0])) or self.meta_ruta(key, pre + p[0])
                tr = _int(mm.get("track", 0))
                return (0 if tr else 1, _int(mm.get("disc", 0)), tr, natkey(p[0]))
            try:
                pistas.sort(key=orden)
            except Exception:
                log.exception("Sorting %s by tags failed; using file names", ruta)
        out += [("t", key, pre + n, mt, sz) for n, mt, sz in pistas]
        if l[0]:                                                  # carpeta con subcarpetas: "▶ Todas las pistas"
            n, completo = self.contar_vivo(ruta)                  # del disco, con presupuesto de tiempo
            if not completo:
                n = max(n, self.app.indice.conteo_carpeta(key, rel))
            if n:
                out.insert(0, self.app.indice._entrada_todas(f"v:carpeta:{key}:{rel}", n))
        return out

    # ---- "▶ Todas las pistas" de una carpeta: siempre del disco (no depende del índice de tags)
    def contar_vivo(self, ruta, t_max=0.15):
        """Pistas debajo de una carpeta contando en el disco (listados en caché si la carpeta no cambió).
        -> (n, completo). Si se acaba el tiempo, completo=False y el llamador usa también el índice.
        Se recuerda 5 min (1 min si quedó incompleto): volver a la carpeta no lo repite. La LISTA de
        "Todas las pistas" sí se lee en vivo al abrirla, así que un número algo atrasado no hace daño."""
        ck = ("cuenta", ruta)
        c = self.art_src_cache.get(ck)
        if c and time.monotonic() - c[0] < (300 if c[1][1] else 60):
            return c[1]
        t0, n, pila, completo = time.monotonic(), 0, [ruta], True
        while pila:
            if time.monotonic() - t0 > t_max:
                completo = False
                break
            actual = pila.pop()
            l = self.listar(actual, ttl=None)
            if l:
                n += len(l[1])
                pila.extend(os.path.join(actual, dd) for dd in l[0])
        with self.dir_lock:
            self.art_src_cache[ck] = (time.monotonic(), (n, completo))
        return n, completo

    def contar_en_cache(self, ruta, prof=0):
        """Pistas debajo de una carpeta según los listados que ya están en memoria (0 si no se ha listado).
        Solo lectura de diccionarios (sin disco); el total de cada carpeta se recuerda 1 min."""
        if prof == 0:
            c = self.art_src_cache.get(("encache", ruta))
            if c and time.monotonic() - c[0] < 60:
                return c[1]
        c = self.dir_cache.get(ruta)
        n = 0
        if c:
            dirs, pistas = c[2][0], c[2][1]
            n = len(pistas)
            if prof < 8 and dirs:
                sep = "" if ruta.endswith(os.sep) else os.sep
                for dd in dirs:
                    n += self.contar_en_cache(ruta + sep + dd, prof + 1)
        if prof == 0:
            with self.dir_lock:
                self.art_src_cache[("encache", ruta)] = (time.monotonic(), n)
        return n

    def pistas_vivas(self, key, rel, limite, t_max=4.0):
        """[(rel, mtime, tam)] debajo de una carpeta, en orden de navegación: las pistas de cada carpeta
        (por disco/número si los tags ya están en caché, si no por nombre) y luego sus subcarpetas en orden natural."""
        raiz = self.resolver(key, rel)
        if not raiz:
            return []
        t0, out, pila = time.monotonic(), [], [(raiz, rel)]
        while pila and len(out) < limite and time.monotonic() - t0 < t_max:
            ruta, r = pila.pop()
            l = self.listar(ruta, ttl=None)
            if not l:
                continue
            dirs, pistas = l[0], l[1]
            pre = f"{r}/" if r else ""
            if pistas:
                metas, _ = self.app.tags._en_cache([(os.path.join(ruta, n), m, z) for n, m, z in pistas])

                def orden(x):
                    mm = metas.get(os.path.join(ruta, x[0])) or {}
                    tr = _int(mm.get("track", 0))
                    return (0 if tr else 1, _int(mm.get("disc", 0)), tr, natkey(x[0]))
                out += [(pre + n, m, z) for n, m, z in sorted(pistas, key=orden)]
            pila.extend((os.path.join(ruta, dd), pre + dd) for dd in reversed(dirs))
        return out[:limite]

    def hijos_raiz(self):
        """Carpetas configuradas; con una sola, directamente su contenido."""
        raices = self.raices_disponibles()
        if len(raices) == 1:
            return self.hijos(f"d{raices[0][0]}:") or []
        return [("raiz", k) for k, _, _ in raices]

    def padre_raices(self):
        return "v:carpetas" if self.app.cfg["musica"]["agrupar_carpetas"] else "0"

    def metadata(self, oid):
        """(descriptor, parentID) para BrowseMetadata."""
        if oid == "0":
            return ("cero",), "-1"
        if oid == "v:carpetas":
            return ("v", oid, T("carpetas"), len(self.hijos_raiz()), None, "object.container.storageFolder",
                    None, None), "0"
        if oid == "v:playlists" or oid.startswith(("v:pl:", "v:pla:", "v:plal:")):
            return self.app.playlists.metadata(oid)
        if oid.startswith("v:"):
            return self.app.indice.metadata(oid)
        m = re.fullmatch(r"([dt])([0-9a-f]{8}):(.*)", oid, re.S)
        if not m:
            return None, None
        tipo, key, rel = m.groups()
        ruta = self.resolver(key, rel)
        if not ruta or not (os.path.isdir(ruta) if tipo == "d" else os.path.isfile(ruta)):
            return None, None
        unica = len(self.raices_disponibles()) == 1
        if tipo == "d" and rel == "":
            return ("raiz", key), self.padre_raices()
        prel = self.padre_rel(rel)
        parent = self.padre_raices() if (prel == "" and unica) else f"d{key}:{prel}"
        if tipo == "d":
            return ("d", key, rel), parent
        st = os.stat(ruta)
        return ("t", key, rel, st.st_mtime_ns, st.st_size), parent

    # ---- DIDL
    def oid(self, desc):
        if desc[0] == "cero":
            return "0"
        if desc[0] == "raiz":
            return f"d{desc[1]}:"
        return f"{desc[0]}{desc[1]}:{desc[2]}"

    def render_lista(self, descs, parent, base):
        """Renderiza varios descriptores; lee los tags de las pistas en un solo lote."""
        pistas = [d for d in descs if d[0] == "t"]
        metas = {}
        if pistas:
            rutas = [(self.resolver(d[1], d[2]), d[3], d[4]) for d in pistas]
            metas = self.app.tags.obtener(rutas, espera=0.4)
        out = []
        for d in descs:
            try:                                         # un archivo o carpeta problemático no tumba la página entera
                if d[0] == "t":
                    mm = metas.get(self.resolver(d[1], d[2])) or self.meta_ruta(d[1], d[2])
                    out.append(self._item(d, parent, base, mm))
                else:
                    out.append(self._contenedor(d, parent, base))
            except Exception:
                log.exception("Skipping %r while rendering %r", d[:3], parent)
        return out

    def _contenedor(self, d, parent, base):
        if d[0] == "v":                                    # ("v", oid, titulo, n, art, clase, artista, anio)
            _, oid, titulo, n, art, clase, artista, anio = d
            if art:
                art = base + art
            return self._xml_cont(oid, parent, titulo, n, art, clase, artista, anio)
        if d[0] == "cero":
            n = len(self.hijos("0") or [])
            return self._xml_cont("0", "-1", "root", n, None)
        key = d[1]
        rel = "" if d[0] == "raiz" else d[2]
        ruta = self.resolver(key, rel)
        titulo = self.raices[key][1] if d[0] == "raiz" else rel.rsplit("/", 1)[-1]
        l = self.listar(ruta, ttl=None, validar=False)   # un solo listado para childCount y el conteo del título
        # el mayor entre el índice y lo que ya se listó del disco (memoria, sin tocar el disco)
        n = max(self.app.indice.conteo_carpeta(key, rel), self.contar_en_cache(ruta) if ruta else 0)
        hijos = (len(l[0]) + len(l[1]) + len(l[3])) if l else 0
        titulo = self.app.indice.nc(titulo, n, hijos)
        if l and l[0] and n:
            hijos += 1                                     # "▶ Todas las pistas"
        qrel = quote(rel, safe="/")
        art = f"{base}/art/d/{key}/{qrel}{'/' if qrel else ''}cover.jpg"
        return self._xml_cont(self.oid(d), parent, titulo, hijos, art)

    def _xml_cont(self, oid, parent, titulo, n, art, clase="object.container.storageFolder", artista=None, anio=None):
        s = (f'<container id="{esc(oid)}" parentID="{esc(parent)}" restricted="1" searchable="0" childCount="{n}">'
             f"<dc:title>{esc(titulo)}</dc:title><upnp:class>{clase}</upnp:class>")
        if artista:
            artista = mostrar_artistas(artista)
            s += f"<upnp:artist>{esc(artista)}</upnp:artist><dc:creator>{esc(artista)}</dc:creator>"
        if anio:
            s += f"<dc:date>{fecha_dc(anio)}</dc:date>"
        if art:
            s += f"<upnp:albumArtURI{self.app.art_profile}>{esc(art)}</upnp:albumArtURI>"
        return s + "</container>"

    def _item(self, d, parent, base, mm):
        _, key, rel, mt, tam = d
        nombre = rel.rsplit("/", 1)[-1]
        ext = os.path.splitext(nombre)[1].lower()
        mime = AUDIO.get(ext, "application/octet-stream")
        prel = self.padre_rel(rel)
        carpeta = prel.rsplit("/", 1)[-1] if prel else self.raices[key][1]
        v = self.app.cfg["vistas"]
        x = mm.get("x")
        titulo = mm.get("title") or os.path.splitext(nombre)[0]
        artista = mostrar_artistas(tag_mostrado(x, lista_tags(v.get("tags_artista")))
                                   or mm.get("artist") or mm.get("aartist") or "")
        aartista = tag_mostrado(x, lista_tags(v.get("tags_artista_album"))) or mm.get("aartist")
        album = mm.get("album") or carpeta
        if v.get("album_en_pistas") and mm.get("album"):
            album = self.app.indice.titulo_album_pista(key, rel) or album
        num = mm.get("track")
        if num and v.get("numeracion_seguida"):                # varios discos: 1…N en todo el álbum
            num = self.app.indice.numero_seguido(key, rel) or num
        tp = v["plantillas"].get("pista") or _PISTA_DEFECTO
        if tp != _PISTA_DEFECTO:
            campos = {"titulo": titulo, "nombre": titulo, "numero": num or "", "disco": mm.get("disc") or "",
                      "artista": artista, "album": mm.get("album") or "", "año": mm.get("year") or ""}
            titulo = plantilla(tp, campos, (lambda k: (x or {}).get(k, "")))
        qrel = quote(rel, safe="/")
        art = self.url_portada_pista(base, key, rel, prel, qrel, mm, mt, tam)
        s = (f'<item id="t{key}:{esc(rel)}" parentID="{esc(parent)}" restricted="1">'
             f"<dc:title>{esc(titulo)}</dc:title><upnp:class>object.item.audioItem.musicTrack</upnp:class>")
        if artista:
            s += f"<dc:creator>{esc(artista)}</dc:creator><upnp:artist>{esc(artista)}</upnp:artist>"
        if aartista:
            s += f'<upnp:artist role="AlbumArtist">{esc(mostrar_artistas(aartista))}</upnp:artist>'
        s += f"<upnp:album>{esc(album)}</upnp:album>"
        if mm.get("genre"):
            s += f"<upnp:genre>{esc(mm['genre'])}</upnp:genre>"
        if num:
            s += f"<upnp:originalTrackNumber>{num}</upnp:originalTrackNumber>"
        if mm.get("fecha") or mm.get("year"):                # fecha de ESTA edición; si no hay, el año
            s += f"<dc:date>{fecha_dc(mm.get('fecha') or mm['year'])}</dc:date>"
        if art:
            s += f"<upnp:albumArtURI{self.app.art_profile}>{esc(art)}</upnp:albumArtURI>"
        attrs =f' protocolInfo="{protocol_info(mime)}" size="{tam}"'
        if mm.get("dur"):
            dd = mm["dur"]
            attrs += f' duration="{int(dd // 3600)}:{int(dd % 3600 // 60):02d}:{dd % 60:06.3f}"'
        if mm.get("br"):
            attrs += f' bitrate="{mm["br"] // 8}"'
        if mm.get("sr"):
            attrs += f' sampleFrequency="{mm["sr"]}"'
        if mm.get("ch"):
            attrs += f' nrAudioChannels="{mm["ch"]}"'
        if mm.get("bits"):
            attrs += f' bitsPerSample="{mm["bits"]}"'
        s += f'<res{attrs}>{esc(base)}/m/{key}/{esc(qrel)}</res>'
        return s + "</item>"

    # ---- portadas
    def url_portada_pista(self, base, key, rel, prel, qrel, mm, mt=None, tam=None):
        """URL de la portada de una pista, sin leer imágenes (decide con los tags en caché):
        - con portada embebida (y "una portada por pista" activo, o carpeta mixta): la suya (/art/t/…)
        - carpeta de un álbum: la del álbum (/art/a/…, una sola imagen para todas las pistas)
        - carpeta mixta sin portada embebida: la imagen de la carpeta si hay; nunca la de otra pista."""
        carpeta = os.path.dirname(self.resolver(key, rel))
        en_album = self.modo_album(carpeta)
        propia = bool(tipos_imagen(mm)) or bool((mm.get("x") or {}).get("discsubtitle"))
        if propia and (self.app.cfg["portadas"]["por_pista"] or not en_album):
            if mt is not None and 3 in tipos_imagen(mm):
                # imagen ya procesada: URL por CONTENIDO, la misma para todas las pistas que la comparten, así el
                # reproductor la descarga una vez y las filas que aparecen al deslizar la toman de su caché
                h = self.app._mapa(self.app.clave_fuente("emb", os.path.join(carpeta, rel.rsplit("/", 1)[-1]), mt, tam))
                if h and self.app.portada_existe(h):
                    return f"{base}/art/h/{h}.jpg"
            return f"{base}/art/t/{key}/{qrel}.jpg"
        if en_album:
            qp = quote(prel, safe="/")                          # portada de su carpeta, sin buscar en subcarpetas
            return f"{base}/art/a/{key}/{qp}{'/' if qp else ''}cover.jpg"
        return f"{base}/art/t/{key}/{qrel}.jpg" if self.tiene_imagenes(carpeta) else None

    def fuente_portada_pista(self, ruta, meta=None):
        """Portada de una pista: embebida frontal › <subtítulo del disco>.jpg › embebida (disco, otra…)
        › imagen de la carpeta (o, si la carpeta es un álbum, su portada) › otras embebidas (contraportada…)."""
        if not os.path.isfile(ruta):
            return None
        if meta is None:
            try:
                st = os.stat(ruta)
                meta = self.app.tags.obtener([(ruta, st.st_mtime_ns, st.st_size)], espera=2).get(ruta, {})
            except OSError:
                meta = {}
        tipos = tipos_imagen(meta)
        if 3 in tipos:
            return ("emb", ruta)
        carpeta = os.path.dirname(ruta)
        ds = (meta.get("x") or {}).get("discsubtitle")
        if ds:
            img = self._imagen_llamada(carpeta, {_norm(x) for x in _multi(ds)})
            if img:
                return ("img", img)
        if any(t in (6, 0, 18) for t in tipos):
            return ("emb", ruta)
        src = self.fuente_portada_carpeta(carpeta, max_prof=0, embebidas=self.modo_album(carpeta))
        if src:
            return src
        return ("emb", ruta) if tipos else None

    def _imagen_llamada(self, carpeta, nombres):
        l = self.listar(carpeta, ttl=None)
        for n in (l[2] if l else ()):
            if _norm(os.path.splitext(n)[0]) in nombres:
                return os.path.join(carpeta, n)
        return None

    @staticmethod
    def _puntaje_imagen(n):
        stem = os.path.splitext(n)[0].strip().casefold()
        if stem in PORTADA_PREF:
            return (0, PORTADA_PREF.index(stem), natkey(n))
        malo = stem.startswith(NO_PORTADA)
        return (2 if malo else 1, 0, natkey(n))

    def fuente_portada_carpeta(self, ruta, prof=0, max_prof=None, embebidas=True):
        """Portada de una carpeta/álbum: cover/folder/front.jpg › otra imagen › portada embebida de sus pistas
        › imágenes tipo contraportada/CD › (opcional) la primera que haya en sus subcarpetas.
        embebidas=False: sin usar la portada de otra pista (carpetas mixtas: cada pista tiene la suya)."""
        if max_prof is None:
            max_prof = 2 if self.app.cfg["portadas"]["subcarpetas"] else 0
        try:
            mt = os.stat(ruta).st_mtime_ns
        except OSError:
            return None
        ck = (ruta, mt, max_prof, embebidas)
        with self.dir_lock:
            c = self.art_src_cache.get(ck)
            if c and time.monotonic() - c[0] < self.TTL_PORTADA:
                return c[1]
        reciente = time.time() - mt / 1e9 < 600         # copiando/descargando: archivos que aún cambian
        l = self.listar(ruta, ttl=None, fresco=reciente)
        src = None
        if l:
            dirs, pistas, imgs = l[:3]
            orden = sorted(imgs, key=self._puntaje_imagen)
            if orden and self._puntaje_imagen(orden[0])[0] < 2:
                src = ("img", os.path.join(ruta, orden[0]))
            if not src and pistas and embebidas:
                metas = self.app.tags.obtener([(os.path.join(ruta, n), m, s) for n, m, s in pistas[:4]])
                hit = next((r for r, mm in sorted(metas.items()) if mm.get("art")), None)
                if not hit and len(pistas) > 4:            # las primeras sin imagen: cualquier otra ya leída que la tenga
                    resto, _ = self.app.tags._en_cache([(os.path.join(ruta, n), m, s) for n, m, s in pistas[4:]])
                    hit = next((r for r, mm in sorted(resto.items()) if mm.get("art")), None)
                if hit:
                    src = ("emb", hit)
            if not src and orden:
                src = ("img", os.path.join(ruta, orden[0]))
            if not src and prof < max_prof:
                for d in dirs[:6]:
                    src = self.fuente_portada_carpeta(os.path.join(ruta, d), prof + 1, max_prof)
                    if src:
                        break
        if src is None and reciente:
            return None                    # carpeta recién cambiada (copiando/descargando): no recordar "sin portada"
        with self.dir_lock:
            self.art_src_cache[ck] = (time.monotonic(), src)
            while len(self.art_src_cache) > 8192:
                self.art_src_cache.popitem(last=False)
        return src

    def tiene_imagenes(self, carpeta):
        l = self.listar(carpeta, ttl=None, validar=False)
        return bool(l and l[2])


def extraer_imagen(src):
    """Bytes originales de la imagen de una fuente: ("img", archivo), ("emb", pista) o ("emb_ar", pista)."""
    tipo, ruta = src
    if tipo == "img":
        with open(ruta, "rb") as fh:
            return fh.read()
    f = mutagen.File(ruta)
    pics = imagenes_embebidas(f) if f is not None else []
    return elegir_imagen(pics, PRIO_IMG_ARTISTA if tipo == "emb_ar" else PRIO_IMG_ALBUM)


def procesar_imagen(data, tamano, recortar, ruta=""):
    """Imagen original -> (mime, bytes) lista para servir: JPEG reducido a 'tamano' (sin Pillow, tal cual)."""
    if not data:
        return None
    if Image is None:
        mime = ("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else
                "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else "image/jpeg")
        return mime, data
    try:
        im = Image.open(io.BytesIO(data))
        if tamano:
            im.draft("RGB", (tamano * 2, tamano * 2))
        im = im.convert("RGB")
        w, h = im.size
        if recortar and max(w, h) > 1.15 * min(w, h):          # miniaturas 16:9 de YouTube, etc.
            lado = min(w, h)
            im = im.crop(((w - lado) // 2, (h - lado) // 2, (w - lado) // 2 + lado, (h - lado) // 2 + lado))
        if tamano:
            im.thumbnail((tamano, tamano), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88, optimize=True)
        return "image/jpeg", out.getvalue()
    except Exception as e:
        log.debug("Pillow could not process the cover of %s: %s", ruta, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════ vistas por etiquetas

CLAVES_VISTAS = ("artistas", "interpretes", "albumes", "generos", "decadas", "recientes", "modificados", "tocadas", "mas_tocadas",
                 "playlists", "favoritas",
                 "hires", "aleatorio", "compositores", "directores", "sellos", "calidad")
# Vistas predefinidas hechas con el motor de vistas personalizadas (niveles de tags › álbumes › pistas)
PRESETS_X = {"compositores": {"niveles": ["composer"]}, "directores": {"niveles": ["conductor"]},
             "sellos": {"niveles": ["label"]}, "calidad": {"niveles": ["#calidad", "albumartist"]}}
# niveles calculados (no son tags del archivo) y su orden
NIVELES_CALC = ("albumartist", "artist", "genre", "year", "#decada", "#calidad", "#resolucion", "#formato", "#agregado")
NIVELES_PERSONA = {"artist", "albumartist", "composer", "conductor", "performer", "lyricist", "remixer", "arranger"}
NIVELES_NUM_DESC = {"year", "#decada", "#agregado", "#resolucion"}
VISTAS_NUEVAS = ("playlists", "favoritas")        # 2.0
_RE_TAG_FECHA = re.compile(r"date|year|time|fecha|año|anio", re.I)
ORDEN_CALIDAD = ("dsd", "hires", "cd", "lossy")
VISTAS = OrderedDict((k, k) for k in CLAVES_VISTAS)      # clave -> nombre visible (lo llena aplicar_idioma)
ARTICULOS = ("the ",)          # como MusicBee/MinimServer: "The Cure" en la C; "Los Prisioneros" se queda en la L
C_ALBUM, C_ARTISTA, C_GENERO = "object.container.album.musicAlbum", "object.container.person.musicArtist", "object.container.genre.musicGenre"


@lru_cache(maxsize=1 << 17)       # textos que se repiten en miles de pistas: se calculan una vez
def _norm(s):
    return " ".join((s or "").split()).casefold()


@lru_cache(maxsize=1 << 17)       # textos que se repiten en miles de pistas: se calculan una vez
def _sin_articulo(s):
    n = _norm(s)
    for a in ARTICULOS:
        if n.startswith(a) and len(n) > len(a):
            return n[len(a):]
    return n


@lru_cache(maxsize=1 << 17)       # textos que se repiten en miles de pistas: se calculan una vez
def _orden(s):
    """Orden natural, sin artículo y sin acentos (Ángela junto a Andrés, no después de la Z)."""
    import unicodedata
    n = unicodedata.normalize("NFKD", _sin_articulo(s))
    return natkey("".join(c for c in n if not unicodedata.combining(c)))


@lru_cache(maxsize=1 << 17)       # textos que se repiten en miles de pistas: se calculan una vez
def _letra(s):
    import unicodedata
    n = _sin_articulo(s)
    c = unicodedata.normalize("NFD", n[:1])[:1].upper() if n else "#"
    return c if c.isalpha() and c.isascii() else ("0-9" if c.isdigit() else "#")


@lru_cache(maxsize=1 << 17)       # textos que se repiten en miles de pistas: se calculan una vez
def _hid(*partes):
    return hashlib.sha1("|".join(_norm(p) for p in partes).encode("utf-8")).hexdigest()[:10]


def _multi(v, seps=";"):
    return [x.strip() for x in re.split("[" + re.escape(seps) + "]", v or "") if x.strip()]


_RE_DISCO = re.compile(r"(?i)^\s*[\[(]?\s*(cd|disc|disk|disco|disque|vol(ume)?\.?)\s*[-_.]?\s*\d+\b")


def _carpeta_album(carpeta):
    """Carpeta que representa al álbum: 'Artista/Álbum/CD2' -> 'Artista/Álbum' (discos en subcarpetas)."""
    if "/" in carpeta:
        padre, ultima = carpeta.rsplit("/", 1)
    else:
        padre, ultima = "", carpeta
    return padre if ultima and _RE_DISCO.match(ultima) else carpeta


class _Registro:
    """Registro compacto (slots) que se usa como un dict: get, [], in, keys, items. Con 100k pistas un dict por
    pista y otro por sus tags pesan ~2× más; así el resto del código no cambia."""
    __slots__ = ()

    def get(self, k, d=None):
        return getattr(self, k, d)

    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k) from None

    def __setitem__(self, k, v):
        setattr(self, k, v)

    def __contains__(self, k):
        return hasattr(self, k)

    def keys(self):
        return [k for k in self.__slots__ if hasattr(self, k)]

    def items(self):
        return [(k, getattr(self, k)) for k in self.keys()]

    def __iter__(self):
        return iter(self.keys())


CONSERVAR_TAGS = ("title", "artist", "aartist", "album", "track", "disc", "year", "genre", "bits", "sr",
                  "asort", "comp", "pt", "br", "arts", "aarts", "ry", "fecha", "of")
_TEXTOS_REPETIDOS = ("artist", "aartist", "album", "genre", "arts", "aarts", "fecha", "of")


class TagsPista(_Registro):
    """Los tags de una pista que usa el índice (solo los que tienen valor)."""
    __slots__ = CONSERVAR_TAGS + ("x",)


class Pista(_Registro):
    """Una pista del índice. vl/xk/add: para reutilizar sus tags en la próxima construcción sin releer la base."""
    __slots__ = ("key", "rel", "mt", "tam", "m", "dir", "ag", "ext", "al", "vl", "xk", "add")


_RE_OID_DISCO = re.compile(r"^((?:v:al|v:.*:al):[0-9a-f]+):d:(\d+)$")


class Indice:
    """Índice por etiquetas construido desde la caché de tags (SQLite). No lee archivos de música:
    solo comprueba que sigan existiendo. Se reconstruye en segundo plano cuando la caché cambia (máx. 1/min)."""

    def __init__(self, app):
        self.app = app
        self._busquedas, self._busq_lock = OrderedDict(), threading.Lock()   # páginas de Search
        self.gen = 0                                      # sube con cada índice publicado
        self.d = None
        self.version = -1
        self.t_build = 0.0
        self.construyendo = False
        self.lock = threading.Lock()
        self.listo = threading.Event()
        self._x_cache = OrderedDict()
        self._forzar = False

    # ---- construcción
    QUIETO_SEG = 15            # reconstruir el índice solo tras este tiempo sin navegar (o si no hay índice)

    def asegurar(self, espera=4.0):
        """Índice actual (o None mientras se construye el primero). Las reconstrucciones por cambios en la caché
        se hacen cuando nadie navega: así abrir una carpeta nunca compite con un índice armándose."""
        v = self.app.tags.version
        quieto = time.time() - self.app.ultima_peticion >= self.QUIETO_SEG
        # índice muy atrasado (instalación nueva, relectura): rehacerlo pronto; si no, como mucho una vez por minuto
        d = self.d
        atrasado = d is not None and len(d["pistas"]) < 0.5 * (self.app.tags.total() - d.get("fuera", 0))
        pausa = 5 if atrasado else 60
        with self.lock:
            lanzar = (not self.construyendo and time.monotonic() - getattr(self, "t_fallo", -1e9) > 60
                      and (self.d is None or self._forzar
                           or (v != self.version and quieto and time.monotonic() - self.t_build > pausa)))
            if lanzar:
                self.construyendo = True
                self._forzar = False
        if lanzar:
            threading.Thread(target=self._construir, daemon=True, name="indice").start()
        if self.d is None:
            self.listo.wait(espera)
        return self.d

    def invalidar(self):
        """Vistas cambiadas en el panel: reconstruir ya (sin esperar el minuto entre construcciones)."""
        with self.lock:
            self.version, self.t_build = -1, -1e9
            self._x_cache = OrderedDict()
            self._forzar = True
        self.asegurar(espera=0)

    def mantener(self):
        """Lo llama la precarga cada pocos segundos: pone el índice al día cuando el servidor está quieto."""
        if self.d is not None and self.app.tags.version != self.version:
            self.asegurar(espera=0)

    def _ceder(self, i):
        """Dentro de los bucles largos de la construcción: si alguien está navegando, soltar el GIL un momento
        para que sus peticiones pasen primero (la construcción tarda un poco más, la navegación no se nota)."""
        if not i & 511 and time.time() - self.app.ultima_peticion < 3:
            time.sleep(0.004)

    def _raiz_de(self, ruta, raices):
        rc = os.path.normcase(ruta)
        for key, pref, plen in raices:
            if rc.startswith(pref):
                return key, ruta[plen:].replace("\\", "/")
        return None

    def _construir(self):
        t0 = time.time()
        try:
            v = self.app.tags.version
            with self.app.tags.lock:                    # sin la columna meta: solo se lee la de lo que cambió
                filas = self.app.tags.db.execute("SELECT ruta, mtime, tam, v FROM pistas").fetchall()
            raices, excluidas = [], []
            for key, (r, _) in self.app.bib.raices.items():
                pref = os.path.normcase(r if r.endswith(os.sep) else r + os.sep)
                # "Indexar tags" desmarcado: se navega por carpetas, pero no entra en artistas/álbumes/búsqueda…
                (excluidas if key in self.app.bib.sin_indexar else raices).append((key, pref, len(pref)))
            # Verificar que los archivos sigan ahí: UN listado por carpeta (no un stat por archivo), y si la
            # carpeta no cambió desde la construcción anterior (misma fecha y mismos archivos), ni eso: un stat.
            por_dir = defaultdict(list)
            n_fuera = 0                                 # pistas en caché de carpetas sin indexar
            for j, fila in enumerate(filas):
                self._ceder(j)
                kr = self._raiz_de(fila[0], raices)
                if kr:
                    por_dir[os.path.dirname(fila[0])].append((fila, kr))
                elif excluidas and self._raiz_de(fila[0], excluidas):
                    n_fuera += 1
            vigentes, cambiados = [], []
            nacido = {}                                 # ruta -> fecha de creación (Windows: cuando llegó al disco)
            dirs_prev, dirs_nuevo = getattr(self, "_dirs", {}), {}
            for j, (carpeta_abs, items) in enumerate(por_dir.items()):
                self._ceder(j * 16)
                nombres = {os.path.basename(f[0]): (f[1], f[2]) for f, _ in items}
                try:
                    dmt = os.stat(carpeta_abs).st_mtime_ns
                except OSError:
                    continue                            # carpeta movida/borrada: nada de ella sigue vigente
                # carpeta igual que en la construcción anterior (misma fecha y mismos archivos en la base): todo
                # sigue vigente sin listarla. Se guarda solo una firma por carpeta, no cada archivo.
                sig = hash(frozenset(nombres.items()))
                c = dirs_prev.get(carpeta_abs)
                if c and c == (dmt, sig):
                    dirs_nuevo[carpeta_abs] = c
                    vigentes.extend(items)
                    continue
                if True:
                    ent = {}
                    try:
                        with os.scandir(carpeta_abs) as it:
                            for e in it:
                                if e.name in nombres:
                                    try:
                                        st = e.stat()
                                    except OSError:
                                        continue
                                    b = getattr(st, "st_birthtime", 0) or (st.st_ctime if ES_WINDOWS else 0)
                                    ent[e.name] = (st.st_mtime_ns, st.st_size, int(b))
                    except OSError:
                        continue
                validos = {}
                for f, kr in items:
                    n = os.path.basename(f[0])
                    e = ent.get(n)
                    if e and e[:2] == (f[1], f[2]):
                        vigentes.append((f, kr))
                        validos[n] = e[:2]
                        if e[2]:
                            nacido[f[0]] = e[2]
                    elif e:
                        # cambió en disco (tags editados): sigue visible con sus datos de antes y se relee ya,
                        # en vez de desaparecer de las vistas hasta la próxima lectura
                        vigentes.append((f, kr))
                        cambiados.append((f[0], e[0], e[1]))
                dirs_nuevo[carpeta_abs] = (dmt, hash(frozenset(validos.items())))
            self._dirs = dirs_nuevo
            vigentes.sort(key=lambda x: x[0][0])
            pistas, albumes = [], {}
            ids_alb, generos_txt = {}, {}               # memos: un hash por álbum y un split por género, no por pista
            claves_x = self.claves_x_necesarias()
            cat_x = Counter()                           # tags extra presentes en la biblioteca (para el editor)
            intern = sys.intern
            # tags ya procesados en la construcción anterior (las pistas del índice publicado): se reutilizan si el
            # archivo no cambió, sin volver a leer ni decodificar su fila de la base
            firma = (tuple(sorted(claves_x)),)
            viejo = self.d
            prev = ({(q.key, q.rel): q for q in viejo["pistas"]}
                    if viejo is not None and viejo.get("firma") == firma else {})
            faltan = [f[0] for f, kr in vigentes
                      if (lambda q: q is None or (q.mt, q.tam, q.vl) != f[1:])(prev.get(kr))]
            crudos_json = {}
            for k in range(0, len(faltan), 800):
                parte = faltan[k:k + 800]
                with self.app.tags.lock:
                    crudos_json.update(self.app.tags.db.execute(
                        f"SELECT ruta, meta FROM pistas WHERE ruta IN ({','.join('?' * len(parte))})", parte).fetchall())
            # 1) pistas + "álbum crudo" (título, artista del álbum). Se anota en qué carpetas aparece cada uno:
            #    el mismo título del mismo artista en carpetas distintas (reediciones, homónimos) son álbumes distintos.
            crudos, carpetas_de = [], defaultdict(set)
            textos = {}                                 # una sola copia de cada texto repetido (artista, álbum…)
            for j, ((ruta, mt, tam, vl), kr) in enumerate(vigentes):
                self._ceder(j)
                q = prev.get(kr)
                ag = None
                if q is not None and (q.mt, q.tam, q.vl) == (mt, tam, vl):
                    m, xk, add, rel, ag = q.m, q.xk, q.add, q.rel, q.ag   # ag: incluye la fecha de creación
                else:
                    js = crudos_json.get(ruta)
                    if js is None:
                        continue                        # borrada entre las dos consultas
                    completo = json.loads(js)
                    m = TagsPista()
                    for k in CONSERVAR_TAGS:
                        v = completo.get(k)
                        if v is not None and v != "":
                            if k in _TEXTOS_REPETIDOS:
                                v = textos.setdefault(v, v) if isinstance(v, str) else v
                            setattr(m, k, v)
                    cx = completo.get("x")
                    xk = tuple(cx) if cx else ()
                    if cx and claves_x:
                        sx = {k: intern(cx[k]) for k in claves_x if k in cx}
                        if sx:
                            m.x = sx
                    add = completo.get("add", 0)
                    rel = kr[1]
                if xk:
                    cat_x.update(xk)
                key = kr[0]
                carpeta = rel.rsplit("/", 1)[0] if "/" in rel else ""
                ag = add or nacido.get(ruta) or ag or mt // 1_000_000_000
                p = Pista()
                p.key, p.rel, p.mt, p.tam, p.m, p.vl, p.xk, p.add = key, rel, mt, tam, m, vl, xk, add
                p.dir, p.ag, p.ext = intern(carpeta), ag, intern(rel.rsplit(".", 1)[-1].lower())
                pistas.append(p)
                alb = getattr(m, "album", None) or (carpeta.rsplit("/", 1)[-1] if carpeta else self.app.bib.raices[key][1])
                aa = getattr(m, "aartist", "")
                crudo = (alb, aa if aa else f"{key}/{carpeta}")
                dalb = f"{key}/{_carpeta_album(carpeta)}"
                crudos.append((crudo, dalb))
                carpetas_de[crudo].add(dalb)
            # 1b) pistas sueltas (carpetas con varios álbumes: playlists, descargas) se unen al álbum "de verdad"
            #     con el mismo título y artista, en vez de salir como otro "21" repetido. Dos carpetas que SON
            #     álbumes completos con el mismo título (reediciones) siguen separadas.
            por_dalb, n_crudo_dalb = defaultdict(set), Counter()
            for crudo, dalb in crudos:
                por_dalb[dalb].add(crudo)
                n_crudo_dalb[(crudo, dalb)] += 1
            principal = {}                               # (título, artista) -> (crudo, dalb) del álbum con más pistas
            for (crudo, dalb), n in n_crudo_dalb.items():
                if len(por_dalb[dalb]) == 1:             # carpeta de un solo álbum
                    k = (_norm(crudo[0]), _norm(crudo[1]) if not crudo[1].startswith(f"{dalb.split('/', 1)[0]}/") else "")
                    if k not in principal or n > n_crudo_dalb[principal[k]]:
                        principal[k] = (crudo, dalb)
            sin_aa = {}                                  # álbumes sin albumartist: (título, artista de pista) -> principal
            for i, (crudo, dalb) in enumerate(crudos):
                if len(por_dalb[dalb]) == 1 and not getattr(pistas[i].m, "aartist", None):
                    sin_aa.setdefault((_norm(crudo[0]), _norm(getattr(pistas[i].m, "artist", ""))), (crudo, dalb))
            for i, (crudo, dalb) in enumerate(crudos):
                if len(por_dalb[dalb]) > 1:              # pista suelta en una carpeta mezclada
                    m = pistas[i].m
                    aa = getattr(m, "aartist", None)
                    dest = principal.get((_norm(crudo[0]), _norm(aa))) if aa else \
                        sin_aa.get((_norm(crudo[0]), _norm(getattr(m, "artist", ""))))
                    if dest and dest != (crudo, dalb):
                        crudos[i] = dest
            carpetas_de = defaultdict(set)
            for crudo, dalb in crudos:
                carpetas_de[crudo].add(dalb)
            # 2) álbumes
            for i, p in enumerate(pistas):
                self._ceder(i)
                m, (crudo, dalb) = p.m, crudos[i]
                ck = crudo if len(carpetas_de[crudo]) == 1 else crudo + (dalb,)   # IDs estables si no hay homónimos
                aid = ids_alb.get(ck)
                if aid is None:
                    aid = ids_alb[ck] = _hid(*ck)
                a = albumes.get(aid)
                if a is None:
                    a = albumes[aid] = {"id": aid, "titulo": crudo[0], "aa": getattr(m, "aartist", ""), "artistas": Counter(),
                                        "anio": 0, "pistas": [], "key": p.key, "dir": p.dir, "mt": 0,
                                        "hires": False, "generos": set(), "asort": "", "comp": False,
                                        "ext": "", "bits": 0, "sr": 0, "br": 0, "_q": None, "aarts": "",
                                        "ry": 0, "fecha": "", "of": "", "ag": None, "mod": 0}
                p.al = aid
                a["pistas"].append(i)
                a["artistas"][getattr(m, "artist", "")] += 1
                a["anio"] = max(a["anio"], getattr(m, "year", 0))
                if getattr(m, "of", "") > a["of"]:
                    a["of"] = m["of"]                         # fecha original completa (desempata el mismo año)
                a["ag"] = p.ag if a["ag"] is None else min(a["ag"], p.ag)   # llegó cuando llegó su 1.ª pista
                a["mt"] = a["ag"]
                a["mod"] = max(a["mod"], p.mt)                 # última modificación (para "Modificados")
                a["aarts"] = a["aarts"] or getattr(m, "aarts", "")
                if not a["fecha"] and getattr(m, "fecha", None):
                    a["fecha"], a["ry"] = m["fecha"], getattr(m, "ry", 0)
                q = (ORDEN_CALIDAD[::-1].index(calidad_pista(p.ext, getattr(m, "bits", None), getattr(m, "sr", None), getattr(m, "br", None))),
                     getattr(m, "bits", None) or 0, getattr(m, "sr", None) or 0, getattr(m, "br", None) or 0)
                if a["_q"] is None or q > a["_q"]:          # la mejor calidad del álbum, para {calidad}
                    a.update(_q=q, ext=p.ext, bits=q[1], sr=q[2], br=q[3])
                a["asort"] = a["asort"] or getattr(m, "asort", "")
                a["comp"] = a["comp"] or bool(getattr(m, "comp", None))
                if (getattr(m, "bits", None) or 0) > 16 or (getattr(m, "sr", None) or 0) > 48000:
                    a["hires"] = True
                gtxt = getattr(m, "genre", None)
                if gtxt:
                    gs = generos_txt.get(gtxt)
                    if gs is None:
                        gs = generos_txt[gtxt] = _multi(gtxt, ";/")
                    a["generos"].update(gs)
            for a in albumes.values():                    # carpeta del álbum = donde está la mayoría de sus pistas
                if len(a["pistas"]) > 1:
                    a["dir"] = Counter(pistas[i].dir for i in a["pistas"]).most_common(1)[0][0]
            por_carpeta = defaultdict(set)
            for a in albumes.values():
                por_carpeta[(a["key"], a["dir"])].add(a["id"])
            for a in albumes.values():
                a["mixto"] = len(por_carpeta[(a["key"], a["dir"])]) > 1
            artistas, generos, interpretes = {}, {}, {}
            varios = T("varios")
            for a in albumes.values():
                if a["aa"]:
                    a["artista"] = a["aa"]
                else:
                    nombres = [n for n in a["artistas"] if n]
                    a["artista"] = (nombres[0] if len(nombres) == 1 and not a["comp"]
                                    else (varios if nombres else T("desconocido")))
                a["pistas"].sort(key=lambda i: (getattr(pistas[i].m, "disc", 0), getattr(pistas[i].m, "track", 0) or 999,
                                                pistas[i].rel))       # nombres "NN. Título": desempate simple basta
                if getattr(pistas[a["pistas"][0]].m, "disc", 0) != getattr(pistas[a["pistas"][-1]].m, "disc", 0):
                    a["varios_discos"] = True
                a["nombres"] = nombres_artistas(a.get("aarts"), a["artista"])   # ALBUMARTISTS si existe
                for n in a["nombres"]:
                    ar = artistas.setdefault(_hid(n), {"id": _hid(n), "nombre": n, "albumes": []})
                    ar["albumes"].append(a["id"])
                for g in a["generos"]:
                    ge = generos.setdefault(_hid(g), {"id": _hid(g), "nombre": g, "artistas": {}})
                    for n in a["nombres"]:
                        ge["artistas"].setdefault(_hid(n), []).append(a["id"])
                for i in a["pistas"]:                            # artistas de pista (featuring, compilaciones…)
                    mi = pistas[i].m
                    for n in nombres_artistas(getattr(mi, "arts", None), getattr(mi, "artist", "")):
                        ip = interpretes.setdefault(_hid(n), {"id": _hid(n), "nombre": n, "albumes": {}})
                        ip["albumes"].setdefault(a["id"], []).append(i)
            # foto de artista embebida (tipos 7/8/10/19): la primera pista que la tenga
            for a in albumes.values():
                for i in a["pistas"]:
                    pt = getattr(pistas[i].m, "pt", None)
                    if pt and any(t in PRIO_IMG_ARTISTA for t in pt):
                        for n in a["nombres"]:
                            artistas[_hid(n)].setdefault("emb", i)
                        mi = pistas[i].m
                        for n in nombres_artistas(getattr(mi, "arts", None), getattr(mi, "artist", "")):
                            if _hid(n) in interpretes:
                                interpretes[_hid(n)].setdefault("emb", i)
            iguales = defaultdict(list)
            for aid, a in albumes.items():
                iguales[(_norm(a["titulo"]), _norm(a["artista"]))].append(aid)
            for grupo in iguales.values():                # dos "21" de Adele (reediciones): " · <carpeta>", pero solo
                if len(grupo) > 1:                        # si el título con formato no basta ("Peter Gabriel [1977]")
                    for aid in grupo:
                        a = albumes[aid]
                        a["iguales"] = grupo
                        a["disamb"] = _carpeta_album(a["dir"]).rsplit("/", 1)[-1] or self.app.bib.raices[a["key"]][1]
            # mismo año: por fecha original completa (1967-06-01 antes que 1967-11-27) y luego por título
            por_anio = lambda aid: (albumes[aid]["anio"] or 9999, albumes[aid]["of"], _orden(albumes[aid]["titulo"]))
            por_titulo = lambda aid: (_orden(albumes[aid]["asort"] or albumes[aid]["titulo"]),)
            for ar in artistas.values():                         # compilaciones: por título (o albumsort), no por año
                ar["albumes"].sort(key=por_titulo if ar["nombre"] == varios else por_anio)
            for ge in generos.values():
                for k, l in ge["artistas"].items():
                    l.sort(key=por_titulo if artistas.get(k, {}).get("nombre") == varios else por_anio)
            for ip in interpretes.values():
                ip["orden"] = sorted(ip["albumes"], key=por_anio)
            d = {
                "fuera": n_fuera,
                "pistas": pistas, "albumes": albumes, "artistas": artistas, "generos": generos, "interpretes": interpretes,
                "orden_artistas": sorted(artistas, key=lambda k: _orden(artistas[k]["nombre"])),
                "orden_interpretes": sorted(interpretes, key=lambda k: _orden(interpretes[k]["nombre"])),
                "orden_albumes": sorted(albumes, key=lambda k: (_orden(albumes[k]["asort"] or albumes[k]["titulo"]),
                                                               _orden(albumes[k]["artista"]), albumes[k]["anio"])),
                "orden_generos": sorted(generos, key=lambda k: _orden(generos[k]["nombre"])),
                "recientes": sorted(albumes, key=lambda k: (-albumes[k]["mt"], _orden(albumes[k]["titulo"])))[:200],
                "modificados": sorted(albumes, key=lambda k: (-albumes[k]["mod"], _orden(albumes[k]["titulo"])))[:200],
                "hires": sorted((k for k in albumes if albumes[k]["hires"]),
                                key=lambda k: (_orden(albumes[k]["artista"]),) + por_anio(k)),
                "decadas": {},
                "conteo_dir": Counter(),         # (raiz, carpeta rel) -> pistas debajo (recursivo)
            }
            directas = Counter((p.key, p.dir) for p in pistas)
            for (k, carpeta), n in directas.items():
                d["conteo_dir"][(k, "")] += n
                if carpeta:
                    partes = carpeta.split("/")
                    for j in range(1, len(partes) + 1):
                        d["conteo_dir"][(k, "/".join(partes[:j]))] += n
            for k in sorted(albumes, key=por_anio):
                y = albumes[k]["anio"]
                if y:
                    d["decadas"].setdefault(y // 10 * 10, []).append(k)
            d["cat_x"] = cat_x
            # Favoritas: pistas con 4 o 5 estrellas, por artista del álbum, álbum y número
            fav = [i for i, p in enumerate(pistas) if p.m.get("x")
                   and estrellas(p.m.x.get("rating") or p.m.x.get("fmps_rating") or 0) >= 4]
            pos = {i: n for n, i in enumerate(i for k in d["orden_albumes"] for i in albumes[k]["pistas"])}
            d["favoritas"] = sorted(fav, key=lambda i: pos.get(i, 1 << 30))
            d["x"] = {}
            for vid, defi in self.defs_x(activas=True).items():
                try:
                    d["x"][vid] = self._x_preparar(d, defi)
                except Exception:
                    log.exception("Custom view %s", vid)
            d["firma"] = firma
            del prev, viejo, crudos_json, vigentes, filas
            if cambiados:                               # releerlos en segundo plano; al terminar, el índice se pone al día
                self.app.tags.obtener(cambiados, espera=0)
                log.info("Tag index: %d changed files being read again", len(cambiados))
            # generación del índice: las cachés la usan de llave (id() se puede repetir al liberar el anterior)
            self.gen += 1
            d["gen"] = self.gen
            self.d, self.version, self.t_build = d, v, time.monotonic()
            self._x_cache = OrderedDict()
            # textos normalizados para la búsqueda: ya con el índice publicado (no retrasa la navegación)
            d["norm"] = self._normalizados(pistas)
            log.info("Tag index: %d tracks, %d albums, %d artists, %d genres (%.1f s)",
                     len(pistas), len(albumes), len(artistas), len(generos), time.time() - t0)
        except Exception:
            self.t_fallo = time.monotonic()
            log.exception("Building the tag index (retrying in 60 s)")
        finally:
            self.construyendo = False
            self.listo.set()

    def diagnostico(self):
        """Salud de la biblioteca, con lo que el índice ya sabe (sin leer archivos): álbumes partidos por tags
        inconsistentes, posibles duplicados, álbumes incompletos, sin portada, pistas sin tags e ilegibles.
        Se calcula al pedirlo y queda guardado mientras el índice no cambie."""
        d = self.asegurar()
        if d is None:
            return {"listo": False}
        if "_diag" in d:
            return d["_diag"]
        t0, MAX = time.time(), 200
        pistas, albumes, bib = d["pistas"], d["albumes"], self.app.bib
        nombre_raiz = lambda k: bib.raices.get(k, ("", k))[1]
        ruta_txt = lambda k, rel: f"{nombre_raiz(k)}/{rel}" if rel else nombre_raiz(k)
        cats = {c: [] for c in ("partidos", "duplicados", "incompletos", "sin_portada", "sin_tags", "ilegibles")}
        cuenta = dict.fromkeys(cats, 0)

        def agregar(c, titulo, ruta, detalle=""):
            cuenta[c] += 1
            if len(cats[c]) < MAX:
                cats[c].append({"t": titulo, "r": ruta, "d": detalle})
        # 1) una carpeta, un título, varios álbumes: el artista del álbum no coincide entre sus pistas
        por_dir = defaultdict(lambda: defaultdict(set))
        for p in pistas:
            a = albumes.get(p.al)
            if a:
                por_dir[(p.key, p.dir)][_norm(a["titulo"])].add(p.al)
        for (k, carpeta), titulos in por_dir.items():
            for ids in titulos.values():
                if len(ids) > 1:
                    al = [albumes[x] for x in ids]
                    agregar("partidos", al[0]["titulo"], ruta_txt(k, carpeta),
                            " · ".join(sorted({x["artista"] or "—" for x in al})))
        # 2) el mismo álbum (título y artista) en varias carpetas: duplicado o dos ediciones
        grupos = defaultdict(list)
        for k, a in albumes.items():
            grupos[(_norm(a["titulo"]), _norm(a["artista"]))].append(a)
        for g in grupos.values():
            if len(g) > 1:
                agregar("duplicados", f"{g[0]['titulo']} — {g[0]['artista']}",
                        " | ".join(ruta_txt(a["key"], a["dir"]) for a in g[:4]), str(len(g)))
        # 3) faltan números de pista (1, 2, 4… sin el 3)
        for a in albumes.values():
            discos = defaultdict(set)
            for i in a["pistas"]:
                m = pistas[i].m
                if getattr(m, "track", 0):
                    discos[getattr(m, "disc", 0) or 1].add(m.track)
            faltan = []
            for n, nums in sorted(discos.items()):
                if len(nums) >= 3:
                    huecos = sorted(set(range(1, max(nums) + 1)) - nums)
                    faltan += [f"{n}-{x}" if len(discos) > 1 else str(x) for x in huecos]
            if faltan:
                agregar("incompletos", f"{a['titulo']} — {a['artista']}", ruta_txt(a["key"], a["dir"]),
                        ", ".join(faltan[:8]) + ("…" if len(faltan) > 8 else ""))
        # 4) sin portada: ni imagen en su carpeta ni embebida en sus pistas (una vez por carpeta)
        vistas_dir = set()
        for a in albumes.values():
            if (a["key"], a["dir"]) in vistas_dir or any(getattr(pistas[i].m, "pt", None) for i in a["pistas"]):
                continue
            vistas_dir.add((a["key"], a["dir"]))
            ruta = bib.resolver(a["key"], a["dir"])
            l = bib.listar(ruta, ttl=None) if ruta else None
            if l is not None and not l[2]:
                agregar("sin_portada", f"{a['titulo']} — {a['artista']}", ruta_txt(a["key"], a["dir"]))
        # 5) sin título, artista o álbum en los tags
        for p in pistas:
            m = p.m
            if not getattr(m, "sr", None) and not getattr(m, "br", None):
                continue                                 # no se pudo leer: va en "ilegibles"
            falta = [T("campo." + c) for c in ("title", "artist", "album") if not getattr(m, c, None)]
            if falta:
                agregar("sin_tags", p.rel.rsplit("/", 1)[-1], ruta_txt(p.key, p.dir), ", ".join(falta))
        # 6) archivos que el lector no pudo abrir (dañados o formato no soportado)
        with self.app.tags.lock:
            filas = self.app.tags.db.execute(
                "SELECT ruta FROM pistas WHERE meta NOT LIKE '%\"dur\"%' AND meta NOT LIKE '%\"sr\"%'").fetchall()
        for (r,) in filas:
            kr = bib.clave_rel(r)
            if kr:
                agregar("ilegibles", os.path.basename(r), ruta_txt(kr[0], kr[1].rsplit("/", 1)[0] if "/" in kr[1] else ""))
        res = {"listo": True, "cuenta": cuenta, "items": cats, "pistas": len(pistas), "albumes": len(albumes),
               "ms": round((time.time() - t0) * 1000)}
        d["_diag"] = res
        return res

    def resumen(self):
        d = self.d
        return {"listo": d is not None, "construyendo": self.construyendo,
                "pistas": len(d["pistas"]) if d else 0, "albumes": len(d["albumes"]) if d else 0,
                "artistas": len(d["artistas"]) if d else 0, "generos": len(d["generos"]) if d else 0}

    # ---- títulos con plantilla ("Korn (15)", "Álbum [1999]")
    def nc(self, nombre, pistas, elementos=None, albumes=None):
        """Título de un contenedor (artista, género, carpeta…) con la plantilla "contenedor".
        {n} = pistas o elementos (álbumes de un artista, artistas de un género…) según tipo_conteo."""
        v = self.app.cfg["vistas"]
        n = elementos if v.get("tipo_conteo") == "elementos" else pistas
        return plantilla(v["plantillas"]["contenedor"], {"nombre": nombre, "n": n, "pistas": pistas, "albumes": albumes})

    def artista_album(self, a):
        """Artista del álbum que se MUESTRA: el crédito impreso (DISPLAY ALBUMARTIST…) si la primera pista lo trae."""
        d = self.d
        if d and a.get("pistas"):
            x = d["pistas"][a["pistas"][0]]["m"].get("x")
            t = tag_mostrado(x, lista_tags(self.app.cfg["vistas"].get("tags_artista_album")))
            if t:
                return t
        return a.get("artista", "")

    def pista_por_ruta(self, key, rel):
        """Pista del índice por ruta (o None). En Windows sin distinguir mayúsculas (rutas de una playlist)."""
        d = self.d
        if not d:
            return None
        pr = d.get("por_ruta")
        if pr is None:
            pr = d["por_ruta"] = {(p["key"], p["rel"]): i for i, p in enumerate(d["pistas"])}
        i = pr.get((key, rel))
        if i is None and ES_WINDOWS:
            pc = d.get("por_ruta_cf")
            if pc is None:
                pc = d["por_ruta_cf"] = {(p["key"], p["rel"].casefold()): i for i, p in enumerate(d["pistas"])}
            i = pc.get((key, rel.casefold()))
        return d["pistas"][i] if i is not None else None

    def album_de(self, key, rel):
        """Álbum del índice al que pertenece una pista (o None: carpeta sin indexar, aún no leída…)."""
        d = self.d
        if not d:
            return None
        pr = d.get("por_ruta")
        if pr is None:
            pr = d["por_ruta"] = {(p["key"], p["rel"]): i for i, p in enumerate(d["pistas"])}
        i = pr.get((key, rel))
        return d["albumes"].get(d["pistas"][i]["al"]) if i is not None else None

    def titulo_album_pista(self, key, rel):
        """Título con formato del álbum de una pista (memo por álbum mientras no cambien índice ni ajustes)."""
        d = self.d
        a = self.album_de(key, rel)
        if not a:
            return None
        memo = d.setdefault("_tit_pista", {})
        k = (a["id"], getattr(self.app.bib, "cfg_gen", 0))
        t = memo.get(k)
        if t is None:
            t = memo[k] = self.na(a, len(a["pistas"]))
        return t

    def na(self, a, pistas, titulo=None, plant=None, _sin_dis=False):
        """Título de un álbum con la plantilla "album" (o la de la vista personalizada)."""
        v = self.app.cfg["vistas"]
        if titulo:                                          # Décadas: "1999 · Álbum"
            return titulo
        orig, ed = a["anio"] or 0, a.get("ry") or 0
        años = f"{orig}/{ed}" if orig and ed and ed != orig else (orig or ed or "")
        campos = {"album": a["titulo"], "año": a["anio"] or "", "artista": mostrar_artistas(self.artista_album(a)),
                  "años": años, "año_edicion": ed or "", "pistas": pistas,
                  "n": pistas if v.get("tipo_conteo") != "elementos" else "", "calidad": calidad_album(a),
                  "formato": a.get("ext", "").upper(), "genero": next(iter(sorted(a.get("generos") or [])), ""),
                  "fecha": a.get("fecha") or "", "fecha_original": a.get("of") or a["anio"] or ""}
        d = self.d
        def extra(k):
            if not d or not a["pistas"]:
                return ""
            x = d["pistas"][a["pistas"][0]]["m"].get("x") or {}
            return x.get(k, "")
        t = plantilla(plant or v["plantillas"]["album"], campos, extra)
        dis = a.get("disamb")
        if dis and not _sin_dis and v.get("carpeta_en_iguales", True) and _norm(dis) not in _norm(t) and d:
            # la carpeta solo si otro álbum igual se vería con el mismo título (mismo año, plantilla sin año…);
            # se compara sin conteos: "21 [2011] (11)" y "21 [2011] (14)" siguen siendo el mismo título
            base = self.na(a, 0, plant=plant, _sin_dis=True)
            if any(o is not a and self.na(o, 0, plant=plant, _sin_dis=True) == base
                   for o in (d["albumes"].get(x) for x in a.get("iguales", ())) if o):
                t += f" · {dis}"
        return t

    def n_albumes(self, d, aids):
        return sum(len(d["albumes"][a]["pistas"]) for a in set(aids))

    def conteo_carpeta(self, key, rel):
        d = self.d
        return d["conteo_dir"].get((key, rel), 0) if d else 0

    # ---- imagen de artista
    def _art_artista(self, d, hid, albs):
        if not albs:
            return None
        if self.app.cfg["portadas"]["artista"]:
            return f"/art/r/{hid}/{albs[0]}.jpg"
        return self._art_album(d["albumes"][albs[0]])

    def fuente_artista(self, hid, aid):
        """artist.jpg (en la carpeta del álbum o la del artista) › foto de artista embebida › portada del álbum aid.
        "folder.jpg" de la carpeta padre solo cuenta si esa carpeta se llama como el artista."""
        d = self.d
        if d is None:
            return None
        if not hasattr(self, "_art_ar"):
            self._art_ar = OrderedDict()
        ck = (hid, aid, d.get("gen"), self.app.cfg["portadas"]["artista"])
        with self.lock:
            if ck in self._art_ar:
                c = self._art_ar[ck]
                if time.monotonic() - c[0] < 300:
                    return c[1]
        bib, src = self.app.bib, None
        ar = d["artistas"].get(hid) or d["interpretes"].get(hid)
        if ar and self.app.cfg["portadas"]["artista"]:
            nombre = _norm(ar["nombre"])
            albs = ar["albumes"] if isinstance(ar["albumes"], list) else ar.get("orden", [])
            vistos = set()
            for a2 in albs[:8]:
                a = d["albumes"].get(a2)
                if not a:
                    continue
                carpeta = bib.resolver(a["key"], _carpeta_album(a["dir"]))
                if not carpeta or a.get("mixto"):         # una playlist no es la carpeta del artista
                    continue
                for c in ([carpeta, os.path.dirname(carpeta)] if a["dir"] else [carpeta]):
                    if c in vistos:
                        continue
                    vistos.add(c)
                    img = bib._imagen_llamada(c, set(ARTISTA_IMG))
                    if not img and c != carpeta and _norm(os.path.basename(c)) == nombre:
                        img = bib._imagen_llamada(c, set(PORTADA_PREF))
                    if img:
                        src = ("img", img)
                        break
                if src:
                    break
            if not src and "emb" in ar:
                p = d["pistas"][ar["emb"]]
                ruta = bib.resolver(p["key"], p["rel"])
                if ruta:
                    src = ("emb_ar", ruta)
        if not src and aid in d["albumes"]:                # la portada de SU álbum, no la de la carpeta
            a = d["albumes"][aid]
            if a.get("mixto") and a["pistas"]:            # carpeta mezclada: la de su primera pista
                p = d["pistas"][a["pistas"][0]]
                ruta = bib.resolver(p["key"], p["rel"])
                src = bib.fuente_portada_pista(ruta) if ruta else None
            else:
                ruta = bib.resolver(a["key"], a["dir"])
                src = bib.fuente_portada_carpeta(ruta, max_prof=0) if ruta else None
        with self.lock:
            self._art_ar[ck] = (time.monotonic(), src)
            while len(self._art_ar) > 4096:
                self._art_ar.popitem(last=False)
        return src

    # ---- descriptores
    def _art_album(self, a):
        if a.get("mixto") and self.d and a["pistas"]:        # carpeta con varios álbumes: la de su primera pista
            p = self.d["pistas"][a["pistas"][0]]
            return f"/art/t/{p['key']}/{quote(p['rel'], safe='/')}.jpg"
        q = quote(a["dir"], safe="/")
        return f"/art/a/{a['key']}/{q}{'/' if q else ''}cover.jpg"

    def _n_hijos_album(self, d, a, idxs):
        """childCount de un álbum: sus pistas + las carpetas de disco (si la opción está activa)."""
        if not a.get("varios_discos") or not self.app.cfg["vistas"].get("discos"):
            return len(idxs)
        return len(idxs) + len({d["pistas"][i]["m"].get("disc") or 0 for i in idxs})

    def _c_album(self, d, aid, oid=None, titulo=None):
        a = d["albumes"][aid]
        return ("v", oid or f"v:al:{aid}", self.na(a, len(a["pistas"]), titulo), self._n_hijos_album(d, a, a["pistas"]),
                self._art_album(a), C_ALBUM, self.artista_album(a), a.get("fecha") or a["anio"] or None)

    def _c_album_ia(self, d, iid, aid):
        """Álbum dentro de un artista de pista: solo cuenta (y contiene) las pistas de ese artista."""
        a, idxs = d["albumes"][aid], d["interpretes"][iid]["albumes"][aid]
        return ("v", f"v:ia:{iid}:al:{aid}", self.na(a, len(idxs)), self._n_hijos_album(d, a, idxs), self._art_album(a), C_ALBUM,
                self.artista_album(a), a.get("fecha") or a["anio"] or None)

    def _c_interprete(self, d, iid):
        ip = d["interpretes"][iid]
        art = self._art_artista(d, iid, ip["orden"])
        n = sum(len(l) for l in ip["albumes"].values())
        return ("v", f"v:ia:{iid}", self.nc(ip["nombre"], n, len(ip["orden"]), len(ip["orden"])),
                len(ip["orden"]) + 1, art, C_ARTISTA, None, None)

    def _c_artista(self, d, arid, oid=None, albumes=None):
        ar = d["artistas"][arid]
        albs = albumes if albumes is not None else ar["albumes"]
        art = self._art_artista(d, arid, albs)
        return ("v", oid or f"v:ar:{arid}", self.nc(ar["nombre"], self.n_albumes(d, albs), len(albs), len(albs)),
                len(albs) + (1 if len(albs) else 0), art, C_ARTISTA, None, None)

    def _t(self, d, i):
        p = d["pistas"][i]
        return ("t", p["key"], p["rel"], p["mt"], p["tam"])

    def _pistas_album(self, d, aid, idxs, oid):
        """Pistas de un álbum (idxs ya en orden). Con "discos" y más de un disco, antes de cada disco va una
        carpeta "» subtítulo" (oid:d:N) con su portada: la lista sigue completa (Play = todo el álbum, los
        reproductores ignoran las carpetas al encolar) y cada disco se puede abrir o encolar solo."""
        if not self.app.cfg["vistas"].get("discos"):
            return [self._t(d, i) for i in idxs]
        pistas = d["pistas"]
        grupos = OrderedDict()
        for i in idxs:
            grupos.setdefault(pistas[i]["m"].get("disc") or 0, []).append(i)
        if len(grupos) < 2:
            return [self._t(d, i) for i in idxs]
        a = d["albumes"][aid]
        out = []
        for n, l in grupos.items():
            ds = next((pistas[i]["m"]["x"]["discsubtitle"] for i in l if (pistas[i]["m"].get("x") or {}).get("discsubtitle")), "")
            if ds:                                    # su portada: <subtítulo>.jpg / embebida de la 1.ª pista
                p = pistas[l[0]]
                art = f"/art/t/{p['key']}/{quote(p['rel'], safe='/')}.jpg"
            else:
                art = self._art_album(a)
            titulo = "» " + (ds or T("disco", n=n or "?"))
            out.append(("v", f"{oid}:d:{n}", titulo, len(l), art, C_ALBUM, self.artista_album(a),
                        a.get("fecha") or a["anio"] or None))
            out += [self._t(d, i) for i in l]
        return out

    def _disco(self, oid):
        """'<oid del álbum>:d:N' -> (oid del álbum, pistas de ese disco, descriptor) o None."""
        m = _RE_OID_DISCO.match(oid)
        if not m:
            return None
        hijos = self._hijos(m.group(1)) or []
        out, desc, dentro = [], None, False
        for h in hijos:
            if h[0] == "v":
                if dentro:
                    break
                if h[1] == oid:
                    desc, dentro = h, True
            elif dentro:
                out.append(h)
        return (m.group(1), out, desc) if desc else None

    def numero_seguido(self, key, rel):
        """Álbum de varios discos con "numeración seguida": número de la pista en todo el álbum (o None)."""
        a = self.album_de(key, rel)
        if not a or not a.get("varios_discos"):
            return None
        d = self.d
        memo = d.setdefault("_num", {})
        pos = memo.get(a["id"])
        if pos is None:
            pos = memo[a["id"]] = {(d["pistas"][i]["key"], d["pistas"][i]["rel"]): n
                                   for n, i in enumerate(a["pistas"], 1)}
        return pos.get((key, rel))

    def _agrupar(self, oid_base, ids, nombre_de, fabricar, pistas_de=None, titulo_todos=None):
        v = self.app.cfg["vistas"]
        lim = v["agrupar_desde"] if v.get("agrupar_letras") else 0
        if not lim or len(ids) <= lim:
            return [fabricar(k) for k in ids]
        grupos = OrderedDict()
        for k in ids:
            grupos.setdefault(_letra(nombre_de(k)), []).append(k)
        orden = sorted(grupos, key=lambda g: (g == "#", g == "0-9", g))
        todos = [("v", f"{oid_base}@*", titulo_todos or (T("todos." + oid_base[2:]) + f" ({len(ids):,})"), len(ids), None,
                  "object.container.storageFolder", None, None)]                 # lista completa, sin letras
        return todos + [("v", f"{oid_base}@{g}", (self.nc(g, sum(pistas_de(k) for k in grupos[g]), len(grupos[g])) if pistas_de else g),
                 len(grupos[g]), None, "object.container.storageFolder", None, None) for g in orden]

    # ═══════════════ motor de vistas por niveles (predefinidas: compositores, sellos…; y las del usuario)
    def defs_x(self, activas=False):
        """{id: definición} de las vistas hechas con el motor. activas=True: solo las que están en la raíz."""
        v = self.app.cfg["vistas"]
        out = OrderedDict()
        for k, dfn in PRESETS_X.items():
            out[k] = dict(dfn, id=k, nombre=VISTAS.get(k, k), filtro=[], plantilla_album="", sin_tag=False)
        for x in v.get("personalizadas", []):
            out[x["id"]] = x
        if activas:
            act = set(v["activas"])
            out = OrderedDict((k, x) for k, x in out.items() if k in act)
        return out

    def es_x(self, vid):
        return vid in PRESETS_X or (vid[:1] == "c" and any(x["id"] == vid for x in self.app.cfg["vistas"]["personalizadas"]))

    def claves_x_necesarias(self):
        """Tags extra que hay que tener en memoria: los de las vistas activas y los de las plantillas."""
        claves = set()
        for dfn in self.defs_x(activas=True).values():
            claves.update(n for n in map(lambda t: partir_mod(t)[0], dfn["niveles"]) if n not in NIVELES_CALC)
            claves.update(n for n in (partir_mod(f["tag"])[0] for f in dfn["filtro"]) if n not in NIVELES_CALC)
            claves.update(_RE_CAMPO.findall(dfn.get("plantilla_album") or ""))
        v = self.app.cfg["vistas"]
        claves.update(_RE_CAMPO.findall(v["plantillas"]["album"]))
        claves.update(_RE_CAMPO.findall(v["plantillas"].get("pista") or ""))
        conocidos = {"nombre", "n", "pistas", "albumes", "album", "año", "años", "año_edicion", "artista", "calidad",
                     "formato", "genero", "titulo", "numero", "disco", "fecha", "fecha_original"}
        claves = {partir_mod(c.strip().casefold())[0] for c in claves}
        return ({_ALIAS_CAMPO.get(c, c) for c in claves} - conocidos - set(_ALIAS_CAMPO.values()) - set(_ALIAS_CAMPO)
                | set(lista_tags(v.get("tags_artista_album")))
                | ({"discsubtitle"} if v.get("discos") else set())
                | set(TAGS_CALIFICACION))

    def _crudo(self, d, tag, i):
        """Dato crudo (hashable) del que sale el valor de un nivel: muchas pistas comparten el mismo,
        así que los valores se calculan una vez por dato crudo, no por pista."""
        p = d["pistas"][i]
        m = p["m"]
        if "." in tag:
            b, mod = partir_mod(tag)
            if mod:
                return self._crudo(d, b, i)
        if tag == "artist":
            return m.get("arts") or m.get("artist") or m.get("aartist", "")
        if tag == "albumartist":
            a = d["albumes"][p["al"]]
            return a.get("aarts") or a["artista"]
        if tag == "genre":
            return m.get("genre", "")
        if tag in ("year", "#decada"):
            return m.get("year", 0)
        if tag in ("#calidad", "#resolucion"):
            return (p["ext"], m.get("bits"), m.get("sr"), m.get("br"))
        if tag == "#formato":
            return p["ext"]
        if tag == "#agregado":
            return (p.get("ag") or 0) // 86400
        return (m.get("x") or {}).get(tag, "")

    def _de_crudo(self, tag, c):
        if "." in tag:
            b, mod = partir_mod(tag)
            if mod:
                return list(dict.fromkeys(x for x in (aplicar_mod(mod, v) for v in self._de_crudo(b, c)) if x))
        if tag in ("artist", "albumartist"):
            return _multi(c)
        if tag == "genre":
            return _multi(c, ";/")
        if tag == "year":
            return [str(c)] if c else []
        if tag == "#decada":
            return [f"{c // 10 * 10}s"] if c else []
        if tag == "#calidad":
            return [T("cal." + calidad_pista(*c))]
        if tag == "#resolucion":
            return [resolucion(*c)]
        if tag == "#formato":
            return ["DSD" if c in ("dsf", "dff") else c.upper()]
        if tag == "#agregado":
            return [time.strftime("%Y-%m", time.localtime(c * 86400 + 43200))] if c else []
        return _multi(c)

    def _valores(self, d, tag, i):
        """Valores de un nivel para la pista i (lista de textos; vacía = sin ese tag)."""
        return self._de_crudo(tag, self._crudo(d, tag, i))

    def _pasa(self, d, filtro, i):
        for f in filtro:
            vals = [x.casefold() for x in self._valores(d, f["tag"], i)]
            q, op = f["valor"].casefold(), f["op"]
            if op == "existe":
                ok = bool(vals)
            elif op == "falta":
                ok = not vals
            elif op == "es":
                ok = q in vals
            elif op == "contiene":
                ok = any(q in x for x in vals)
            elif op == "no_contiene":
                ok = not any(q in x for x in vals)
            else:
                nums = [float(mm.group()) for mm in (re.search(r"-?\d+(\.\d+)?", x) for x in vals) if mm]
                try:
                    lim = float(q)
                except ValueError:
                    return False
                ok = any((x >= lim) if op == "mayor" else (x <= lim) for x in nums)
            if not ok:
                return False
        return True

    def _x_preparar(self, d, dfn):
        """Precalcula, una vez por construcción del índice, los valores de cada nivel por pista.
        vals[nivel][pista] = tupla de ids de valor (compartida entre pistas con el mismo dato: poca memoria)."""
        niveles = dfn["niveles"]
        n = len(d["pistas"])
        base = range(n) if not dfn["filtro"] else [i for i in range(n) if self._pasa(d, dfn["filtro"], i)]
        base = list(base)
        vals, nombres = [], []
        for tag in niveles:
            fila, nom, memo = [None] * n, {}, {}
            for i in base:
                self._ceder(i)
                c = self._crudo(d, tag, i)
                t = memo.get(c)
                if t is None:
                    hs = []
                    for x in self._de_crudo(tag, c):
                        h = _hid(x)
                        nom.setdefault(h, x)
                        if h not in hs:
                            hs.append(h)
                    t = memo[c] = tuple(hs) or ("_",)
                fila[i] = t
            vals.append(fila)
            nombres.append(nom)
        inv0 = defaultdict(list)
        f0 = vals[0]
        for i in base:
            for h in f0[i]:
                inv0[h].append(i)
        return {"dfn": dfn, "base": base, "vals": vals, "nombres": nombres, "inv0": dict(inv0)}

    def _x_ruta(self, oid):
        """'v:<vid>:<h0>:<h1>@B:al:<aid>' -> (vid, [h0, h1], letra, aid)."""
        partes = oid[2:].split(":")
        vid, _, letra = partes[0].partition("@")
        valores, aid = [], None
        resto = partes[1:]
        if len(resto) >= 2 and resto[-2] == "al":
            aid = resto[-1]
            resto = resto[:-2]
        for j, h in enumerate(resto):
            h, _, l = h.partition("@")
            valores.append(h)
            if l and j == len(resto) - 1:
                letra = l
        return vid, valores, letra, aid

    def _x_sel(self, d, vid, valores):
        """Pistas que cumplen el camino elegido (cacheado por construcción del índice)."""
        ck = (vid, tuple(valores))
        c = self._x_cache.get(ck)
        if c is not None:
            return c
        px = d["x"][vid]
        if not valores:
            sel = px["base"]
        else:
            sel = px["inv0"].get(valores[0], [])
            for j, h in enumerate(valores[1:], 1):
                sel = [i for i in sel if h in px["vals"][j][i]]
        with self.lock:
            self._x_cache[ck] = sel
            while len(self._x_cache) > 256:
                self._x_cache.popitem(last=False)
        return sel

    def _x_orden_albumes(self, d, aids):
        al = d["albumes"]
        return sorted(aids, key=lambda k: (_orden(al[k]["artista"]), al[k]["anio"] or 9999, al[k].get("of", ""),
                                           _orden(al[k]["titulo"])))

    def _x_hijos(self, d, oid):
        """Hijos de un nodo de vista personalizada; cacheados hasta el próximo índice o cambio de ajustes."""
        ck = ("h", oid)
        c = self._x_cache.get(ck)
        if c is not None:
            return c
        res = self._x_hijos_calc(d, oid)
        if res is not None:
            with self.lock:
                self._x_cache[ck] = res
                while len(self._x_cache) > 256:
                    self._x_cache.popitem(last=False)
        return res

    def _x_hijos_calc(self, d, oid):
        vid, valores, letra, aid = self._x_ruta(oid)
        if vid not in d["x"]:
            return None
        px = d["x"][vid]
        dfn, niveles = px["dfn"], px["dfn"]["niveles"]
        if len(valores) > len(niveles):
            return None
        sel = self._x_sel(d, vid, valores)
        base_oid = "v:" + ":".join([vid] + valores)
        if aid is not None:                                     # álbum: solo sus pistas que entran en la selección
            dentro = set(sel)
            return self._pistas_album(d, aid, [i for i in d["albumes"][aid]["pistas"] if i in dentro], oid)
        if len(valores) == len(niveles):                        # último nivel: álbumes
            por_album = OrderedDict()
            for i in sel:
                por_album.setdefault(d["pistas"][i]["al"], []).append(i)
            plant = dfn.get("plantilla_album") or None
            out = []
            for a in self._x_orden_albumes(d, por_album):
                al, n = d["albumes"][a], len(por_album[a])
                out.append(("v", f"{base_oid}:al:{a}", self.na(al, n, plant=plant),
                            self._n_hijos_album(d, al, por_album[a]), self._art_album(al), C_ALBUM,
                            self.artista_album(al), al.get("fecha") or al["anio"] or None))
            return out
        k = len(valores)
        tag = niveles[k]
        grupos = OrderedDict()
        for i in sel:
            for h in px["vals"][k][i]:
                grupos.setdefault(h, []).append(i)
        nom = px["nombres"][k]
        sin = grupos.pop("_", None)
        if k == 0 and not dfn.get("sin_tag"):              # arriba se oculta; en niveles más profundos no (se perderían)
            sin = None
        if tag == "#calidad":
            rango = {T("cal." + c): j for j, c in enumerate(ORDEN_CALIDAD)}
            ids = sorted(grupos, key=lambda h: rango.get(nom[h], 9))
        elif tag in NIVELES_NUM_DESC or partir_mod(tag)[1]:
            def num(h):
                return tuple(-float(x) for x in re.findall(r"\d+(?:\.\d+)?", nom[h])) or (0,)
            ids = sorted(grupos, key=num)
        else:
            ids = sorted(grupos, key=lambda h: _orden(nom[h]))
        siguiente = niveles[k + 1] if k + 1 < len(niveles) else None
        persona = tag in NIVELES_PERSONA

        def fabricar(h):
            tr = grupos[h] if h != "_" else sin
            albs = list(dict.fromkeys(d["pistas"][i]["al"] for i in tr))
            if siguiente:
                elems = len({x for i in tr for x in px["vals"][k + 1][i]})
            else:
                elems = len(albs)
            nombre = nom.get(h) or T("sin_tag", tag=nombre_tag(tag))
            art = (self._art_artista(d, h, albs) if persona and h != "_" else self._art_album(d["albumes"][albs[0]]))
            clase = C_ARTISTA if persona else (C_GENERO if tag == "genre" else "object.container.storageFolder")
            return ("v", f"{base_oid}:{h}", self.nc(nombre, len(tr), elems, len(albs)), elems + 1, art, clase, None, None)

        if letra:
            ids = [h for h in ids if letra == "*" or _letra(nom[h]) == letra]
            return [fabricar(h) for h in ids]
        alfa = tag not in NIVELES_NUM_DESC and tag != "#calidad" and not partir_mod(tag)[1]
        if alfa:
            out = self._agrupar(base_oid, ids, lambda h: nom[h], fabricar, lambda h: len(grupos[h]),
                                titulo_todos=T("todos.x", n=f"{len(ids):,}"))
        else:
            out = [fabricar(h) for h in ids]
        if sin:
            out.append(fabricar("_"))
        return out

    def _x_pistas(self, d, oid):
        """Todas las pistas de un nodo, en orden de álbum (para "▶ Todas las pistas")."""
        vid, valores, letra, aid = self._x_ruta(oid)
        if vid not in d["x"]:
            return []
        sel = self._x_sel(d, vid, valores)
        px, k = d["x"][vid], len(valores)
        if letra and letra != "*" and aid is None and k < len(px["vals"]):
            nom = px["nombres"][k]
            sel = [i for i in sel if any(h != "_" and _letra(nom[h]) == letra for h in px["vals"][k][i])]

        if aid is not None:
            dentro = set(sel)
            return [self._t(d, i) for i in d["albumes"][aid]["pistas"] if i in dentro]
        dentro, por_album = set(sel), {}
        for i in sel:
            por_album.setdefault(d["pistas"][i]["al"], None)
        out = []
        for a in self._x_orden_albumes(d, por_album):
            out += [self._t(d, i) for i in d["albumes"][a]["pistas"] if i in dentro]
        return out

    def _x_padre(self, oid):
        vid, valores, letra, aid = self._x_ruta(oid)
        if aid is not None:
            return "v:" + ":".join([vid] + valores)
        partes = oid[2:].split(":")
        if "@" in partes[-1]:
            return "v:" + ":".join(partes[:-1] + [partes[-1].split("@")[0]])
        return "v:" + ":".join(partes[:-1]) if len(partes) > 1 else "0"

    def _x_titulo(self, d, oid):
        vid, valores, letra, aid = self._x_ruta(oid)
        dfn = self.defs_x().get(vid)
        partes = [dfn["nombre"] if dfn else vid]
        px = d["x"].get(vid) if d else None
        for j, h in enumerate(valores):
            partes.append(px["nombres"][j].get(h, T("sin_tag", tag="")) if px else h)
        if aid is not None and d and aid in d["albumes"]:
            partes.append(d["albumes"][aid]["titulo"])
        if letra:
            partes.append(letra)
        return " / ".join(partes)

    def catalogo_tags(self):
        """Niveles disponibles para el editor de vistas: calculados + tags extra con cuántas pistas los tienen."""
        d = self.asegurar(espera=0.5)
        n = len(d["pistas"]) if d else 0
        out = [{"tag": t, "pistas": n} for t in NIVELES_CALC]
        if d:
            for k, c in d["cat_x"].most_common():
                out.append({"tag": k, "pistas": c})
                if _RE_TAG_FECHA.search(k):          # tags con fecha: también agrupados por década
                    out.append({"tag": k + ".decade", "pistas": c})
        return {"listo": d is not None, "pistas": n, "tags": out}

    def vista_previa(self, dfn):
        """Primer nivel de una vista sin guardarla: lee los tags extra de la caché si no están en memoria."""
        d = self.asegurar(espera=1)
        if d is None:
            return {"valores": [], "total": 0, "pistas": 0}
        dfn = validar_personalizadas([dfn])
        if not dfn:
            return {"valores": [], "total": 0, "pistas": 0}
        dfn = dfn[0]
        faltan = {partir_mod(n)[0] for n in dfn["niveles"][:1] + [f["tag"] for f in dfn["filtro"]]} - set(NIVELES_CALC)
        tmp = d
        if faltan:                                   # copia superficial con esos tags extra (desde cache.db)
            tmp = dict(d)
            rutas = {}
            for i, p in enumerate(d["pistas"]):
                r = self.app.bib.resolver(p["key"], p["rel"])
                if r:
                    rutas[r] = i
            extra = [dict(p, m=dict(p["m"])) for p in d["pistas"]]
            with self.app.tags.lock:
                filas = self.app.tags.db.execute("SELECT ruta, meta FROM pistas").fetchall()
            for ruta, js in filas:
                i = rutas.get(ruta)
                if i is None or '"x"' not in js:
                    continue
                x = json.loads(js).get("x") or {}
                sx = {k: x[k] for k in faltan if k in x}
                if sx:
                    extra[i]["m"]["x"] = dict(extra[i]["m"].get("x") or {}, **sx)
            tmp["pistas"] = extra
        px = self._x_preparar(tmp, dict(dfn, niveles=dfn["niveles"][:1]))
        cuenta = sorted(((px["nombres"][0].get(h, ""), len(l)) for h, l in px["inv0"].items() if h != "_"),
                        key=lambda x: -x[1])
        return {"valores": cuenta[:12], "total": len(cuenta), "pistas": len(px["base"]),
                "sin_tag": len(px["inv0"].get("_", []))}

    def _tocadas(self, d, recientes):
        """Tocadas recientemente / Más tocadas: pistas (de la caché si están; si no, del disco)."""
        if "por_ruta" not in d:
            d["por_ruta"] = {(p["key"], p["rel"]): i for i, p in enumerate(d["pistas"])}
        out = []
        for key, rel, n in self.app.repro.lista(recientes):
            i = d["por_ruta"].get((key, rel))
            if i is not None:
                out.append(self._t(d, i))
                continue
            ruta = self.app.bib.resolver(key, rel)
            try:
                st = os.stat(ruta) if ruta else None
            except OSError:
                st = None
            if st:
                out.append(("t", key, rel, st.st_mtime_ns, st.st_size))
        return out

    def raiz(self):
        activas = self.app.cfg["vistas"]["activas"]
        if not activas:
            return []
        d = self.asegurar(espera=0.5)
        out, tpl = [], self.app.cfg["vistas"]["plantillas"]["raiz"]
        defs = self.defs_x()
        for k in activas:
            nombre = defs[k]["nombre"] if k in defs else VISTAS.get(k, k)
            n = len(self.app.bib.hijos(f"v:{k}") or []) if d else 1      # cacheada: abrir la vista luego es inmediato
            if k in ("playlists", "favoritas") and not n:
                continue                                  # sin playlists / calificaciones: no se muestra
            out.append(("v", f"v:{k}", plantilla(tpl, {"nombre": nombre, "n": n}), n, None,
                        "object.container.storageFolder", None, None))
        return out

    LIMITE_TODAS = 3000
    SIN_TODAS = {"v:artistas", "v:interpretes", "v:albumes", "v:generos", "v:decadas", "v:aleatorio", "v:indexando",
                 "v:tocadas", "v:mas_tocadas"}

    def _entrada_todas(self, base, n):
        tit = (T("todas", n=f"{n:,}") if n <= self.LIMITE_TODAS
               else T("todas.limite", lim=f"{self.LIMITE_TODAS:,}", n=f"{n:,}"))
        return ("v", f"{base}:todas", tit, min(n, self.LIMITE_TODAS), None, "object.container.playlistContainer", None, None)

    def todas(self, base):
        """Todas las pistas bajo un contenedor (vista o carpeta), en orden de navegación y sin duplicados.
        Se cachea por construcción del índice: paginar en el WiiM no la recalcula.
        Carpetas: del disco en vivo (caché de 20 s para la paginación), sin depender del índice."""
        if base.startswith("v:carpeta:"):
            if not hasattr(self, "_todas_carp"):
                self._todas_carp = OrderedDict()
            with self.lock:
                c = self._todas_carp.get(base)
                if c and time.monotonic() - c[0] < 20:
                    return c[1]
            key, rel = base[10:18], base[19:]
            out = [("t", key, r, m, z) for r, m, z in self.app.bib.pistas_vivas(key, rel, self.LIMITE_TODAS)]
            with self.lock:
                self._todas_carp[base] = (time.monotonic(), out)
                while len(self._todas_carp) > 32:
                    self._todas_carp.popitem(last=False)
            return out
        d = self.d
        if d is None:
            return []
        if not hasattr(self, "_todas_cache"):
            self._todas_cache = OrderedDict()
        ck = (base, d.get("gen"))
        with self.lock:
            if ck in self._todas_cache:
                self._todas_cache.move_to_end(ck)
                return self._todas_cache[ck]
        out = self._calcular_todas(d, base)
        with self.lock:
            self._todas_cache[ck] = out
            while len(self._todas_cache) > 64:
                self._todas_cache.popitem(last=False)
        return out

    def _calcular_todas(self, d, base):
        if self.es_x(base[2:].split(":")[0].split("@")[0]):
            return self._x_pistas(d, base)
        if base.startswith("v:carpeta:"):
            key, rel = base[10:18], base[19:]
            pref = rel + "/" if rel else ""
            sel = [i for i, p in enumerate(d["pistas"]) if p["key"] == key and p["rel"].startswith(pref)]
            nk = {}                                       # orden natural calculado una vez por carpeta, no por pista
            for i in sel:
                c = d["pistas"][i]["dir"]
                if c not in nk:
                    nk[c] = natkey(c)
            sel.sort(key=lambda i: (nk[d["pistas"][i]["dir"]], d["pistas"][i]["m"].get("disc", 0),
                                    d["pistas"][i]["m"].get("track", 0) or 999, d["pistas"][i]["rel"]))
            return [self._t(d, i) for i in sel]
        out, vistos = [], set()

        def rec(oid, prof):
            for h in self._hijos(oid) or []:
                if h[0] == "t":
                    if (h[1], h[2]) not in vistos:
                        vistos.add((h[1], h[2]))
                        out.append(h)
                elif h[0] == "v" and prof < 5 and not _RE_OID_DISCO.match(h[1]):   # discos: ya están en la lista
                    rec(h[1], prof + 1)
        rec(base, 0)
        return out

    def hijos(self, oid):
        if oid.endswith(":todas"):
            if not oid.startswith("v:carpeta:") and self.asegurar() is None:
                return []
            return self.todas(oid[:-6])[:self.LIMITE_TODAS]
        res = self._hijos(oid)
        sin_todas = (oid in self.SIN_TODAS or oid.endswith("@*")          # "Todos los artistas" = media biblioteca
                     or ":al:" in oid or oid.startswith("v:al:")         # álbum con discos: ya lista todo
                     or (":" not in oid[2:] and self.es_x(oid[2:].split("@")[0])))
        if res and not sin_todas and self.d is not None and any(h[0] == "v" for h in res):
            t = self.todas(oid)
            if t:
                res = [self._entrada_todas(oid, len(t))] + res
        return res

    def _hijos(self, oid):
        d = self.asegurar()
        if d is None:
            return [("v", "v:indexando", T("indexando"), 0, None,
                     "object.container.storageFolder", None, None)]
        if ":d:" in oid and _RE_OID_DISCO.match(oid):      # un disco dentro de un álbum
            r = self._disco(oid)
            return r[1] if r else None
        partes = oid[2:].split(":")
        vista, _, letra = partes[0].partition("@")
        try:
            if self.es_x(vista):
                return self._x_hijos(d, oid)
            if vista == "favoritas":
                return [self._t(d, i) for i in d.get("favoritas", ())]
            if vista == "tocadas":
                return self._tocadas(d, recientes=True)
            if vista == "mas_tocadas":
                return self._tocadas(d, recientes=False)
            if vista == "artistas":
                ids = d["orden_artistas"]
                if letra:
                    ids = [k for k in ids if letra == "*" or _letra(d["artistas"][k]["nombre"]) == letra]
                    return [self._c_artista(d, k) for k in ids]
                return self._agrupar("v:artistas", ids, lambda k: d["artistas"][k]["nombre"], lambda k: self._c_artista(d, k),
                                     lambda k: self.n_albumes(d, d["artistas"][k]["albumes"]))
            if vista == "interpretes":
                ids = d["orden_interpretes"]
                if letra:
                    return [self._c_interprete(d, k) for k in ids
                            if letra == "*" or _letra(d["interpretes"][k]["nombre"]) == letra]
                return self._agrupar("v:interpretes", ids, lambda k: d["interpretes"][k]["nombre"],
                                     lambda k: self._c_interprete(d, k),
                                     lambda k: sum(len(l) for l in d["interpretes"][k]["albumes"].values()))
            if vista == "ia":                               # artista de pista -> sus álbumes -> solo sus pistas
                ip = d["interpretes"][partes[1]]
                if len(partes) > 3 and partes[2] == "al":
                    return self._pistas_album(d, partes[3], ip["albumes"][partes[3]], oid)
                return [self._c_album_ia(d, partes[1], a) for a in ip["orden"]]
            if vista == "albumes":
                ids = d["orden_albumes"]
                if letra:
                    return [self._c_album(d, k) for k in ids if letra == "*" or _letra(d["albumes"][k]["titulo"]) == letra]
                return self._agrupar("v:albumes", ids, lambda k: d["albumes"][k]["titulo"], lambda k: self._c_album(d, k),
                                     lambda k: len(d["albumes"][k]["pistas"]))
            if vista == "generos":
                return [("v", f"v:ge:{g}", self.nc(d["generos"][g]["nombre"],
                                 self.n_albumes(d, [a for l in d["generos"][g]["artistas"].values() for a in l]),
                                 len(d["generos"][g]["artistas"])),
                         len(d["generos"][g]["artistas"]) + 1,          # + "▶ Todas las pistas"
                         self._art_album(d["albumes"][next(iter(d["generos"][g]["artistas"].values()))[0]]),
                         C_GENERO, None, None) for g in d["orden_generos"]]
            if vista == "decadas":
                return [("v", f"v:de:{dec}", self.nc(f"{dec}s", self.n_albumes(d, d["decadas"][dec]), len(d["decadas"][dec]), len(d["decadas"][dec])), len(d["decadas"][dec]) + 1, self._art_album(d["albumes"][d["decadas"][dec][0]]),
                         "object.container.storageFolder", None, None) for dec in sorted(d["decadas"])]
            if vista == "recientes":
                return [self._c_album(d, k) for k in d["recientes"]]
            if vista == "modificados":
                return [self._c_album(d, k) for k in d["modificados"]]
            if vista == "hires":
                return [self._c_album(d, k) for k in d["hires"]]
            if vista == "aleatorio":
                n = min(100, len(d["pistas"]))
                return [self._t(d, i) for i in random.Random(int(time.time() // 600)).sample(range(len(d["pistas"])), n)]
            if vista == "ar":                               # artista -> álbumes (la entrada "todas" es genérica)
                return [self._c_album(d, a) for a in d["artistas"][partes[1]]["albumes"]]
            if vista == "al":
                return self._pistas_album(d, partes[1], d["albumes"][partes[1]]["pistas"], oid)
            if vista == "ge":                               # género -> artistas -> álbumes
                ge = d["generos"][partes[1]]
                if len(partes) > 3 and partes[2] == "ar":
                    return [self._c_album(d, a) for a in ge["artistas"][partes[3]]]
                return [self._c_artista(d, arid, oid=f"v:ge:{partes[1]}:ar:{arid}", albumes=ge["artistas"][arid])
                        for arid in sorted(ge["artistas"], key=lambda k: _orden(d["artistas"][k]["nombre"]))]
            if vista == "de":
                return [self._c_album(d, a, titulo=f"{d['albumes'][a]['anio']} · {d['albumes'][a]['titulo']}")
                        for a in d["decadas"][int(partes[1])]]
        except (KeyError, IndexError, ValueError):
            return None
        return None

    def padre(self, oid):
        m = _RE_OID_DISCO.match(oid)
        if m:
            return m.group(1)
        partes = oid[2:].split(":")
        v = partes[0]
        if self.es_x(v.split("@")[0]) and len(partes) > 1:
            return self._x_padre(oid)
        if v in VISTAS or v.split("@")[0] in VISTAS:
            return "v:" + v.split("@")[0] if "@" in v else "0"
        return {"ar": "v:artistas", "ia": "v:interpretes", "al": "v:albumes", "ge": "v:generos",
                "de": "v:decadas"}.get(v, "0") \
            if len(partes) <= 2 else f"v:{v}:{partes[1]}"

    def metadata(self, oid):
        d = self.asegurar()
        if ":d:" in oid and d is not None:
            r = self._disco(oid)
            if r:
                return r[2], r[0]
        if oid.endswith(":todas"):
            base = oid[:-6]
            t = self.todas(base) if (d or base.startswith("v:carpeta:")) else []
            if not t:
                return None, None
            padre = f"d{base[10:18]}:{base[19:]}" if base.startswith("v:carpeta:") else base
            return self._entrada_todas(base, len(t)), padre
        partes = oid[2:].split(":")
        v, _, letra = partes[0].partition("@")
        try:
            if self.es_x(v) and len(partes) == 1 and not letra:
                desc = ("v", oid, self.defs_x()[v]["nombre"], len(self.hijos(oid) or []), None,
                        "object.container.storageFolder", None, None)
            elif self.es_x(v) and d is not None:
                hijos = self.hijos(oid)
                if hijos is None:
                    return None, None
                vid, valores, _, aid = self._x_ruta(oid)
                # el mismo descriptor que en la lista del padre (título con plantilla, portada, clase)
                desc = next((x for x in (self.hijos(self._x_padre(oid)) or []) if x[0] == "v" and x[1] == oid), None)
                if desc is None:
                    desc = ("v", oid, self._x_titulo(d, oid).rsplit(" / ", 1)[-1], len(hijos), None,
                            C_ALBUM if aid else "object.container.storageFolder", None, None)
            elif v in VISTAS and not letra and len(partes) == 1:
                desc = ("v", oid, VISTAS[v], len(self.hijos(oid) or []), None, "object.container.storageFolder", None, None)
            elif d is None:
                return None, None
            elif letra:
                n = len(self.hijos(oid) or [])
                titulo = T("todos." + v) + f" ({n:,})" if letra == "*" else letra
                desc = ("v", oid, titulo, n, None, "object.container.storageFolder", None, None)
            elif v == "al":
                desc = self._c_album(d, partes[1])
            elif v == "ar" and len(partes) == 2:
                desc = self._c_artista(d, partes[1])
            elif v == "ge" and len(partes) == 4:
                ge = d["generos"][partes[1]]
                desc = self._c_artista(d, partes[3], oid=oid, albumes=ge["artistas"][partes[3]])
            elif v == "ia" and len(partes) == 2:
                desc = self._c_interprete(d, partes[1])
            elif v == "ia" and len(partes) == 4:
                desc = self._c_album_ia(d, partes[1], partes[3])
            else:
                hijos = self.hijos(oid)
                if hijos is None:
                    return None, None
                desc = ("v", oid, self.titulo(oid), len(hijos), None, "object.container.storageFolder", None, None)
        except (KeyError, IndexError, ValueError):
            return None, None
        return desc, self.padre(oid)

    def titulo(self, oid):
        """Nombre legible para el log/dashboard."""
        if oid.endswith(":todas"):
            base = oid[:-6]
            return (f"{T('t.carpeta')} / {base[19:] or '/'}" if base.startswith("v:carpeta:") else self.titulo(base)) + f" / {T('t.todas')}"
        d = self.d
        if oid == "v:playlists" or oid.startswith(("v:pl:", "v:pla:", "v:plal:")):
            return self.app.playlists.titulo(oid)
        m = _RE_OID_DISCO.match(oid)
        if m:
            return f"{self.titulo(m.group(1))} / {T('disco', n=m.group(2))}"
        partes = oid[2:].split(":")
        v = partes[0].split("@")[0]
        try:
            if self.es_x(v):
                return self._x_titulo(d, oid)
            if v in VISTAS:
                return VISTAS[v] + (f" / {partes[0].split('@')[1]}" if "@" in partes[0] else "")
            if v == "al":
                a = d["albumes"][partes[1]]
                return f"{T('t.album')} / {a['artista']} — {a['titulo']}"
            if v == "ar":
                return f"{T('t.artista')} / {d['artistas'][partes[1]]['nombre']}"
            if v == "ia":
                t = f"{T('t.artista')} / {d['interpretes'][partes[1]]['nombre']}"
                return t + (f" / {d['albumes'][partes[3]]['titulo']}" if len(partes) > 3 else "")
            if v == "ge":
                t = f"{T('t.genero')} / {d['generos'][partes[1]]['nombre']}"
                return t + (f" / {d['artistas'][partes[3]]['nombre']}" if len(partes) > 3 else "")
            if v == "de":
                return f"{T('t.decada')} / {partes[1]}s"
        except (KeyError, IndexError, TypeError):
            pass
        return oid

    # ---- búsqueda UPnP (ContentDirectory:Search)
    @staticmethod
    def _normalizados(pistas):
        """Textos normalizados para buscar: una lista por campo, y una sola copia de cada artista/álbum/género."""
        memo = {}
        norm = lambda s: memo[s] if s in memo else memo.setdefault(s, _norm(s))
        return {"title": [_norm(p.m.get("title")) for p in pistas],
                "artist": [norm(p.m.get("artist") or p.m.get("aartist")) for p in pistas],
                "album": [norm(p.m.get("album")) for p in pistas],
                "genre": [norm(p.m.get("genre")) for p in pistas]}

    def buscar(self, criterio, contenedor="0", orden=""):
        """Search UPnP completo: and/or/paréntesis, =, !=, contains, doesNotContain, derivedfrom, exists, <, >…
        Devuelve pistas, álbumes, artistas y géneros según upnp:class (los grupos que no pueden coincidir ni se
        recorren), dentro de ContainerID y ordenados por SortCriteria. Sin tope: se pagina desde una caché."""
        d = self.asegurar()
        if d is None:
            return []
        ck = (criterio, contenedor, orden, d.get("gen"))
        memo = self._busquedas
        with self._busq_lock:
            if ck in memo:
                memo.move_to_end(ck)
                return memo[ck]
        try:
            ast = _parse_busqueda(criterio)
        except ValueError:
            ast = None
        if ast is None:
            return []
        if "norm" not in d:
            d["norm"] = self._normalizados(d["pistas"])
        alcance = self._alcance(d, contenedor)
        out = []
        for grupo in ("artista", "album", "genero", "pista"):
            f = _compilar_busqueda(ast, grupo, self._props(d, grupo))
            if f is False:
                continue
            idxs = self._candidatos(d, grupo, alcance)
            sel = idxs if f is True else [x for x in idxs if f(x)]
            if not sel:
                continue
            sel = self._ordenar(d, grupo, sel, orden)
            if grupo == "pista":
                out += [self._t(d, i) for i in sel]
            elif grupo == "album":
                out += [self._c_album(d, k) for k in sel]
            elif grupo == "artista":
                out += [self._c_artista(d, k) for k in sel]
            else:
                out += [self._c_genero(d, k) for k in sel]
        with self._busq_lock:
            memo[ck] = out
            while len(memo) > 8:
                memo.popitem(last=False)
        return out

    def _c_genero(self, d, g):
        ge = d["generos"][g]
        return ("v", f"v:ge:{g}", self.nc(ge["nombre"], self.n_albumes(d, [a for l in ge["artistas"].values() for a in l]),
                                          len(ge["artistas"])),
                len(ge["artistas"]) + 1, self._art_album(d["albumes"][next(iter(ge["artistas"].values()))[0]]),
                C_GENERO, None, None)

    def _alcance(self, d, contenedor):
        """ContainerID -> conjunto de índices de pista dentro (None = toda la biblioteca)."""
        if contenedor in ("0", "", None) or contenedor.startswith("v:") and re.fullmatch(r"v:\w+", contenedor):
            return None
        m = re.fullmatch(r"d([0-9a-f]{8}):(.*)", contenedor, re.S)
        if m:
            key, rel = m.groups()
            pref = rel + "/" if rel else ""
            return {i for i, p in enumerate(d["pistas"]) if p.key == key and p.rel.startswith(pref)}
        if contenedor.startswith("v:"):
            pistas = self._calcular_todas(d, contenedor)
            return {i for i in (self._i_pista(d, t[1], t[2]) for t in pistas) if i is not None}
        return None

    def _i_pista(self, d, key, rel):
        pr = d.get("por_ruta")
        if pr is None:
            pr = d["por_ruta"] = {(p.key, p.rel): i for i, p in enumerate(d["pistas"])}
        return pr.get((key, rel))

    def _candidatos(self, d, grupo, alcance):
        if grupo == "pista":
            return sorted(alcance) if alcance is not None else range(len(d["pistas"]))
        if grupo == "album":
            if alcance is None:
                return list(d["orden_albumes"])
            dentro = {d["pistas"][i].al for i in alcance}
            return [k for k in d["orden_albumes"] if k in dentro]
        if alcance is not None:
            return []                                    # artistas/géneros: solo en toda la biblioteca
        return list(d["orden_artistas"] if grupo == "artista" else d["orden_generos"])

    def _props(self, d, grupo):
        """Propiedad UPnP -> función(índice) que da su texto normalizado (o un valor fijo), por grupo."""
        if grupo == "pista":
            nm, ps = d["norm"], d["pistas"]
            return {"upnp:class": "object.item.audioitem.musictrack",
                    "dc:title": nm["title"].__getitem__, "upnp:artist": nm["artist"].__getitem__,
                    "dc:creator": nm["artist"].__getitem__, "upnp:album": nm["album"].__getitem__,
                    "upnp:genre": nm["genre"].__getitem__,
                    "dc:date": lambda i: str(getattr(ps[i].m, "year", "") or ""),
                    "upnp:originaltracknumber": lambda i: str(getattr(ps[i].m, "track", "") or "")}
        if grupo == "album":
            al = d["albumes"]
            return {"upnp:class": "object.container.album.musicalbum",
                    "dc:title": lambda k: _norm(al[k]["titulo"]), "upnp:album": lambda k: _norm(al[k]["titulo"]),
                    "upnp:artist": lambda k: _norm(al[k]["artista"]), "dc:creator": lambda k: _norm(al[k]["artista"]),
                    "upnp:genre": lambda k: " ".join(sorted(_norm(g) for g in al[k].get("generos") or ())),
                    "dc:date": lambda k: str(al[k]["anio"] or "")}
        if grupo == "artista":
            ar = d["artistas"]
            nombre = lambda k: _norm(ar[k]["nombre"])
            return {"upnp:class": "object.container.person.musicartist",
                    "dc:title": nombre, "upnp:artist": nombre, "dc:creator": nombre}
        ge = d["generos"]
        nombre = lambda k: _norm(ge[k]["nombre"])
        return {"upnp:class": "object.container.genre.musicgenre", "dc:title": nombre, "upnp:genre": nombre}

    def _ordenar(self, d, grupo, sel, orden):
        """SortCriteria: '+dc:title,-dc:date,…' (sin orden: el natural de cada grupo)."""
        claves = [(c[0] != "-", c.lstrip("+-").strip().lower()) for c in (orden or "").split(",") if c.strip()]
        if not claves:
            return sel
        props = self._props(d, grupo)
        sel = list(sel)
        for asc, prop in reversed(claves):              # orden estable: de la última clave a la primera
            f = props.get(prop)
            if f is None or isinstance(f, str):
                continue
            num = prop in ("dc:date", "upnp:originaltracknumber")
            sel.sort(key=(lambda x: int(f(x) or 0)) if num else f, reverse=not asc)
        return sel


# ═══════════════════════════════════════════════════════════════════════════ UPnP: XML

# ---- Search UPnP: gramática de ContentDirectory (criterios con and/or, paréntesis y operadores)
_RE_TOKEN_BUSQ = re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+)')
_OPS_BUSQ = {"=", "!=", "<", "<=", ">", ">=", "contains", "doesnotcontain", "derivedfrom", "exists"}


def _parse_busqueda(txt):
    """'upnp:class derivedfrom "object.item" and (dc:title contains "x" or upnp:artist = "y")' -> árbol.
    '*' o vacío = todo. ValueError si está mal formado."""
    txt = (txt or "").strip()
    if txt in ("", "*"):
        return True
    toks = _RE_TOKEN_BUSQ.findall(txt)
    pos = 0

    def ver():
        return toks[pos] if pos < len(toks) else None

    def tomar():
        nonlocal pos
        if pos >= len(toks):
            raise ValueError("criterio incompleto")
        pos += 1
        return toks[pos - 1]

    def expr():
        a = termino()
        while (ver() or "").lower() == "or":
            tomar()
            a = ("or", a, termino())
        return a

    def termino():
        a = factor()
        while (ver() or "").lower() == "and":
            tomar()
            a = ("and", a, factor())
        return a

    def factor():
        t = tomar()
        if t == "(":
            a = expr()
            if tomar() != ")":
                raise ValueError("falta )")
            return a
        prop, op = t.lower(), tomar().lower()
        if op not in _OPS_BUSQ:
            raise ValueError(f"operador {op}")
        v = tomar()
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return ("cmp", prop, op, v)

    arbol = expr()
    if pos != len(toks):
        raise ValueError("sobra texto")
    return arbol


def _compilar_busqueda(nodo, grupo, props):
    """Árbol -> True / False (constante para todo el grupo) o función(índice) -> bool."""
    if nodo is True:
        return True
    if nodo[0] in ("and", "or"):
        a, b = _compilar_busqueda(nodo[1], grupo, props), _compilar_busqueda(nodo[2], grupo, props)
        if nodo[0] == "and":
            if a is False or b is False:
                return False
            if a is True:
                return b
            if b is True:
                return a
            return lambda x: a(x) and b(x)
        if a is True or b is True:
            return True
        if a is False:
            return b
        if b is False:
            return a
        return lambda x: a(x) or b(x)
    _, prop, op, v = nodo
    f = props.get(prop)
    if f is None:                                        # propiedad que este grupo no tiene: no existe, no cumple
        return op in ("!=", "doesnotcontain") or (op == "exists" and v.lower() == "false")
    if op == "exists":
        quiere = v.lower() == "true"
        return (bool(f) == quiere) if isinstance(f, str) else (lambda x: bool(f(x)) == quiere)
    if prop == "upnp:class":
        val, v = f, v.lower()
        r = (val.startswith(v) if op == "derivedfrom" else val == v if op == "=" else val != v if op == "!="
             else (v in val) if op == "contains" else (v not in val) if op == "doesnotcontain" else False)
        return bool(r)
    vn = _norm(v)
    if isinstance(f, str):
        f = (lambda c: (lambda x: c))(_norm(f))
    if op == "contains":
        return lambda x: vn in f(x)
    if op == "doesnotcontain":
        return lambda x: vn not in f(x)
    if op == "=":
        return lambda x: f(x) == vn
    if op == "!=":
        return lambda x: f(x) != vn
    if op == "derivedfrom":
        return lambda x: f(x).startswith(vn)
    def comparar(x, op=op):
        a = f(x)
        try:
            a, b = float(a), float(vn)
        except ValueError:
            b = vn
        return (a < b) if op == "<" else (a <= b) if op == "<=" else (a > b) if op == ">" else (a >= b)
    return comparar


def device_xml(nombre, udn):
    def svc(stype, sid, short):
        return (f"<service><serviceType>{stype}</serviceType><serviceId>urn:upnp-org:serviceId:{sid}</serviceId>"
                f"<SCPDURL>/{short}.xml</SCPDURL><controlURL>/ctl/{short}</controlURL>"
                f"<eventSubURL>/evt/{short}</eventSubURL></service>")
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
            f"<deviceType>{DEV_TYPE}</deviceType><friendlyName>{esc(nombre)}</friendlyName>"
            "<manufacturer>SupaVic</manufacturer><modelName>SupaVic DLNA</modelName>"
            f"<modelDescription>{esc(T('descripcion'))}</modelDescription><modelNumber>{VERSION}</modelNumber>"
            f"<serialNumber>{udn[-12:]}</serialNumber><UDN>uuid:{udn}</UDN>"
            "<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC><presentationURL>/</presentationURL><serviceList>"
            + svc(NS_CDS, "ContentDirectory", "cds") + svc(NS_CMS, "ConnectionManager", "cms")
            + "</serviceList></device></root>")


def scpd(acciones, variables):
    a = ""
    for nombre, args in acciones:
        a += f"<action><name>{nombre}</name><argumentList>"
        for an, d, var in args:
            a += (f"<argument><name>{an}</name><direction>{d}</direction>"
                  f"<relatedStateVariable>{var}</relatedStateVariable></argument>")
        a += "</argumentList></action>"
    v = ""
    for nombre, tipo, eventos, permitidos in variables:
        v += f'<stateVariable sendEvents="{"yes" if eventos else "no"}"><name>{nombre}</name><dataType>{tipo}</dataType>'
        if permitidos:
            v += "<allowedValueList>" + "".join(f"<allowedValue>{x}</allowedValue>" for x in permitidos) + "</allowedValueList>"
        v += "</stateVariable>"
    return ('<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            f"<actionList>{a}</actionList><serviceStateTable>{v}</serviceStateTable></scpd>")


CDS_SCPD = scpd(
    [("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
     ("Search", [("ContainerID", "in", "A_ARG_TYPE_ObjectID"), ("SearchCriteria", "in", "A_ARG_TYPE_SearchCriteria"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
     ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID")]),
     ("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities")]),
     ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities")])],
    [("A_ARG_TYPE_ObjectID", "string", False, None), ("A_ARG_TYPE_Result", "string", False, None),
     ("A_ARG_TYPE_BrowseFlag", "string", False, ["BrowseMetadata", "BrowseDirectChildren"]),
     ("A_ARG_TYPE_Filter", "string", False, None), ("A_ARG_TYPE_SortCriteria", "string", False, None),
     ("A_ARG_TYPE_SearchCriteria", "string", False, None),
     ("A_ARG_TYPE_Index", "ui4", False, None), ("A_ARG_TYPE_Count", "ui4", False, None),
     ("A_ARG_TYPE_UpdateID", "ui4", False, None), ("SearchCapabilities", "string", False, None),
     ("SortCapabilities", "string", False, None), ("SystemUpdateID", "ui4", True, None)])

CMS_SCPD = scpd(
    [("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")]),
     ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
     ("GetCurrentConnectionInfo", [
         ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"), ("RcsID", "out", "A_ARG_TYPE_RcsID"),
         ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"), ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
         ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
         ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"), ("Direction", "out", "A_ARG_TYPE_Direction"),
         ("Status", "out", "A_ARG_TYPE_ConnectionStatus")])],
    [("SourceProtocolInfo", "string", True, None), ("SinkProtocolInfo", "string", True, None),
     ("CurrentConnectionIDs", "string", True, None),
     ("A_ARG_TYPE_ConnectionStatus", "string", False, ["OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown"]),
     ("A_ARG_TYPE_ConnectionManager", "string", False, None),
     ("A_ARG_TYPE_Direction", "string", False, ["Input", "Output"]),
     ("A_ARG_TYPE_ProtocolInfo", "string", False, None), ("A_ARG_TYPE_ConnectionID", "i4", False, None),
     ("A_ARG_TYPE_AVTransportID", "i4", False, None), ("A_ARG_TYPE_RcsID", "i4", False, None)])

SOURCE_PROTOCOLS = ",".join(protocol_info(m) for m in sorted(set(AUDIO.values())))
DIDL_OPEN = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">')


def soap_ok(service, accion, args):
    body = "".join(f"<{k}>{esc(v)}</{k}>" for k, v in args)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{accion}Response xmlns:u="{service}">{body}</u:{accion}Response></s:Body></s:Envelope>')


def soap_fault(code, desc):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
            "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
            f'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode>'
            f"<errorDescription>{esc(desc)}</errorDescription></UPnPError></detail>"
            "</s:Fault></s:Body></s:Envelope>")


def localname(tag):
    return tag.rsplit("}", 1)[-1]


# ═══════════════════════════════════════════════════════════════════════════ red

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


def ip_hacia(destino):
    """IP local que el sistema usaría para llegar a 'destino' (correcta aunque haya VPN / Hyper-V)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((destino, 1900))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# ---- contraseña opcional del panel (datos/clave.txt; borrar el archivo la quita)
RUTA_CLAVE = DIR_DATOS / "clave.txt"
COOKIE_SESION = "supavic_s"


def leer_clave():
    try:
        c = RUTA_CLAVE.read_text(encoding="ascii").strip()
        return c if c.startswith("pbkdf2_sha256$") else ""
    except OSError:
        return ""


def hash_clave(clave, sal=None, it=240_000):
    sal = sal or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), bytes.fromhex(sal), it).hex()
    return f"pbkdf2_sha256${it}${sal}${h}"


def verificar_clave(clave, guardada):
    try:
        _, it, sal, h = guardada.split("$")
        return hmac.compare_digest(hash_clave(clave, sal, int(it)).rsplit("$", 1)[1], h)
    except (ValueError, TypeError):
        return False


def token_sesion(guardada):
    """Valor de la cookie de sesión: deriva del hash guardado (cambiar la contraseña cierra todas las sesiones).
    El dashboard (otro proceso) lo calcula igual leyendo datos/clave.txt."""
    return hmac.new(guardada.encode("ascii"), b"supavic-sesion", hashlib.sha256).hexdigest()


def es_local(ip):
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def cliente_permitido(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════════════════ eventos

class Eventos:
    """Bitácora JSONL de actividad para el dashboard. Rota a 5 MB (conserva 5).
    El lector abre/lee/cierra en cada consulta, así que en Windows el renombrado no choca;
    si chocara, se reintenta en la siguiente escritura."""
    MAX = 5_000_000
    COPIAS = 5

    def __init__(self, ruta):
        self.ruta = ruta
        self.lock = threading.Lock()
        self.ultimo_desc = {}

    def emitir(self, tipo, **datos):
        datos = {"t": round(time.time(), 3), "tipo": tipo, **datos}
        linea = json.dumps(datos, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            try:
                if self.ruta.exists() and self.ruta.stat().st_size > self.MAX:
                    self._rotar()
                with open(self.ruta, "a", encoding="utf-8") as fh:
                    fh.write(linea)
            except OSError as e:
                log.debug("Could not write event: %s", e)

    def _rotar(self):
        try:
            for i in range(self.COPIAS - 1, 0, -1):
                a = self.ruta.with_name(f"{self.ruta.name}.{i}")
                if a.exists():
                    os.replace(a, self.ruta.with_name(f"{self.ruta.name}.{i + 1}"))
            os.replace(self.ruta, self.ruta.with_name(f"{self.ruta.name}.1"))
        except OSError as e:
            log.debug("Event log rotation postponed: %s", e)

    def descubrimiento(self, ip, ua):
        ahora = time.time()
        if ahora - self.ultimo_desc.get(ip, 0) > 300:          # no llenar el log con re-anuncios
            self.ultimo_desc[ip] = ahora
            self.emitir("descubrimiento", ip=ip, ua=(ua or "")[:120])


# ═══════════════════════════════════════════════════════════════════════════ aplicación

class App:
    def __init__(self):
        self.cfg = cargar_config()
        self.inicio = time.time()
        # (el prefijo histórico se conserva: cambiarlo haría que los reproductores vean un servidor nuevo)
        self.udn = obtener_udn()
        self.update_id = int(time.time()) % 100000
        self.dir_cache = dir_cache(self.cfg)
        try:
            self.dir_cache.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("Could not use the cache at %s (%s); using %s", self.dir_cache, e, DIR_DATOS)
            self.dir_cache = DIR_DATOS
        self.dir_portadas = self.dir_cache / "portadas"
        self.migrar_cache()
        self.tags = CacheTags(self.dir_cache / "cache.db")
        self.tags.al_mover = self._historial_movidos
        self.aplicar_portadas()
        self.playlists = Playlists(self)
        self.bib = Biblioteca(self)
        self.art_mem = OrderedDict()                      # hash -> (mime, bytes): las más pedidas, en memoria
        self.art_map = OrderedDict()                      # clave de fuente -> hash (espejo parcial de cache.db)
        self.art_lock = threading.Lock()
        self._extrayendo = {}                             # carpeta -> Event: extrayendo una embebida de ese álbum
        self._h_en_disco = set()                          # hashes de portada ya vistos en disco
        self._hermanas_hechas = OrderedDict()             # (carpeta, fecha, hash) ya revisados en _mapear_hermanas
        self.subs = {}
        self.subs_lock = threading.Lock()
        self.clientes = {}
        self.cli_lock = threading.Lock()
        self.httpd = None
        self.ssdp = None
        self.eventos = Eventos(RUTA_EVENTOS)
        self.repro = Reproducciones(DIR_DATOS / "reproducciones.db")
        self.repro.migrar_claves({clave_raiz_vieja(r): k for k, (r, _) in self.bib.raices.items()})
        self.indice = Indice(self)
        self.ultima_peticion = 0.0                        # última navegación real (el índice le cede el paso)
        self.ultima_nav = time.time()
        self.ultima_transfer = 0.0
        self.transfers = 0
        self.transfer_lock = threading.Lock()             # += no es atómico entre hilos (varias conexiones del WiiM)
        self.clave = leer_clave()
        self.login_fallos = {}
        # actualización a una versión con playlists: recorrer las carpetas pronto para encontrarlas (sin esperar los
        # minutos de inactividad); cualquier navegación lo pausa como siempre
        self.forzar_precarga = bool(self.playlists.nueva and self.tags.total())
        self.precarga = None
        self.precarga_img = None                          # imágenes del siguiente nivel (se arranca en main)

    def migrar_cache(self):
        """Si la caché cambió de carpeta (p. ej. de datos\\ al NVMe) y la nueva está vacía, mueve la anterior."""
        if self.dir_cache.resolve() == DIR_DATOS.resolve() or (self.dir_cache / "cache.db").exists():
            return
        if not (DIR_DATOS / "cache.db").exists():
            return
        import shutil
        try:
            for n in ("cache.db", "cache.db-wal", "cache.db-shm"):
                if (DIR_DATOS / n).exists():
                    shutil.move(str(DIR_DATOS / n), str(self.dir_cache / n))
            if (DIR_DATOS / "portadas").is_dir() and not self.dir_portadas.exists():
                shutil.move(str(DIR_DATOS / "portadas"), str(self.dir_portadas))
            log.info("Previous cache moved from %s to %s", DIR_DATOS, self.dir_cache)
        except OSError as e:
            log.warning("Could not move the previous cache (%s); starting a new one", e)

    def dashboard_activo(self):
        """¿Responde el dashboard? (para el enlace del panel; cacheado 10 s)."""
        c = getattr(self, "_dash", None)
        if c and time.monotonic() - c[0] < 10:
            return c[1]
        try:
            with socket.create_connection(("127.0.0.1", self.cfg["dashboard"]["puerto"]), timeout=0.2):
                ok = True
        except OSError:
            ok = False
        self._dash = (time.monotonic(), ok)
        return ok

    def aplicar_portadas(self):
        p = self.cfg["portadas"]
        self.art_profile = ' dlna:profileID="JPEG_SM"' if Image and 0 < p["tamano"] <= 640 else ""

    @property
    def puerto(self):
        return self.cfg["servidor"]["puerto"]

    # ---- actividad de clientes (para el panel)
    def registrar(self, ip, ua, que):
        with self.cli_lock:
            c = self.clientes.setdefault(ip, {"ip": ip, "ua": "", "ultimo": 0, "accion": "", "sonando": ""})
            c["ultimo"] = time.time()
            if ua:
                c["ua"] = ua[:120]
            c["accion"] = que
            if que.startswith("▶ "):
                c["sonando"] = que[2:]

    # ---- portadas
    # ---- actividad (para pausar la precarga)
    def marcar_actividad(self):
        self.ultima_nav = self.ultima_peticion = time.time()
        self.forzar_precarga = False

    def inactivo(self):
        if self.transfers > 0 or time.time() - self.ultima_transfer < 10:
            return False
        if self.forzar_precarga:
            return True
        return time.time() - self.ultima_nav >= self.cfg["precarga"]["inactividad_min"] * 60

    def clave_portada(self, src):
        """Identidad de la FUENTE (no de la imagen): tipo + nombre de archivo + fecha + tamaño.
        Sin la carpeta: mover un álbum conserva sus portadas ya generadas."""
        try:
            st = os.stat(src[1])
        except OSError:
            return None
        nombre = os.path.basename(src[1]).casefold()
        return hashlib.sha1(f"{src[0]}|{nombre}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8", "replace")).hexdigest()

    def clave_fuente(self, tipo, ruta, mt_ns, tam):
        """Lo mismo que clave_portada, con fecha y tamaño ya conocidos (del listado): sin tocar el disco."""
        nombre = os.path.basename(ruta).casefold()
        return hashlib.sha1(f"{tipo}|{nombre}|{mt_ns}|{tam}".encode("utf-8", "replace")).hexdigest()

    def _hash_imagen(self, data):
        """Nombre en disco = hash del CONTENIDO + opciones: 12 pistas con la misma imagen embebida = 1 archivo."""
        p = self.cfg["portadas"]
        return hashlib.sha1(data + f"|{p['tamano']}|{p['recortar_cuadrado']}".encode()).hexdigest()[:24]

    def _mapa(self, clave):
        with self.art_lock:
            h = self.art_map.get(clave)
            if h is not None:
                self.art_map.move_to_end(clave)
                return h
        h = self.tags.mapa_portada(clave)
        if h is not None:
            self._recordar_mapa(clave, h)
        return h

    def _recordar_mapa(self, clave, h):
        with self.art_lock:
            self.art_map[clave] = h
            while len(self.art_map) > 16384:
                self.art_map.popitem(last=False)

    def _archivo_portada(self, h):
        return self.dir_portadas / h[:2] / f"{h}.jpg"

    def portada_existe(self, h):
        """¿Está el JPEG de este hash en disco? Se comprueba una vez por hash (un álbum = un hash)."""
        if h in self._h_en_disco:
            return True
        if self._archivo_portada(h).exists():
            self._h_en_disco.add(h)
            return True
        return False

    def hash_portada(self, src):
        """Hash de la imagen ya conocido para una fuente (None = aún no se ha procesado; "" = no tiene)."""
        clave = self.clave_portada(src) if src else None
        return self._mapa(clave) if clave else None

    def fuente_url_portada(self, resto):
        """'/art/<resto>' -> fuente de la imagen (lo usan el servidor y la precarga de imágenes)."""
        r = re.fullmatch(r"r/([0-9a-f]{10})/([0-9a-f]{10})\.jpg", resto)
        if r:                                               # artista: artist.jpg › foto embebida › su primer álbum
            return self.indice.fuente_artista(r.group(1), r.group(2))
        m = re.fullmatch(r"([dtap])/([0-9a-f]{8})/(.*)", resto, re.S)
        if not m:
            return None
        tipo, key, rel = m.groups()
        if tipo == "t" and rel.endswith(".jpg"):
            ruta = self.bib.resolver(key, rel[:-4])
            return self.bib.fuente_portada_pista(ruta) if ruta else None
        if tipo == "p" and rel.endswith(".jpg"):           # playlist: <nombre>.jpg o la de su primera pista
            return self.playlists.fuente_portada(key, rel[:-4])
        if rel.endswith("cover.jpg"):                      # d = carpeta (busca en subcarpetas), a = solo esa carpeta
            ruta = self.bib.resolver(key, rel[:-9].rstrip("/"))
            return self.bib.fuente_portada_carpeta(ruta, max_prof=None if tipo == "d" else 0) if ruta else None
        return None

    def portada_en_disco(self, src):
        h = self.hash_portada(src)
        return h == "" or (bool(h) and self._archivo_portada(h).exists())

    def portada(self, src, guardar_en_memoria=True):
        if not src:
            return None
        clave = self.clave_portada(src)
        if clave is None:
            return None
        h = self._mapa(clave)
        if h == "":
            return None                                   # ya se sabe que esta fuente no tiene imagen
        if h:
            with self.art_lock:
                if h in self.art_mem:
                    self.art_mem.move_to_end(h)
                    return self.art_mem[h]
            archivo = self._archivo_portada(h)
            if archivo.exists():
                return self._en_memoria(h, ("image/jpeg", archivo.read_bytes()), guardar_en_memoria)
        if src[0] == "emb" and self.bib.modo_album(os.path.dirname(src[1])):
            # álbum: una pista se extrae y las demás con la misma imagen quedan asignadas. Carpetas mixtas
            # (playlists): cada pista por su cuenta, en paralelo y sin revisar a las demás
            esperar = self._turno_carpeta(os.path.dirname(src[1]))
            if esperar:                                   # otra pista de este álbum se está extrayendo
                esperar.wait(3)
                h = self._mapa(clave)
                if h:
                    archivo = self._archivo_portada(h)
                    if archivo.exists():
                        return self._en_memoria(h, ("image/jpeg", archivo.read_bytes()), guardar_en_memoria)
            else:
                try:
                    return self._portada_emb(src, clave, guardar_en_memoria)
                finally:
                    self._fin_turno(os.path.dirname(src[1]))
        return self._extraer_portada(src, clave, guardar_en_memoria)

    def _turno_carpeta(self, carpeta):
        """None = esta petición extrae (y mapea las hermanas); si no, el Event a esperar."""
        with self.art_lock:
            ev = self._extrayendo.get(carpeta)
            if ev is None:
                self._extrayendo[carpeta] = threading.Event()
            return ev

    def _fin_turno(self, carpeta):
        with self.art_lock:
            ev = self._extrayendo.pop(carpeta, None)
        if ev:
            ev.set()

    def _portada_emb(self, src, clave, guardar_en_memoria):
        """Extrae la embebida de una pista y asigna el mismo resultado a las pistas de la carpeta que traen la
        misma imagen (mismo tipo y largo exacto, visto en sus encabezados): el resto del álbum sale al instante."""
        res = self._extraer_portada(src, clave, guardar_en_memoria)
        h = self._mapa(clave)
        if res and h and src[1][-5:].lower() == ".flac":
            try:
                self._mapear_hermanas(src[1], h)
            except Exception as e:
                log.debug("Cover siblings %s: %s", src[1], e)
        return res

    def _mapear_hermanas(self, ruta, h):
        carpeta = os.path.dirname(ruta)
        try:
            ck_dir = (carpeta, os.stat(carpeta).st_mtime_ns, h)
        except OSError:
            return
        with self.art_lock:                               # una vez por álbum (mientras la carpeta no cambie)
            if ck_dir in self._hermanas_hechas:
                return
            self._hermanas_hechas[ck_dir] = True
            while len(self._hermanas_hechas) > 4096:
                self._hermanas_hechas.popitem(last=False)
        propia = _elegida(firmas_imagen_flac(ruta) or {})
        if not propia:
            return
        l = self.bib.listar(carpeta, ttl=None)
        n = 0
        for nombre, mt, tam in (l[1] if l else ()):
            r = os.path.join(carpeta, nombre)
            if r == ruta or nombre[-5:].lower() != ".flac":
                continue
            ck = self.clave_fuente("emb", r, mt, tam)
            if self._mapa(ck) is not None:
                continue
            if _elegida(firmas_imagen_flac(r) or {}) == propia:
                self.tags.guardar_mapa_portada(ck, h)
                self._recordar_mapa(ck, h)
                n += 1
        if n:
            log.debug("Cover of %s reused for %d tracks", carpeta, n)

    def _extraer_portada(self, src, clave, guardar_en_memoria):
        p = self.cfg["portadas"]
        try:
            data = extraer_imagen(src)
        except Exception as e:
            log.debug("Cover %s: %s", src, e)
            return None                                   # error de lectura: se reintenta la próxima vez
        if not data:
            self.tags.guardar_mapa_portada(clave, "")
            self._recordar_mapa(clave, "")
            return None
        h = self._hash_imagen(data)
        archivo = self._archivo_portada(h)
        if archivo.exists():                              # la misma imagen ya se generó para otra pista/carpeta
            res = ("image/jpeg", archivo.read_bytes())
        else:
            res = procesar_imagen(data, p["tamano"], p["recortar_cuadrado"], src[1])
            if not res or res[0] != "image/jpeg":
                return res                                # sin Pillow: tal cual, sin caché en disco
            archivo.parent.mkdir(parents=True, exist_ok=True)
            tmp = archivo.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(res[1])
            os.replace(tmp, archivo)
        self.tags.guardar_mapa_portada(clave, h)
        self._recordar_mapa(clave, h)
        return self._en_memoria(h, res, guardar_en_memoria)

    def _en_memoria(self, h, res, guardar):
        if guardar:
            with self.art_lock:
                self.art_mem[h] = res
                while len(self.art_mem) > 256:
                    self.art_mem.popitem(last=False)
        return res

    def limpiar_cache(self):
        self._h_en_disco.clear()
        n = 0
        if self.dir_portadas.exists():
            for f in self.dir_portadas.rglob("*.jpg"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        self.tags.limpiar()
        with self.art_lock:
            self.art_mem.clear()
            self.art_map.clear()
        self.bib.configurar(self.cfg)
        self.contenido_cambio()
        return n

    def estadisticas_portadas(self):
        c = getattr(self, "_est_port", None)
        if c and time.monotonic() - c[0] < 60:
            return c[1]
        r = self._estadisticas_portadas()
        self._est_port = (time.monotonic(), r)
        return r

    def _estadisticas_portadas(self):
        n = tam = 0
        if self.dir_portadas.exists():
            for f in self.dir_portadas.rglob("*.jpg"):
                n += 1
                try:
                    tam += f.stat().st_size
                except OSError:
                    pass
        return n, tam

    # ---- configuración en caliente
    def actualizar_config(self, nueva):
        cfg, errores, avisos = validar_config(nueva)
        if errores:
            return {"ok": False, "errores": errores, "avisos": avisos}
        reiniciar = cfg["servidor"] != self.cfg["servidor"] or cfg["cache"] != self.cfg["cache"]
        cambio_portadas = cfg["portadas"] != self.cfg["portadas"]
        cambio_vistas = (cfg["vistas"] != self.cfg["vistas"]
                         or [cfg["musica"][k] for k in ("carpetas", "sin_indexar")]
                         != [self.cfg["musica"].get(k) for k in ("carpetas", "sin_indexar")])
        self.cfg_previo_portadas = dict(self.cfg["portadas"])
        self.cfg_previo_musica = list(self.cfg["musica"].get("sin_indexar") or ())
        guardar_config(cfg)
        self.cfg = cfg
        self.cfg_gen = getattr(self, "cfg_gen", 0) + 1     # invalida las listas cacheadas
        self.bib.configurar(cfg)
        if cambio_portadas:
            self.aplicar_portadas()
            viejo = self.cfg_previo_portadas
            with self.art_lock:
                self.art_mem.clear()
                self.art_map.clear()
            if (viejo.get("tamano"), viejo.get("recortar_cuadrado")) != (cfg["portadas"]["tamano"],
                                                                         cfg["portadas"]["recortar_cuadrado"]):
                with self.tags.lock:                      # otro tamaño/recorte: los hashes cambian
                    self.tags.db.execute("DELETE FROM portadas")
                self._h_en_disco.clear()
            with self.bib.dir_lock:
                self.bib.art_src_cache.clear()
            self.indice._x_cache = OrderedDict()
        if cambio_vistas:
            self.indice.invalidar()
        if set(self.cfg_previo_musica) - set(cfg["musica"]["sin_indexar"]):
            self.forzar_precarga = True                   # carpeta vuelta a indexar: leer lo que falte
        self.contenido_cambio()
        log.info("Settings saved%s", " (restarting)" if reiniciar else "")
        self.eventos.emitir("config", reiniciar=reiniciar)
        if self.precarga:
            self.precarga.ultimo_completo = 0                # ajustes nuevos: permitir otra pasada
        if reiniciar:
            threading.Timer(0.8, self.reiniciar).start()
        return {"ok": True, "avisos": avisos, "reiniciar": reiniciar}

    def cambio_detectado(self):
        """Agrupa cambios detectados en el disco (2 s) en un solo aviso a los controles."""
        with self.subs_lock:
            if getattr(self, "_cambio_pendiente", False):
                return
            self._cambio_pendiente = True

        def aplicar():
            self._cambio_pendiente = False
            self.contenido_cambio()
        threading.Timer(2.0, aplicar).start()

    def contenido_cambio(self):
        self.update_id += 1
        self.notificar_suscriptores()

    def reiniciar(self):
        log.info("Restarting process…")
        flags = 0x00000008 | 0x00000200 if ES_WINDOWS else 0      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            if self.ssdp:
                self.ssdp.notificar("ssdp:byebye")
                self.ssdp.sock.close()
            if self.httpd:
                self.httpd.server_close()
        except Exception:
            pass
        try:
            RUTA_PID.unlink()
        except OSError:
            pass
        if callable(AL_SALIR):
            try:
                AL_SALIR()
            except Exception:
                pass
        cmd = RELANZAR or [sys.executable, str(Path(__file__).resolve())]
        if not ES_WINDOWS:
            # Linux/macOS/Docker: reemplazar este mismo proceso (mismo PID). Con Popen + salir, en un contenedor
            # el proceso principal terminaba y el contenedor entero se detenía.
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                os.execv(cmd[0], cmd)
            except OSError as e:
                log.error("Restart via exec failed (%s); starting a new process", e)
        subprocess.Popen(cmd, cwd=str(DIR_BASE), creationflags=flags, close_fds=True)
        os._exit(0)

    # ---- GENA (eventos UPnP)
    def suscribir(self, callback, servicio, timeout):
        urls = re.findall(r"<([^>]+)>", callback or "")
        sid = f"uuid:{uuid.uuid4()}"
        with self.subs_lock:
            self.subs[sid] = [urls, servicio, time.time() + timeout, 0]
        threading.Thread(target=self._evento, args=(sid,), daemon=True).start()
        return sid

    def renovar(self, sid, timeout):
        with self.subs_lock:
            if sid not in self.subs:
                return False
            self.subs[sid][2] = time.time() + timeout
            return True

    def desuscribir(self, sid):
        with self.subs_lock:
            return self.subs.pop(sid, None) is not None

    def notificar_suscriptores(self):
        with self.subs_lock:
            sids = list(self.subs)
        for sid in sids:
            threading.Thread(target=self._evento, args=(sid,), daemon=True).start()

    def _evento(self, sid):
        time.sleep(0.2)
        with self.subs_lock:
            sub = self.subs.get(sid)
            if not sub or sub[2] < time.time():
                self.subs.pop(sid, None)
                return
            urls, servicio, _, seq = sub
            sub[3] += 1
        if servicio == "cds":
            props = [("SystemUpdateID", self.update_id), ("ContainerUpdateIDs", "")]
        else:
            props = [("SourceProtocolInfo", SOURCE_PROTOCOLS), ("SinkProtocolInfo", ""), ("CurrentConnectionIDs", "0")]
        body = ('<?xml version="1.0" encoding="utf-8"?><e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                + "".join(f"<e:property><{k}>{esc(v)}</{k}></e:property>" for k, v in props)
                + "</e:propertyset>").encode()
        for url in urls:
            try:
                u = urlparse(url)
                c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=5)
                c.request("NOTIFY", u.path or "/", body, {
                    "HOST": f"{u.hostname}:{u.port or 80}", "CONTENT-TYPE": 'text/xml; charset="utf-8"',
                    "NT": "upnp:event", "NTS": "upnp:propchange", "SID": sid, "SEQ": str(seq)})
                c.getresponse().read()
                c.close()
                return
            except Exception as e:
                log.debug("Event to %s failed: %s", url, e)

    def nombre_objeto(self, oid):
        """Ruta legible de un ObjectID para el dashboard: 'Discografia_FLAC/Bill Evans'."""
        if oid == "0":
            return "/"
        if oid == "v:carpetas":
            return T("carpetas")
        if oid.startswith("v:"):
            return self.indice.titulo(oid)
        m = re.fullmatch(r"[dt]([0-9a-f]{8}):(.*)", oid, re.S)
        if not m or m.group(1) not in self.bib.raices:
            return oid
        raiz = self.bib.raices[m.group(1)][1]
        return f"{raiz}/{m.group(2)}" if m.group(2) else raiz

    def _historial_movidos(self, pares):
        for viejo, nuevo in pares:
            a, b = self.bib.clave_rel(viejo), self.bib.clave_rel(nuevo)
            if a and b:
                try:
                    self.repro.mover(a, b)
                except Exception:
                    log.debug("Moving play history", exc_info=True)

    def meta_archivo(self, ruta):
        try:
            st = os.stat(ruta)
            return self.tags.obtener([(ruta, st.st_mtime_ns, st.st_size)]).get(ruta, {})
        except OSError:
            return {}

    # ---- SOAP
    def control(self, servicio, accion, args, base, ip=""):
        if servicio == "cds":
            if accion == "Browse":
                return self.browse(args, base, ip)
            if accion == "GetSystemUpdateID":
                return soap_ok(NS_CDS, accion, [("Id", self.update_id)])
            if accion == "Search":
                return self.buscar(args, base, ip)
            if accion == "GetSearchCapabilities":
                return soap_ok(NS_CDS, accion, [("SearchCaps", "dc:title,dc:creator,upnp:artist,upnp:album,upnp:genre,"
                                                                 "upnp:class,dc:date,upnp:originalTrackNumber")])
            if accion == "GetSortCapabilities":
                return soap_ok(NS_CDS, accion, [("SortCaps", "dc:title,dc:creator,upnp:artist,upnp:album,upnp:genre,"
                                                             "dc:date,upnp:originalTrackNumber")])
        elif servicio == "cms":
            if accion == "GetProtocolInfo":
                return soap_ok(NS_CMS, accion, [("Source", SOURCE_PROTOCOLS), ("Sink", "")])
            if accion == "GetCurrentConnectionIDs":
                return soap_ok(NS_CMS, accion, [("ConnectionIDs", "0")])
            if accion == "GetCurrentConnectionInfo":
                return soap_ok(NS_CMS, accion, [
                    ("RcsID", -1), ("AVTransportID", -1), ("ProtocolInfo", ""), ("PeerConnectionManager", ""),
                    ("PeerConnectionID", -1), ("Direction", "Output"), ("Status", "OK")])
        return None

    def buscar(self, a, base, ip=""):
        try:
            inicio = max(0, int(a.get("StartingIndex") or 0))
            cuantos = max(0, int(a.get("RequestedCount") or 0))
        except ValueError:
            return soap_fault(402, "Invalid Args")
        t0 = time.time()
        res = self.indice.buscar(a.get("SearchCriteria", ""), a.get("ContainerID", "0") or "0",
                                 a.get("SortCriteria", ""))
        pagina = res[inicio:inicio + cuantos] if cuantos else res[inicio:]
        items = self.bib.render_lista(pagina, a.get("ContainerID", "0") or "0", base)
        self.eventos.emitir("navegacion", ip=ip, carpeta=f"{T('t.busqueda')}: {a.get('SearchCriteria', '')[:80]}", inicio=inicio,
                            n=len(items), total=len(res), ms=round((time.time() - t0) * 1000, 1))
        return soap_ok(NS_CDS, "Search", [
            ("Result", DIDL_OPEN + "".join(items) + "</DIDL-Lite>"),
            ("NumberReturned", len(items)), ("TotalMatches", len(res)), ("UpdateID", self.update_id)])

    def browse(self, a, base, ip=""):
        oid = a.get("ObjectID", "0") or "0"
        flag = a.get("BrowseFlag", "BrowseDirectChildren")
        try:
            inicio = max(0, int(a.get("StartingIndex") or 0))
            cuantos = max(0, int(a.get("RequestedCount") or 0))
        except ValueError:
            return soap_fault(402, "Invalid Args")
        t0 = time.time()
        bib = self.bib
        if flag == "BrowseMetadata":
            desc, parent = bib.metadata(oid)
            if desc is None:
                return soap_fault(701, "No such object")
            items, total = bib.render_lista([desc], parent, base), 1
        elif flag == "BrowseDirectChildren":
            hijos = bib.hijos(oid)
            if hijos is None:
                return soap_fault(701, "No such object")
            total = len(hijos)
            pagina = hijos[inicio:inicio + cuantos] if cuantos else hijos[inicio:]
            descs = [("raiz", h[1]) if h[0] == "raiz" else h for h in pagina]
            items = bib.render_lista(descs, oid, base)
            pi = self.precarga_img
            if pi is not None and PrecargaImagenes.aplica(oid):
                pi.pedir(oid, pagina, base)                  # el siguiente nivel, en segundo plano
        else:
            return soap_fault(402, "Invalid Args")
        ms = (time.time() - t0) * 1000
        log.debug("Browse %s %r [%d+%d] -> %d/%d in %.0f ms", flag, oid, inicio, cuantos, len(items), total, ms)
        if flag == "BrowseDirectChildren":
            self.eventos.emitir("navegacion", ip=ip, carpeta=self.nombre_objeto(oid), inicio=inicio,
                                n=len(items), total=total, ms=round(ms, 1))
        return soap_ok(NS_CDS, "Browse", [
            ("Result", DIDL_OPEN + "".join(items) + "</DIDL-Lite>"),
            ("NumberReturned", len(items)), ("TotalMatches", total), ("UpdateID", self.update_id)])


# ═══════════════════════════════════════════════════════════════════════════ precarga en reposo

class PrecargaImagenes(threading.Thread):
    """Prepara un nivel adelante lo que vas a ver: al abrir un artista (o un género, una carpeta, una playlist…),
    arma en segundo plano la primera página de cada álbum/carpeta visible (tags y lista quedan en caché) y genera
    las portadas que esa página va a pedir. Nunca en la primera vista (serían cientos de imágenes). Cede el paso a
    cualquier petición real (las portadas visibles van primero), se cancela al navegar a otro lado y tiene tope."""
    MAX_CONTENEDORES, MAX_IMAGENES, MAX_SEG, PAGINA = 30, 150, 15, 40

    def __init__(self, app):
        super().__init__(daemon=True, name="precarga-imagenes")
        self.app = app
        self.cond = threading.Condition()
        self.trabajo, self.gen = None, 0
        self.hechas = 0

    @staticmethod
    def aplica(oid):
        if oid in ("0", "v:carpetas", "v:playlists") or re.fullmatch(r"v:[\w]+", oid):
            return False                                 # primera vista: raíz y listas de cada vista
        if re.fullmatch(r"d[0-9a-f]{8}:", oid):
            return False                                 # raíz de una carpeta de música (sus artistas)
        return True

    def pedir(self, oid, pagina, base):
        if not self.app.cfg["precarga"]["activa"]:
            return
        conts = [h[1] if h[0] == "v" else self.app.bib.oid(("d", h[1], h[2])) for h in pagina
                 if h[0] == "v" or h[0] == "d"][:self.MAX_CONTENEDORES]
        with self.cond:
            self.gen += 1                                # lo anterior queda cancelado
            self.trabajo = (self.gen, conts, base) if conts else None
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while self.trabajo is None:
                    self.cond.wait()
                gen, conts, base = self.trabajo
                self.trabajo = None
            try:
                self._preparar(gen, conts, base)
            except Exception:
                log.debug("Image prefetch", exc_info=True)

    def _ceder(self, gen):
        """Esperar mientras haya peticiones reales; False si hay un trabajo más nuevo."""
        while time.time() - self.app.ultima_peticion < 0.3:
            if self.gen != gen:
                return False
            time.sleep(0.1)
        return self.gen == gen

    def _preparar(self, gen, conts, base):
        app, bib = self.app, self.app.bib
        t0, hechas, vistas = time.monotonic(), 0, set()
        for oid in conts:
            if not self._ceder(gen) or time.monotonic() - t0 > self.MAX_SEG:
                return
            hijos = bib.hijos(oid)                       # la lista queda cacheada: abrirlo es inmediato
            if not hijos:
                continue
            pagina = [("raiz", h[1]) if h[0] == "raiz" else h for h in hijos[:self.PAGINA]]
            xml = "".join(bib.render_lista(pagina, oid, base))      # y sus tags leídos
            for u in re.findall(r"<upnp:albumArtURI[^>]*>([^<]+)</upnp:albumArtURI>", xml):
                resto = unquote(urlparse(html_unescape(u)).path).split("/art/", 1)[-1]
                if resto in vistas or resto.startswith("h/"):
                    continue                             # /art/h/ = ya generada
                vistas.add(resto)
                if not self._ceder(gen) or hechas >= self.MAX_IMAGENES or time.monotonic() - t0 > self.MAX_SEG:
                    return
                src = app.fuente_url_portada(resto)
                if src and not app.portada_en_disco(src):
                    app.portada(src, guardar_en_memoria=False)
                    hechas += 1
                    self.hechas += 1
        if hechas:
            log.debug("Image prefetch: %d covers ready (%.1f s)", hechas, time.monotonic() - t0)


TAGS_CALIFICACION = ("rating", "fmps_rating")    # RATING (Picard, foobar2000, MediaMonkey…) y FMPS_RATING


def estrellas(v):
    """Calificación en cualquier escala (1–5, 0–10, 0–100, 0–255, FMPS 0–1) -> estrellas 0–5."""
    try:
        x = float(str(v).split(";")[0].strip().replace(",", "."))
    except ValueError:
        return 0
    if x <= 0:
        return 0
    if x <= 1:
        return round(x * 5)
    for tope in (5, 10, 100, 255):
        if x <= tope:
            return round(x * 5 / tope)
    return 0


def _firma_listado(l):
    """Firma de un listado de carpeta: cambia si se agrega, quita, renombra o modifica algo adentro."""
    return hash((tuple(l[0]), tuple(l[1]), tuple(l[3])))


class Precargador(threading.Thread):
    """Con el servidor inactivo, recorre las carpetas y llena la caché: tags (lector rápido) y
    portadas de cada carpeta (y de cada pista en carpetas mixtas). Se detiene ante cualquier navegación
    y retoma en la siguiente inactividad saltando lo que ya está en caché."""
    REVISION_MIN = 600       # s entre revisiones ligeras (como mínimo)

    def __init__(self, app):
        super().__init__(daemon=True, name="precarga")
        self.app = app
        self.ultimo_completo = 0.0
        self.pend_lector, self.prox_lector = None, 0.0
        self.firmas = {}                     # carpeta -> firma de su listado (revisión ligera)
        self.prox_revision = 0.0
        self.estado = "esperando"
        self.st = {"carpetas": 0, "total_carpetas": 0, "pistas": 0, "portadas": 0, "actual": "",
                   "inicio": 0, "fin": 0}

    def resumen(self):
        cfg = self.app.cfg["precarga"]
        r = dict(self.st, estado=self.estado, activa=cfg["activa"], ultimo_completo=int(self.ultimo_completo))
        r["espera_seg"] = max(0, int(cfg["inactividad_min"] * 60 - (time.time() - self.app.ultima_nav)))
        return r

    def _quieto(self):
        """Nadie navega: momento de trabajo de fondo ligero (no espera los minutos de la precarga). Puede estar
        sonando música: leer unos KB de tags no la afecta (el reproductor tiene segundos de búfer) y así lo nuevo
        aparece aunque se escuche todo el día."""
        return time.time() - self.app.ultima_peticion > 20

    def _revision_ligera(self):
        """Música nueva en minutos, no en la próxima precarga completa: recorre las carpetas comparando su
        listado (nombre, fecha y tamaño de cada archivo) con el de la vez anterior y solo lee las pistas nuevas.
        No depende de la fecha de la carpeta (en Windows no cambia al mover carpetas adentro). El intervalo se
        adapta a lo que tarde el recorrido (un NAS lento se revisa menos seguido)."""
        app, bib = self.app, self.app.bib
        if not app.cfg["precarga"]["activa"] or time.time() < self.prox_revision:
            return
        if not self.prox_revision:                         # primera vez: un minuto después de arrancar
            self.prox_revision = time.time() + 60
            return
        if not self._quieto():
            return
        t0 = time.monotonic()
        pila = [(r, k not in bib.sin_indexar) for k, (r, _) in bib.raices.items() if os.path.isdir(r)]
        vistas, nuevas, cambios = set(), 0, 0
        # primera vuelta de la sesión: solo anota cómo está cada carpeta (leer una biblioteca entera que nunca se
        # leyó es trabajo de la precarga, en reposo de verdad); de ahí en adelante, lee lo que cambie
        sembrar = not self.firmas
        while pila:
            if not self._quieto():
                self.prox_revision = time.time() + 60       # alguien navega: se retoma en un rato
                return
            ruta, indexar = pila.pop()
            l = bib.listar(ruta, fresco=True)              # un scandir; avisa a los controles si cambió
            if not l:
                continue
            vistas.add(ruta)
            pila.extend((os.path.join(ruta, d), indexar) for d in l[0])
            f = _firma_listado(l)
            if self.firmas.get(ruta) == f:
                continue
            if sembrar:
                self.firmas[ruta] = f
                continue
            if indexar and l[1]:
                archivos = [(os.path.join(ruta, n), m, sz) for n, m, sz in l[1]]
                _, faltan = app.tags._en_cache(archivos)
                for r, m, sz in faltan:
                    if not self._seguir_lento():
                        self.prox_revision = time.time() + 60   # la carpeta se termina en la próxima vuelta
                        return
                    try:
                        app.tags._guardar(r, m, sz, leer_tags(r))
                        nuevas += 1
                    except Exception as e:
                        log.debug("Quick check %s: %s", r, e)
            self.firmas[ruta] = f                          # solo cuando la carpeta quedó leída completa
            cambios += 1
        for r in set(self.firmas) - vistas:                # carpetas borradas
            del self.firmas[r]
            cambios += 0 if sembrar else 1
        dur = time.monotonic() - t0
        if cambios and not nuevas:
            app.tags.tocar()                               # solo borrados/movidos: el índice se pone al día igual
        if nuevas or cambios:
            log.info("Quick check: %d new tracks, %d folders changed (%.1f s)", nuevas, cambios, dur)
        self.prox_revision = time.time() + max(self.REVISION_MIN, dur * 40)

    def _seguir_lento(self):
        """Para trabajos de fondo largos: si está sonando música, un respiro entre archivo y archivo (en un NAS el
        audio que se está enviando va primero)."""
        if self.app.transfers:
            time.sleep(0.02)
        return self._quieto()

    def _actualizar_lector(self):
        """Relee en segundo plano las pistas guardadas con un lector anterior (tras actualizar SupaVic).
        Va aunque la precarga esté apagada: los datos viejos sirven, pero les faltan campos nuevos."""
        if time.time() < self.prox_lector:
            return
        tags = self.app.tags
        if self.pend_lector is None:
            self.pend_lector = tags.pendientes_lector()
            if self.pend_lector:
                log.info("Re-reading %d tracks with the new tag reader (in the background)", self.pend_lector)
        if not self.pend_lector:
            self.prox_lector = float("inf")
            return
        t0 = time.monotonic()
        while self._quieto() and time.monotonic() - t0 < 2.5:
            r = tags.actualizar_lote(32, seguir=self._seguir_lento)
            if r == 0:
                log.info("Tag reader update finished")
                self.pend_lector, self.prox_lector = 0, float("inf")
                return
            if r == 2:                                   # solo quedan archivos que cambiaron: reintentar en 1 h
                self.prox_lector = time.time() + 3600
                return

    def run(self):
        time.sleep(5)
        while True:
            time.sleep(3)
            try:
                self.app.indice.mantener()                   # índice al día cuando nadie navega
            except Exception:
                log.exception("Index upkeep")
            try:
                self._actualizar_lector()
            except Exception:
                log.exception("Tag reader update")
                self.prox_lector = time.time() + 600
            try:
                self._revision_ligera()
            except Exception:
                log.exception("Quick check")
                self.prox_revision = time.time() + 600
            cfg = self.app.cfg["precarga"]
            if not cfg["activa"]:
                self.estado = "desactivada"
                continue
            if self.ultimo_completo and time.time() - self.ultimo_completo < cfg["repetir_horas"] * 3600:
                self.estado = "completa"
                continue
            if not self.app.inactivo():
                if self.estado not in ("pausada",):
                    self.estado = "esperando"
                continue
            try:
                self.pasada()
            except Exception:
                log.exception("Prefetch")
                self.estado = "esperando"
                time.sleep(60)

    def _pausa(self):
        self.estado = "pausada"
        log.info("Prefetch paused by activity (%d tracks, %d covers this pass)",
                 self.st["pistas"], self.st["portadas"])
        self.app.eventos.emitir("precarga", accion="pausa", pistas=self.st["pistas"], portadas=self.st["portadas"],
                                carpetas=self.st["carpetas"], total=self.st["total_carpetas"])

    def pasada(self):
        app, bib = self.app, self.app.bib
        self.estado = "escaneando"
        self.st.update(carpetas=0, total_carpetas=0, pistas=0, portadas=0, actual="", inicio=int(time.time()), fin=0)
        log.info("Prefetch: server idle, filling cache…")
        app.eventos.emitir("precarga", accion="inicio")
        # 1) enumerar carpetas (DFS, mismo orden que en el WiiM)
        # Carpetas con "Indexar tags" desmarcado se recorren igual (solo listados), para que la limpieza no borre
        # lo que ya está en caché de ellas, pero no se leen tags ni portadas: eso pasa solo al abrirlas.
        carpetas, pila = [], [(r, k not in bib.sin_indexar) for k, (r, _) in reversed(list(bib.raices.items()))
                              if os.path.isdir(r)]
        while pila:
            if not app.inactivo():
                return self._pausa()
            ruta, indexar = pila.pop()
            l = bib.listar(ruta)
            if not l:
                continue
            carpetas.append((ruta, indexar))
            pila.extend((os.path.join(ruta, d), indexar) for d in reversed(l[0]))
            self.st["total_carpetas"] = len(carpetas)
        hacer_portadas = app.cfg["precarga"]["portadas"]
        vigentes, claves = set(), set()
        fuentes_vivas = set()                            # claves de TODAS las fuentes que existen (para la limpieza)
        # 2) tags y portadas carpeta por carpeta
        for i, (ruta, indexar) in enumerate(carpetas):
            self.st["carpetas"], self.st["actual"] = i, ruta
            if i and not i % 200:
                app.indice.mantener()                     # en reposo: las vistas se van llenando durante la pasada
            l = bib.listar(ruta)
            if not l:
                continue
            self.firmas[ruta] = _firma_listado(l)          # la revisión ligera parte de aquí: nada que repetir
            for img in l[2]:                             # imágenes de la carpeta: fuentes posibles (portada, disco, artista)
                ck = app.clave_portada(("img", os.path.join(ruta, img)))
                if ck:
                    fuentes_vivas.add(ck)
            if not indexar:
                for n, m, sz in l[1]:
                    r = os.path.join(ruta, n)
                    vigentes.add(r)
                    fuentes_vivas.add(app.clave_fuente("emb", r, m, sz))
                    fuentes_vivas.add(app.clave_fuente("emb_ar", r, m, sz))
                continue
            if not l[1]:
                if hacer_portadas:                       # carpeta de artista con su propia imagen
                    src = bib.fuente_portada_carpeta(ruta, max_prof=0)
                    if src:
                        if not app.portada_en_disco(src) and app.inactivo():
                            app.portada(src, guardar_en_memoria=False)
                        claves.add(app.hash_portada(src))
                continue
            archivos = [(os.path.join(ruta, n), m, s) for n, m, s in l[1]]
            vigentes.update(a[0] for a in archivos)
            for r, m, s in archivos:                     # la clave sale del listado: sin un stat por pista
                fuentes_vivas.add(app.clave_fuente("emb", r, m, s))
                fuentes_vivas.add(app.clave_fuente("emb_ar", r, m, s))
            _, faltan = app.tags._en_cache(archivos)
            for r, m, s in faltan:
                if not app.inactivo():
                    return self._pausa()
                try:
                    app.tags._guardar(r, m, s, leer_tags(r))
                    self.st["pistas"] += 1
                except Exception as e:
                    log.debug("Prefetch tags %s: %s", r, e)
            if not hacer_portadas:
                continue
            pc = app.cfg["portadas"]
            fuentes = [bib.fuente_portada_carpeta(ruta, max_prof=0)]
            metas, _ = app.tags._en_cache(archivos)
            # Carpeta de un álbum: basta su portada; la de cada pista se saca cuando el reproductor la pide (y casi
            # siempre es la misma imagen: se reutiliza). Leer la imagen embebida de cada pista en reposo eran
            # decenas de GB de lectura en una biblioteca grande. Carpetas mezcladas: cada pista tiene la suya.
            por_pista = not bib.modo_album(ruta)
            if not por_pista and pc["por_pista"]:
                # álbum: UNA embebida frontal basta; las pistas con la misma imagen quedan asignadas a ella
                # (_mapear_hermanas) y al abrir el álbum todas las portadas salen al instante
                pri = next((r for r, mm in sorted(metas.items()) if 3 in tipos_imagen(mm)), None)
                if pri:
                    fuentes.append(("emb", pri))
            for r, mm in sorted(metas.items()):
                tipos = tipos_imagen(mm)
                if por_pista and tipos:
                    fuentes.append(("emb", r))
                if pc["artista"] and any(t in PRIO_IMG_ARTISTA for t in tipos):
                    fuentes.append(("emb_ar", r))
            for src in fuentes:
                if not src:
                    continue
                if not app.portada_en_disco(src):
                    if not app.inactivo():
                        return self._pausa()
                    try:
                        if app.portada(src, guardar_en_memoria=False):
                            self.st["portadas"] += 1
                    except Exception as e:
                        log.debug("Prefetch cover %s: %s", src, e)
                claves.add(app.hash_portada(src))
        # 3) limpieza: pistas que ya no existen y portadas que nadie usa (solo la caché, nunca la música)
        quitadas = app.tags.podar(vigentes) if vigentes else 0
        borradas = 0
        if hacer_portadas and app.dir_portadas.exists():
            # fuentes que ya no existen (archivo borrado o cambiado) dejan de contar; lo generado al pedirlo se queda
            with app.tags.lock:
                filas = app.tags.db.execute("SELECT clave, h FROM portadas").fetchall()
                viejas = [(c,) for c, _ in filas if c not in fuentes_vivas]
                if viejas and len(viejas) < len(filas):  # (si TODO parece viejo, algo raro pasa: no borrar)
                    app.tags.db.executemany("DELETE FROM portadas WHERE clave=?", viejas)
                    filas = [(c, h) for c, h in filas if c in fuentes_vivas]
            claves |= {h for _, h in filas if h}
            with app.art_lock:
                app.art_map.clear()
            app._h_en_disco.clear()
            for f in app.dir_portadas.rglob("*.jpg"):
                try:
                    if f.stem not in claves and f.stat().st_mtime < self.st["inicio"]:
                        f.unlink()
                        borradas += 1
                except OSError:
                    pass
        if quitadas or borradas:
            log.info("Cache cleaned: %d missing tracks, %d unused covers", quitadas, borradas)
        self.st.update(carpetas=len(carpetas), actual="", fin=int(time.time()))
        self.ultimo_completo = time.time()
        self.estado = "completa"
        app.forzar_precarga = False
        if app.indice.version != app.tags.version:      # vistas al día sin esperar a que alguien las abra
            app.indice.invalidar()
        log.info("Prefetch complete: %d folders, %d tracks read, %d covers generated in %.0f s",
                 len(carpetas), self.st["pistas"], self.st["portadas"], time.time() - self.st["inicio"])
        app.eventos.emitir("precarga", accion="fin", pistas=self.st["pistas"], portadas=self.st["portadas"],
                           carpetas=len(carpetas), seg=int(time.time() - self.st["inicio"]))


# ═══════════════════════════════════════════════════════════════════════════ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_HDR
    sys_version = ""

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def base(self):
        ip = self.connection.getsockname()[0]
        return f"http://{ip}:{self.server.server_address[1]}"

    def _send(self, code, body=b"", ctype='text/xml; charset="utf-8"', headers=None, head=False):
        if isinstance(body, str):
            # nombres que no son UTF-8 válido (sistema con locale no UTF-8): reemplazar en vez de fallar
            body = body.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Date", formatdate(usegmt=True))
        self.send_header("EXT", "")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def do_HEAD(self):
        self.do_GET(head=True)

    def autorizado(self):
        """Panel y API: libres si no hay contraseña o si es la misma computadora; si no, cookie de sesión.
        Las rutas UPnP (descripción, control, audio, portadas) nunca piden contraseña: los reproductores no saben."""
        clave = self.app.clave
        if not clave or es_local(self.client_address[0]):
            return True
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == COOKIE_SESION:
                return hmac.compare_digest(v, token_sesion(clave))
        return False

    def mismo_origen(self):
        """Protección CSRF: un POST a la API solo desde el propio panel. Pedimos JSON (otra página no puede mandarlo
        sin permiso CORS, que no damos) y, si el navegador manda Origin, que sea este mismo servidor."""
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return False
        origen = self.headers.get("Origin")
        if origen and origen != "null":
            return urlparse(origen).netloc.lower() == (self.headers.get("Host") or "").lower()
        return True

    def do_GET(self, head=False):
        url = urlparse(self.path)
        ruta = unquote(url.path)
        app = self.app
        if ruta == "/desc.xml":
            app.registrar(self.client_address[0], self.headers.get("User-Agent"), "descubrimiento")
            app.eventos.descubrimiento(self.client_address[0], self.headers.get("User-Agent"))
            return self._send(200, device_xml(app.cfg["servidor"]["nombre"], app.udn), head=head)
        if ruta == "/cds.xml":
            return self._send(200, CDS_SCPD, head=head)
        if ruta == "/cms.xml":
            return self._send(200, CMS_SCPD, head=head)
        if ruta.startswith("/m/"):
            return self.enviar_audio(ruta[3:], head)
        if ruta.startswith("/art/"):
            return self.enviar_portada(ruta[5:], head)
        if ruta.startswith("/pl/") and ruta.endswith(".m3u8"):
            return self.enviar_m3u8(ruta[4:-5], parse_qs(url.query), head)
        if ruta in ("/", "/index.html"):
            html = PANEL_HTML if self.autorizado() else LOGIN_HTML
            return self._send(200, html.replace("__LANG__", i18n.LANG), "text/html; charset=utf-8",
                              {"Cache-Control": "no-store"}, head=head)
        if ruta in ("/favicon.svg", "/favicon.ico"):
            return self._send(200, FAVICON_SVG, "image/svg+xml", {"Cache-Control": "max-age=86400"}, head=head)
        if ruta.startswith("/api/"):
            if not self.autorizado():
                return self._json({"error": "auth"}, 401)
            return self.api_get(ruta, parse_qs(url.query))
        return self._send(404, "", "text/plain", head=head)

    # ---- streaming
    def enviar_audio(self, resto, head):
        key, _, rel = resto.partition("/")
        app = self.app
        ruta = app.bib.resolver(key, rel)
        if not ruta or os.path.splitext(ruta)[1].lower() not in AUDIO or not os.path.isfile(ruta):
            return self._send(404, "", "text/plain", head=head)
        mime = AUDIO[os.path.splitext(ruta)[1].lower()]
        tam = os.path.getsize(ruta)
        ini, fin, code = 0, tam - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    ini = int(m.group(1))
                    fin = min(int(m.group(2)), tam - 1) if m.group(2) else tam - 1
                else:
                    ini = max(0, tam - int(m.group(2)))
                if ini >= tam or ini > fin:
                    return self._send(416, "", "text/plain", {"Content-Range": f"bytes */{tam}"}, head=head)
                code = 206
        if not head and ini == 0:
            app.registrar(self.client_address[0], self.headers.get("User-Agent"), "▶ " + rel)
        largo = fin - ini + 1
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(largo))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
        self.send_header("Date", formatdate(usegmt=True))
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", protocol_info(mime).split(":", 3)[3])
        self.end_headers()
        if head:
            return
        sid = uuid.uuid4().hex[:12]
        ip = self.client_address[0]
        base_ev = {"id": sid, "ip": ip, "raiz": app.bib.raices[key][1], "key": key, "rel": rel,
                   "ext": os.path.splitext(ruta)[1].lower().lstrip("."), "tam": tam, "ini": ini, "fin": fin}
        t0 = time.time()
        enviados, estado, avisado = 0, "completo", False
        with app.transfer_lock:
            app.transfers += 1
        try:
            self.wfile.flush()
            with open(ruta, "rb") as fh:
                falta, pos = largo, ini
                while falta > 0:
                    # sendfile: el sistema copia del disco a la red sin pasar por Python (menos CPU en NAS/Pi);
                    # por tramos para llevar la cuenta de lo enviado y notar un corte
                    n = self.connection.sendfile(fh, pos, min(4 << 20, falta))
                    if not n:
                        break
                    primero = pos == 0
                    pos += n
                    falta -= n
                    enviados += n
                    if not avisado:                      # metadatos para el dashboard DESPUÉS del primer tramo:
                        avisado = True                   # la música empieza sin esperar a leer tags
                        meta = app.meta_archivo(ruta)
                        meta.pop("art", None)
                        app.eventos.emitir("stream_inicio", ua=(self.headers.get("User-Agent") or "")[:120],
                                           meta=meta, **base_ev)
                    app.repro.avance(ip, key, rel, n, tam, primero)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            estado = "cortado"
        except OSError as e:
            log.debug("Stream %s: %s", ruta, e)
            self.close_connection = True
            estado = "error"
        finally:
            with app.transfer_lock:
                app.transfers -= 1
            app.ultima_transfer = time.time()
        if not avisado:
            meta = app.meta_archivo(ruta)
            meta.pop("art", None)
            app.eventos.emitir("stream_inicio", ua=(self.headers.get("User-Agent") or "")[:120], meta=meta, **base_ev)
        app.eventos.emitir("stream", estado=estado, enviados=enviados, seg=round(time.time() - t0, 3), **base_ev)

    def enviar_portada(self, resto, head):
        app = self.app
        app.marcar_actividad()
        mh = re.fullmatch(r"h/([0-9a-f]{24})\.jpg", resto)
        if mh:                                              # por contenido: no cambia nunca, caché del reproductor
            h = mh.group(1)
            with app.art_lock:
                res = app.art_mem.get(h)
            if res is None:
                archivo = app._archivo_portada(h)
                try:
                    res = app._en_memoria(h, ("image/jpeg", archivo.read_bytes()), True)
                except OSError:
                    return self._send(404, "", "text/plain", head=head)
            return self._send(200, res[1], res[0], {
                "Cache-Control": "public, max-age=31536000, immutable",
                "contentFeatures.dlna.org": "DLNA.ORG_PN=JPEG_SM;DLNA.ORG_OP=00;DLNA.ORG_FLAGS=00f00000000000000000000000000000",
                "transferMode.dlna.org": "Interactive"}, head=head)
        res = app.portada(app.fuente_url_portada(resto))
        if not res:
            return self._send(404, "", "text/plain", head=head)
        mime, data = res
        return self._send(200, data, mime, {
            "Cache-Control": "max-age=604800",
            "contentFeatures.dlna.org": "DLNA.ORG_PN=JPEG_SM;DLNA.ORG_OP=00;DLNA.ORG_FLAGS=00f00000000000000000000000000000"
            if mime == "image/jpeg" else "*",
            "transferMode.dlna.org": "Interactive"}, head=head)

    LISTAS_M3U8 = ("aleatorio", "recientes", "tocadas", "mas_tocadas", "favoritas", "hires")
    ALIAS_M3U8 = {"random": "aleatorio", "recent": "recientes", "played": "tocadas", "most-played": "mas_tocadas",
                  "favourites": "favoritas", "favorites": "favoritas"}

    def enviar_m3u8(self, vista, q, head):
        """Una vista como playlist .m3u8 (con #EXTINF), para guardarla como preset en el WiiM u otro reproductor:
        /pl/aleatorio.m3u8 (100 al azar, distintas en cada pedido), /pl/favoritas.m3u8, /pl/recientes.m3u8…
        Sin contraseña, como el audio: los reproductores no saben iniciar sesión."""
        app = self.app
        vista = self.ALIAS_M3U8.get(vista, vista)
        if vista not in self.LISTAS_M3U8 and vista not in app.indice.defs_x(activas=False):
            return self._send(404, "", "text/plain", head=head)
        d = app.indice.asegurar(espera=2)
        if d is None:
            return self._send(503, "", "text/plain", head=head)
        try:
            n = max(1, min(int((q.get("n") or ["100"])[0]), 2000))
        except ValueError:
            n = 100
        if vista == "aleatorio":                         # al azar de verdad en cada pedido (el preset suena distinto)
            idxs = random.sample(range(len(d["pistas"])), min(n, len(d["pistas"])))
            ts = [app.indice._t(d, i) for i in idxs]
        else:
            ts = [h for h in app.indice._calcular_todas(d, f"v:{vista}") if h[0] == "t"][:n]
        base = self.base()
        metas = app.tags.obtener([(app.bib.resolver(t[1], t[2]), t[3], t[4]) for t in ts], espera=2)
        lineas = ["#EXTM3U", f"#PLAYLIST:{VISTAS.get(vista) or vista}"]
        for t in ts:
            ruta = app.bib.resolver(t[1], t[2])
            m = metas.get(ruta) or {}
            artista = m.get("artist") or ""
            titulo = m.get("title") or os.path.splitext(t[2].rsplit("/", 1)[-1])[0]
            lineas.append(f"#EXTINF:{int(m.get('dur') or -1)},{artista + ' - ' if artista else ''}{titulo}".replace("\n", " "))
            if m.get("album"):
                lineas.append(f"#EXTALB:{m['album']}".replace("\n", " "))
            lineas.append(f"{base}/m/{t[1]}/{quote(t[2], safe='/')}")
        return self._send(200, "\n".join(lineas) + "\n", "audio/x-mpegurl; charset=utf-8",
                          {"Cache-Control": "no-store"}, head=head)

    # ---- API del panel
    def api_get(self, ruta, q):
        app = self.app
        if ruta == "/api/estado":
            ahora = time.time()
            with app.cli_lock:
                clientes = sorted((dict(c) for c in app.clientes.values() if ahora - c["ultimo"] < 86400),
                                  key=lambda c: -c["ultimo"])
            for c in clientes:
                c["hace"] = int(ahora - c["ultimo"])
            n_port, tam_port = app.estadisticas_portadas()
            raices = [{"ruta": r, "nombre": n, "disponible": os.path.isdir(r)} for r, n in app.bib.raices.values()]
            return self._json({
                "nombre": app.cfg["servidor"]["nombre"], "version": VERSION,
                "activo_desde": int(app.inicio), "uptime": int(ahora - app.inicio),
                "ips": app.ssdp.ips if app.ssdp else [], "puerto": app.puerto,
                "carpetas": raices, "pistas_cache": app.tags.total(), "pistas_leidas_sesion": app.tags.leidos,
                "portadas": n_port, "portadas_mb": round(tam_port / 1048576, 1),
                "clientes": clientes, "pillow": Image is not None, "cache": str(app.dir_cache),
                "python": sys.version.split()[0],
                "precarga": app.precarga.resumen() if app.precarga else None,
                "reubicados": app.tags.reubicados,
                "indice": app.indice.resumen(),
                "vistas_disponibles": list(VISTAS.items()),
                "vistas_x": list(PRESETS_X),
                "lectura": {"cache": app.tags.total(), "indice": len(app.indice.d["pistas"]) if app.indice.d else 0},
                "idioma": i18n.LANG, "datos": str(DIR_BASE),
                "protegido": bool(app.clave), "local": es_local(self.client_address[0]),
                "dashboard": {"puerto": app.cfg["dashboard"]["puerto"], "activo": app.dashboard_activo()},
            })
        if ruta == "/api/diagnostico":
            return self._json(app.indice.diagnostico())
        if ruta == "/api/tags":
            c = app.indice.catalogo_tags()
            c["nombres"] = {t["tag"]: nombre_tag(t["tag"]) for t in c["tags"]}
            return self._json(c)
        if ruta == "/api/config":
            return self._json(app.cfg)
        if ruta == "/api/log":
            try:
                with open(RUTA_LOG, "r", encoding="utf-8", errors="replace") as fh:
                    lineas = fh.readlines()[-300:]
            except OSError:
                lineas = []
            return self._json({"lineas": [l.rstrip("\n") for l in lineas]})
        if ruta == "/api/explorar":
            if not cliente_permitido(self.client_address[0]):
                return self._json({"error": T("e.no_permitido")}, 403)
            return self._json(explorar((q.get("ruta") or [""])[0]))
        return self._json({"error": T("e.no_encontrado")}, 404)

    def do_POST(self):
        ruta = urlparse(self.path).path
        largo = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(largo) if largo else b""
        if ruta.startswith("/api/"):
            return self.api_post(ruta, body)
        servicio = {"/ctl/cds": "cds", "/ctl/cms": "cms"}.get(ruta)
        if not servicio:
            return self._send(404, "", "text/plain")
        try:
            root = ET.fromstring(body)
            soap_body = next(e for e in root if localname(e.tag) == "Body")
            act = soap_body[0]
            accion = localname(act.tag)
            args = {localname(c.tag): (c.text or "") for c in act}
        except Exception:
            return self._send(500, soap_fault(401, "Invalid Action"))
        if accion == "Browse":
            self.app.marcar_actividad()
            self.app.registrar(self.client_address[0], self.headers.get("User-Agent"), "navegando")
        try:
            resp = self.app.control(servicio, accion, args, self.base(), self.client_address[0])
        except Exception:
            log.exception("Error in %s.%s", servicio, accion)
            return self._send(500, soap_fault(501, "Action Failed"))
        if resp is None:
            return self._send(500, soap_fault(401, "Invalid Action"))
        return self._send(500 if "<s:Fault>" in resp else 200, resp)

    def _cookie(self, clave):
        return {"Set-Cookie": f"{COOKIE_SESION}={token_sesion(clave)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"}

    def api_login(self, body):
        app, ip = self.app, self.client_address[0]
        if not app.clave:
            return self._json({"ok": True})
        with app.cli_lock:
            fallos, hasta = app.login_fallos.get(ip, (0, 0))
        if time.time() < hasta:
            return self._json({"ok": False, "error": T("clave.espera")}, 429)
        try:
            clave = str(json.loads(body.decode("utf-8") or "{}").get("clave") or "")
        except (ValueError, AttributeError):
            clave = ""
        if verificar_clave(clave, app.clave):
            with app.cli_lock:
                app.login_fallos.pop(ip, None)
            return self._send(200, '{"ok": true}', "application/json", self._cookie(app.clave))
        time.sleep(1)                                    # frena los intentos a ciegas
        with app.cli_lock:
            fallos += 1
            app.login_fallos[ip] = (fallos, time.time() + 60 if fallos >= 5 else 0)
        return self._json({"ok": False, "error": T("clave.mal")}, 403)

    def api_clave(self, body):
        """Poner, cambiar o quitar la contraseña. Fuera de esta computadora pide la actual."""
        app = self.app
        try:
            d = json.loads(body.decode("utf-8") or "{}")
            actual, nueva = str(d.get("actual") or ""), str(d.get("nueva") or "")
        except (ValueError, AttributeError):
            return self._json({"ok": False, "error": T("e.json")}, 400)
        if app.clave and not es_local(self.client_address[0]) and not verificar_clave(actual, app.clave):
            time.sleep(1)
            return self._json({"ok": False, "error": T("clave.mal")}, 403)
        if nueva and len(nueva) < 4:
            return self._json({"ok": False, "error": T("clave.corta")}, 400)
        try:
            if nueva:
                g = hash_clave(nueva)
                RUTA_CLAVE.parent.mkdir(parents=True, exist_ok=True)
                RUTA_CLAVE.write_text(g, encoding="ascii")
            else:
                g = ""
                RUTA_CLAVE.unlink(missing_ok=True)
        except OSError as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        app.clave = g
        log.info("Panel password %s", "set" if g else "removed")
        return self._send(200, json.dumps({"ok": True, "protegido": bool(g)}), "application/json",
                          self._cookie(g) if g else None)

    def api_post(self, ruta, body):
        if not cliente_permitido(self.client_address[0]):
            return self._json({"error": T("e.red_local")}, 403)
        if not self.mismo_origen():
            return self._json({"error": "origin"}, 403)
        app = self.app
        if ruta == "/api/login":
            return self.api_login(body)
        if not self.autorizado():
            return self._json({"error": "auth"}, 401)
        if ruta == "/api/salir":
            return self._send(200, "{}", "application/json", {"Set-Cookie": f"{COOKIE_SESION}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        if ruta == "/api/clave":
            return self.api_clave(body)
        if ruta == "/api/config":
            try:
                datos = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json({"ok": False, "errores": [T("e.json")]}, 400)
            return self._json(app.actualizar_config(datos))
        if ruta == "/api/vista-previa":
            try:
                datos = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json({"error": T("e.json")}, 400)
            return self._json(app.indice.vista_previa(datos))
        if ruta == "/api/limpiar-cache":
            n = app.limpiar_cache()
            log.info("Cache cleared (%d covers)", n)
            return self._json({"ok": True, "portadas": n})
        if ruta == "/api/precarga-ahora":
            if app.precarga:
                app.precarga.ultimo_completo = 0
            app.forzar_precarga = True
            return self._json({"ok": True})
        if ruta == "/api/reiniciar":
            threading.Timer(0.5, app.reiniciar).start()
            return self._json({"ok": True})
        return self._json({"error": T("e.no_encontrado")}, 404)

    def do_SUBSCRIBE(self):
        svc = {"/evt/cds": "cds", "/evt/cms": "cms"}.get(urlparse(self.path).path)
        if not svc:
            return self._send(404, "", "text/plain")
        m = re.search(r"(\d+)", self.headers.get("TIMEOUT", "") or "")
        timeout = min(int(m.group(1)), 3600) if m else 1800
        sid = self.headers.get("SID")
        if sid:
            if not self.app.renovar(sid, timeout):
                return self._send(412, "", "text/plain")
        else:
            if "upnp:event" not in (self.headers.get("NT") or ""):
                return self._send(412, "", "text/plain")
            sid = self.app.suscribir(self.headers.get("CALLBACK"), svc, timeout)
        self._send(200, "", "text/plain", {"SID": sid, "TIMEOUT": f"Second-{timeout}"})

    def do_UNSUBSCRIBE(self):
        ok = self.app.desuscribir(self.headers.get("SID", ""))
        self._send(200 if ok else 412, "", "text/plain")


def explorar(ruta):
    """Lista subcarpetas para el selector de carpetas del panel."""
    if not ruta:
        if ES_WINDOWS:
            unidades = [f"{l}:\\" for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")]
            return {"ruta": "", "padre": None, "carpetas": [{"nombre": u, "ruta": u} for u in unidades]}
        ruta = "/"
    ruta = os.path.abspath(ruta)
    while not os.path.isdir(ruta):                 # sube hasta la carpeta existente más cercana
        arriba = os.path.dirname(ruta.rstrip("\\/"))
        if not arriba or arriba == ruta:
            return explorar("")
        ruta = arriba
    out = []
    try:
        with os.scandir(ruta) as it:
            for e in it:
                try:
                    if not e.is_dir() or e.name.startswith((".", "$")) or e.name == "System Volume Information":
                        continue
                    if ES_WINDOWS and e.stat().st_file_attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                        continue
                    out.append({"nombre": e.name, "ruta": os.path.join(ruta, e.name)})
                except OSError:
                    continue
    except OSError as ex:
        return {"ruta": ruta, "padre": os.path.dirname(ruta), "carpetas": [], "error": str(ex)}
    out.sort(key=lambda x: natkey(x["nombre"]))
    padre = os.path.dirname(ruta.rstrip("\\/"))
    if ES_WINDOWS and re.fullmatch(r"[A-Za-z]:\\?", ruta):
        padre = ""
    elif padre == ruta:
        padre = None
    return {"ruta": ruta, "padre": padre, "carpetas": out}


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not ES_WINDOWS        # en Windows SO_REUSEADDR permitiría dos instancias en el mismo puerto
    request_queue_size = 64

    def server_bind(self):
        if ES_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        """Un reproductor que corta la conexión (saltó de pista, se apagó) es normal: sin traza en el log."""
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            log.debug("Connection closed by %s: %s", client_address[0], e)
            return
        super().handle_error(request, client_address)


# ═══════════════════════════════════════════════════════════════════════════ SSDP

class SSDP(threading.Thread):
    GRUPO, PUERTO = "239.255.255.250", 1900

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.uuid = f"uuid:{app.udn}"
        self.targets = ["upnp:rootdevice", self.uuid, DEV_TYPE, NS_CDS, NS_CMS]
        self.ip_fija = app.cfg["servidor"]["ip"]
        self.ips = []
        self.unidas = set()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", self.PUERTO))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.sock = s
        self.refrescar_interfaces()

    def refrescar_interfaces(self):
        ips = [self.ip_fija] if self.ip_fija else ips_locales()
        for ip in ips:
            if ip in self.unidas:
                continue
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(self.GRUPO), socket.inet_aton(ip))
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self.unidas.add(ip)
            except OSError as e:
                log.debug("Could not join multicast on %s: %s", ip, e)
        if ips != self.ips:
            log.info("Announcing on: %s", ", ".join(ips) or "(no interface)")
        self.ips = ips

    def location(self, ip):
        return f"http://{ip}:{self.app.puerto}/desc.xml"

    def usn(self, nt):
        return self.uuid if nt == self.uuid else f"{self.uuid}::{nt}"

    def notificar(self, nts):
        for ip in list(self.ips):
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            except OSError:
                continue
            for nt in self.targets:
                msg = ("NOTIFY * HTTP/1.1\r\n"
                       f"HOST: {self.GRUPO}:{self.PUERTO}\r\n"
                       "CACHE-CONTROL: max-age=1800\r\n"
                       f"LOCATION: {self.location(ip)}\r\n"
                       f"NT: {nt}\r\nNTS: {nts}\r\n"
                       f"SERVER: {SERVER_HDR}\r\nUSN: {self.usn(nt)}\r\n\r\n")
                try:
                    self.sock.sendto(msg.encode(), (self.GRUPO, self.PUERTO))
                except OSError as e:
                    log.debug("NOTIFY on %s failed: %s", ip, e)

    def anunciador(self):
        for _ in range(3):
            self.notificar("ssdp:alive")
            time.sleep(1)
        ultimo = time.time()
        while True:
            time.sleep(30)                         # al arrancar con Windows la red puede tardar en estar lista
            antes = list(self.ips)
            self.refrescar_interfaces()
            if self.ips != antes or time.time() - ultimo >= 300:
                self.notificar("ssdp:alive")
                ultimo = time.time()

    def responder(self, st, addr, mx):
        time.sleep(random.uniform(0, min(mx, 3) * 0.4))
        ip = self.ip_fija or ip_hacia(addr[0]) or (self.ips[0] if self.ips else None)
        if not ip:
            return
        for t in (self.targets if st == "ssdp:all" else [st]):
            msg = ("HTTP/1.1 200 OK\r\n"
                   "CACHE-CONTROL: max-age=1800\r\n"
                   f"DATE: {formatdate(usegmt=True)}\r\nEXT:\r\n"
                   f"LOCATION: {self.location(ip)}\r\nSERVER: {SERVER_HDR}\r\n"
                   f"ST: {t}\r\nUSN: {self.usn(t)}\r\nContent-Length: 0\r\n\r\n")
            try:
                self.sock.sendto(msg.encode(), addr)
            except OSError as e:
                log.debug("SSDP reply to %s failed: %s", addr, e)

    def run(self):
        threading.Thread(target=self.anunciador, daemon=True).start()
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                if self.sock.fileno() == -1:
                    return
                continue
            texto = data.decode("utf-8", "replace")
            if not texto.startswith("M-SEARCH"):
                continue
            h = {}
            for linea in texto.split("\r\n")[1:]:
                if ":" in linea:
                    k, v = linea.split(":", 1)
                    h[k.strip().upper()] = v.strip()
            if h.get("MAN", "").strip('"') != "ssdp:discover":
                continue
            st = h.get("ST", "")
            if st != "ssdp:all" and st not in self.targets:
                continue
            threading.Thread(target=self.responder, args=(st, addr, _int(h.get("MX", "1")) or 1), daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════ panel web

LOGIN_HTML = r"""<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SupaVic DLNA</title><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#f4f2ee;--card:#fff;--tx:#1d1c1a;--mut:#6d6a64;--line:#e3dfd8;--acc:#c2410c;--bad:#b91c1c;--in:#faf9f7}
@media(prefers-color-scheme:dark){:root{--bg:#141312;--card:#1e1d1b;--tx:#eceae6;--mut:#9b978f;--line:#2f2d2a;--acc:#fb923c;--bad:#f87171;--in:#181716}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--tx);font:15px/1.5 "Segoe UI",system-ui,sans-serif;padding:16px}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px 24px;width:100%;max-width:340px}
h1{font-size:18px;margin:0 0 4px}p{color:var(--mut);font-size:13px;margin:0 0 16px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--in);color:var(--tx);font:inherit}
input:focus-visible,button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
#e{color:var(--bad);font-size:13px;min-height:20px;margin-top:8px}
</style></head><body>
<form id="f"><h1>SupaVic DLNA</h1><p id="d"></p>
<input type="password" id="k" autocomplete="current-password" autofocus required><button id="b"></button><div id="e" role="alert"></div></form>
<script>
const es=document.documentElement.lang==="es";
document.getElementById("d").textContent=es?"Este panel está protegido con contraseña.":"This panel is password protected.";
document.getElementById("k").placeholder=es?"Contraseña":"Password";
document.getElementById("b").textContent=es?"Entrar":"Log in";
document.getElementById("f").onsubmit=async ev=>{ev.preventDefault();const e=document.getElementById("e");e.textContent="";
  const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({clave:document.getElementById("k").value})});
  const j=await r.json().catch(()=>({}));if(j.ok)location.reload();else e.textContent=j.error||"Error"};
</script></body></html>"""

PANEL_HTML = r"""<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SupaVic DLNA</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#f4f2ee;--card:#fff;--tx:#1d1c1a;--mut:#6d6a64;--line:#e3dfd8;--acc:#c2410c;--acc2:#fff3ea;--ok:#15803d;--bad:#b91c1c;--in:#faf9f7}
@media(prefers-color-scheme:dark){:root{--bg:#141312;--card:#1e1d1b;--tx:#eceae6;--mut:#9b978f;--line:#2f2d2a;--acc:#fb923c;--acc2:#2a1d14;--ok:#4ade80;--bad:#f87171;--in:#181716}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 "Segoe UI",system-ui,sans-serif}
header{padding:22px 16px 0;max-width:980px;margin:auto;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{width:42px;height:42px;flex:none}
h1{font-size:20px;margin:0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px}
.hd-r{margin-left:auto;display:flex;gap:10px;align-items:center}
.pill{font-size:12px;padding:4px 10px;border-radius:99px;background:var(--acc2);color:var(--acc);font-weight:600}
.lnk{font-size:13px;color:var(--acc);text-decoration:none;font-weight:600}.lnk:hover{text-decoration:underline}
nav{max-width:980px;margin:18px auto 0;padding:0 16px;display:flex;gap:4px;border-bottom:1px solid var(--line);overflow-x:auto}
nav button{background:none;border:0;color:var(--mut);font:inherit;font-weight:600;padding:10px 14px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
nav button.on{color:var(--tx);border-color:var(--acc)}
nav button:focus-visible,.btn:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
main{max-width:980px;margin:auto;padding:18px 16px 40px}
section{display:none}section.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
details.diag{margin-top:10px}details.diag summary{cursor:pointer;list-style:none}details.diag summary::-webkit-details-marker{display:none}
details.diag ul{margin:10px 0 0;padding:0;list-style:none;max-height:420px;overflow:auto}details.diag li{padding:6px 0;border-top:1px solid var(--line);font-size:14px;overflow-wrap:anywhere}
details.diag .ruta{font-size:12px}
.k{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.v{font-size:22px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
h2{font-size:15px;margin:24px 0 10px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}
.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}
code{font:13px ui-monospace,Consolas,monospace;word-break:break-all}
label{display:block;font-weight:600;font-size:13px;margin:14px 0 5px}
.hint{font-weight:400;color:var(--mut);font-size:12px}
input[type=text],input[type=number],textarea,select{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--in);color:var(--tx);font:inherit}
textarea{min-height:70px;font:13px ui-monospace,Consolas,monospace}
input:focus,textarea:focus,select:focus{outline:2px solid var(--acc);outline-offset:-1px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:600px){.row{grid-template-columns:1fr}}
.btn{border:1px solid var(--line);background:var(--card);color:var(--tx);font:inherit;font-weight:600;padding:8px 14px;border-radius:8px;cursor:pointer}
.btn.pri{background:var(--acc);border-color:var(--acc);color:#fff}
.btn.sm{padding:4px 10px;font-size:13px}
.btn:disabled{opacity:.5}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}
.lista{list-style:none;padding:0;margin:0}
.lista li{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
.lista li code{flex:1}
.chk.idx{margin:0;font-weight:500;white-space:nowrap}
.msg{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:14px;display:none}
.msg.on{display:block}.msg.ok{background:color-mix(in srgb,var(--ok) 12%,transparent)}.msg.bad{background:color-mix(in srgb,var(--bad) 12%,transparent)}
.bienv{display:none;margin-bottom:16px;border-color:var(--acc);background:var(--acc2)}
.bienv b{display:block;font-size:16px;margin-bottom:4px}
#log{background:var(--in);border:1px solid var(--line);border-radius:10px;padding:12px;font:12px/1.55 ui-monospace,Consolas,monospace;max-height:65vh;overflow:auto;white-space:pre-wrap;word-break:break-all}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--tx);padding:0;width:min(560px,calc(100vw - 32px))}
dialog::backdrop{background:#0007}
.dh{padding:14px 16px;border-bottom:1px solid var(--line);font-weight:650}
.dpath{padding:8px 16px;font:13px ui-monospace,Consolas,monospace;color:var(--mut);word-break:break-all}
.dl{max-height:50vh;overflow:auto;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.dl div{padding:8px 16px;cursor:pointer;display:flex;gap:8px}.dl div:hover{background:var(--acc2)}
.df{padding:12px 16px;display:flex;gap:8px;justify-content:flex-end}
.chk{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13px;margin-top:14px}
.pbar{height:6px;background:var(--line);border-radius:99px;overflow:hidden;margin:10px 0 6px}.pbar i{display:block;height:100%;background:var(--acc);border-radius:99px}
.prow{display:flex;gap:14px;flex-wrap:wrap;align-items:center;justify-content:space-between}
h3.f{font-size:14px;margin:22px 0 0;padding-top:16px;border-top:1px solid var(--line)}
footer{max-width:980px;margin:auto;padding:0 16px 40px;color:var(--mut);font-size:12px}
.modos{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:700px){.modos{grid-template-columns:1fr}}
.modo{border:1px solid var(--line);border-radius:10px;padding:14px;background:var(--in)}
.modo>b{display:block;font-size:15px;margin-bottom:2px}
.tag{display:inline-block;font-size:11px;font-weight:650;padding:2px 8px;border-radius:99px;margin-left:6px;vertical-align:2px}
.tag.ya{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}.tag.scan{background:var(--acc2);color:var(--acc)}
.vrow{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
.vrow:last-child{border-bottom:0}
.vrow .vt{flex:1;min-width:0}.vrow .vt b{font-size:14px}.vrow .vt div{font-size:12px;color:var(--mut)}
.vrow .mv{display:flex;gap:4px}.vrow .mv button{padding:2px 8px}
.vrow input[type=checkbox]{width:18px;height:18px;flex:none}
.plant{display:grid;grid-template-columns:200px 1fr;gap:8px 14px;align-items:center;margin-top:12px}
@media(max-width:700px){.plant{grid-template-columns:1fr}}
.plant .ej{font-size:13px;color:var(--mut);grid-column:2}@media(max-width:700px){.plant .ej{grid-column:1}}
.plant .ej b{color:var(--tx);font-weight:600}
.chips{font-size:12px;color:var(--mut);margin-top:10px;line-height:1.9}.chips code{background:var(--in);border:1px solid var(--line);border-radius:6px;padding:1px 6px;cursor:pointer;word-break:normal;overflow-wrap:anywhere}
.prio{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:10px}
.prio ol{margin:6px 0 0;padding-left:20px;font-size:13px}.prio li{margin:2px 0}
.ed-niv{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.ed-niv select{width:auto;min-width:170px;flex:1}
.ed-niv .fl{color:var(--mut)}
.ed-f{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;margin-top:6px}@media(max-width:600px){.ed-f{grid-template-columns:1fr}}
.prev{margin-top:12px;font-size:13px;background:var(--in);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-height:44px}
dialog.ancho{width:min(680px,calc(100vw - 32px))}
.dbody{padding:4px 16px 14px;max-height:70vh;overflow:auto}
.nueva{display:none;margin-bottom:16px}.nueva b{display:inline;font-size:inherit;margin:0}
</style></head><body>
<header>
  <svg class="logo" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="15" fill="#c2410c"/><g fill="none" stroke="#fff" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"><path d="M25 45V18l20-4v24"/><circle cx="19.5" cy="45" r="5.5"/><circle cx="39.5" cy="38" r="5.5"/></g><g fill="none" stroke="#ffd7bd" stroke-width="3" stroke-linecap="round"><path d="M50 22a10 10 0 0 1 0 12"/><path d="M54.5 17.5a16 16 0 0 1 0 21"/></g></svg>
  <div><h1 id="nombre">SupaVic DLNA</h1><div class="sub" id="sub" data-i="cargando"></div></div>
  <div class="hd-r"><a class="lnk" id="l-dash" href="#" target="_blank" rel="noopener" style="display:none">Dashboard ↗</a><span class="pill" id="estado">●</span></div>
</header>
<nav><button data-t="estado" class="on" data-i="tab.estado"></button><button data-t="carpetas" data-i="tab.carpetas"></button><button data-t="nav" data-i="tab.nav"></button><button data-t="config" data-i="tab.config"></button><button data-t="diag" data-i="tab.diag"></button><button data-t="log">Log</button></nav>
<main>
<section id="t-estado" class="on">
  <div class="card bienv nueva" id="nueva"></div>
  <div class="card bienv" id="bienv"><b data-i="bienv.t"></b><span data-i="bienv.d"></span><div class="acts" style="margin-top:12px"><button class="btn pri" id="bienv-btn" data-i="bienv.btn"></button></div></div>
  <div class="grid">
    <div class="card"><div class="k" data-i="k.pistas"></div><div class="v" id="s-pistas">–</div></div>
    <div class="card"><div class="k" data-i="k.portadas"></div><div class="v" id="s-port">–</div></div>
    <div class="card"><div class="k" data-i="k.activo"></div><div class="v" id="s-up">–</div></div>
    <div class="card"><div class="k" data-i="k.carpetas"></div><div class="v" id="s-carp">–</div></div>
  </div>
  <h2 data-i="h.indice"></h2>
  <div class="grid" id="s-idx"></div>
  <h2 data-i="h.precarga"></h2>
  <div class="card" id="s-pre"></div>
  <h2 data-i="h.disp"></h2>
  <div class="card" style="padding:4px 12px"><table><thead><tr><th>IP</th><th data-i="th.ultima"></th><th data-i="th.sonando"></th></tr></thead><tbody id="s-cli"></tbody></table></div>
  <h2 data-i="h.red"></h2>
  <div class="card" id="s-red"></div>
</section>

<section id="t-carpetas">
  <p class="mut" style="margin-top:0" data-i="carp.intro"></p>
  <div class="card"><ul class="lista" id="c-lista"></ul>
    <div class="acts"><button class="btn" id="c-explorar" data-i="btn.agregar"></button><button class="btn pri" id="c-guardar" data-i="btn.guardar"></button></div>
    <div class="msg" id="c-msg"></div></div>
  <p class="mut" style="font-size:13px" data-i="carp.idx.ayuda"></p>
</section>

<section id="t-nav">
  <p class="mut" style="margin-top:0" data-i="n.intro"></p>
  <div class="card">
    <h2 style="margin-top:0" data-i="n.modos"></h2>
    <div class="modos">
      <div class="modo"><b><span data-i="n.carp.t"></span><span class="tag ya" data-i="n.ya"></span></b><span class="mut" style="font-size:13px" data-i="n.carp.d"></span>
        <label class="chk"><input type="checkbox" id="v-carp"> <span data-i="v.carp"></span></label>
        <label for="f-orden" data-i="f.orden"></label><input type="number" id="f-orden" min="0"></div>
      <div class="modo"><b><span data-i="n.tags.t"></span><span class="tag scan" id="n-prog"></span></b><span class="mut" style="font-size:13px" data-i="n.tags.d"></span>
        <div class="mut" style="font-size:13px;margin-top:10px" id="n-lect"></div></div>
    </div>
  </div>

  <h2 data-i="n.vistas"></h2>
  <div class="card">
    <p class="mut" style="margin:0 0 6px" data-i="n.vistas.d"></p>
    <div id="v-lista"></div>
    <div class="acts"><button class="btn" id="v-nueva" data-i="n.nueva"></button></div>
    <h3 class="f" data-i="n.listas"></h3>
    <p class="mut" style="margin:6px 0 0;font-size:13px" data-i="n.listas.d"></p>
    <div class="row">
      <div><label for="v-modo" data-i="v.modo"></label><select id="v-modo"><option value="lista" data-i="v.modo.lista"></option><option value="letras" data-i="v.modo.letras"></option></select></div>
      <div id="v-agr-c"><label for="v-agr" data-i="v.agr"></label><input type="number" id="v-agr" min="1"></div>
    </div>
  </div>

  <h2 data-i="n.titulos"></h2>
  <div class="card">
    <p class="mut" style="margin:0" data-i="n.titulos.d"></p>
    <div class="plant">
      <label for="pl-raiz" style="margin:0" data-i="pl.raiz"></label><div class="ed-niv"><select id="pls-raiz"></select><input type="text" id="pl-raiz" maxlength="120"></div><div class="ej" id="ej-raiz"></div>
      <label for="pl-cont" style="margin:0" data-i="pl.cont"></label><div class="ed-niv"><select id="pls-cont"></select><input type="text" id="pl-cont" maxlength="120"></div><div class="ej" id="ej-cont"></div>
      <label for="pl-album" style="margin:0" data-i="pl.album"></label><div class="ed-niv"><select id="pls-album"></select><input type="text" id="pl-album" maxlength="120"></div><div class="ej" id="ej-album"></div>
      <label for="pl-pista" style="margin:0" data-i="pl.pista"></label><div class="ed-niv"><select id="pls-pista"></select><input type="text" id="pl-pista" maxlength="120"></div><div class="ej" id="ej-pista"></div>
    </div>
    <div class="chips" id="pl-campos"></div>
    <div class="row">
      <div><label for="v-conteo" data-i="v.conteo"></label><select id="v-conteo"><option value="pistas" data-i="v.conteo.pistas"></option><option value="elementos" data-i="v.conteo.elem"></option></select></div>
      <div></div>
    </div>
    <label class="chk"><input type="checkbox" id="v-alpista"> <span data-i="v.alpista"></span></label>
    <label class="chk"><input type="checkbox" id="v-carpig"> <span data-i="v.carpig"></span></label>
    <label class="chk"><input type="checkbox" id="v-discos"> <span data-i="v.discos"></span></label>
    <label class="chk"><input type="checkbox" id="v-numseg"> <span data-i="v.numseg"></span></label>
    <h3 class="f" data-i="n.artmost"></h3>
    <p class="mut" style="margin:4px 0 0;font-size:13px" data-i="n.artmost.d"></p>
    <div class="row">
      <div><label for="v-tart" data-i="v.tart"></label><input type="text" id="v-tart" maxlength="200"></div>
      <div><label for="v-taart" data-i="v.taart"></label><input type="text" id="v-taart" maxlength="200"></div>
    </div>
  </div>

  <h2 data-i="n.port"></h2>
  <div class="card">
    <p class="mut" style="margin:0" data-i="n.port.d"></p>
    <div class="prio">
      <div class="modo"><b data-i="pr.pista"></b><ol data-i="pr.pista.l"></ol></div>
      <div class="modo"><b data-i="pr.album"></b><ol data-i="pr.album.l"></ol></div>
      <div class="modo"><b data-i="pr.artista"></b><ol data-i="pr.artista.l"></ol></div>
    </div>
    <label class="chk"><input type="checkbox" id="c-pista"> <span data-i="c.pista"></span></label>
    <label class="chk"><input type="checkbox" id="c-sub"> <span data-i="c.sub"></span></label>
    <label class="chk"><input type="checkbox" id="c-art"> <span data-i="c.art"></span></label>
    <label class="chk"><input type="checkbox" id="f-crop"> <span data-i="f.crop"></span></label>
    <div class="row"><div><label for="f-tam" data-i="f.tam"></label><input type="number" id="f-tam" min="0" max="2000"></div><div></div></div>
  </div>
  <div class="acts"><button class="btn pri" id="n-guardar" data-i="n.guardar"></button></div>
  <div class="msg" id="n-msg"></div>
  <h2 data-i="h.m3u"></h2>
  <div class="card"><p class="mut" style="margin-top:0" data-i="m3u.d"></p><div id="m3u"></div></div>
</section>

<section id="t-config">
  <div class="card">
    <div class="row">
      <div><label for="f-nombre" data-i="f.nombre"></label><input type="text" id="f-nombre" maxlength="64"></div>
      <div><label for="f-idioma" data-i="f.idioma"></label><select id="f-idioma"><option value="auto" data-i="f.idioma.auto"></option><option value="es">Español</option><option value="en">English</option></select></div>
    </div>
    <div class="row">
      <div><label for="f-puerto" data-i="f.puerto"></label><input type="number" id="f-puerto" min="1024" max="65535"></div>
      <div><label for="f-ip" data-i="f.ip"></label><input type="text" id="f-ip" data-ip="f.ip.ph"></div>
    </div>
    <label for="f-cache" data-i="f.cache"></label><input type="text" id="f-cache" data-ip="f.cache.ph">
    <label class="chk"><input type="checkbox" id="f-abrir"> <span data-i="f.abrir"></span></label>
    <label class="chk"><input type="checkbox" id="f-dash"> <span data-i="f.dash"></span></label>
    <label class="chk"><input type="checkbox" id="f-act"> <span data-i="f.act"></span></label>
    <h3 class="f" data-i="p.titulo"></h3>
    <label class="chk"><input type="checkbox" id="p-activa"> <span data-i="p.activa"></span></label>
    <div class="row">
      <div><label for="p-min" data-i="p.min"></label><input type="number" id="p-min" min="0.1" step="0.5"></div>
      <div><label for="p-horas" data-i="p.horas"></label><input type="number" id="p-horas" min="0" step="1"></div>
    </div>
    <label class="chk"><input type="checkbox" id="p-port"> <span data-i="p.port"></span></label>
    <label for="f-ign" data-i="f.ign"></label><textarea id="f-ign"></textarea>
    <div class="acts"><button class="btn pri" id="f-guardar" data-i="btn.guardarAj"></button></div>
    <div class="msg" id="f-msg"></div>
  </div>
  <h2 data-i="h.mant"></h2>
  <div class="card"><p class="mut" style="margin-top:0" data-i="mant.txt"></p>
    <div class="acts" style="margin-top:0"><button class="btn" id="m-cache" data-i="btn.borrar"></button><button class="btn" id="m-reini" data-i="btn.reiniciar"></button></div>
    <div class="msg" id="m-msg"></div>
    <h3 class="f" data-i="h.export"></h3><p class="mut" style="margin:4px 0 0;font-size:13px" data-i="export.d"></p>
    <div class="acts"><button class="btn" id="m-exp" data-i="btn.exportar"></button><button class="btn" id="m-imp" data-i="btn.importar"></button><input type="file" id="m-imp-f" accept=".json,application/json" hidden></div>
    <div class="msg" id="m-imsg"></div>
    <h3 class="f" data-i="h.clave"></h3><p class="mut" style="margin:4px 0 0;font-size:13px" data-i="clave.d"></p>
    <div class="row" style="margin-top:8px;flex-wrap:wrap;gap:8px">
      <input type="password" id="k-actual" autocomplete="current-password" data-ph="clave.actual" style="max-width:220px" hidden>
      <input type="password" id="k-nueva" autocomplete="new-password" data-ph="clave.nueva" style="max-width:220px"></div>
    <div class="acts"><button class="btn" id="k-guardar" data-i="btn.clave"></button><button class="btn" id="k-quitar" data-i="btn.quitarclave" hidden></button><button class="btn" id="k-salir" data-i="btn.salir" hidden></button></div>
    <div class="msg" id="k-msg"></div></div>
</section>

<section id="t-diag"><div class="card"><p class="mut" style="margin-top:0" data-i="diag.txt"></p>
  <div class="acts" style="margin-top:0"><button class="btn" id="d-run" data-i="btn.diag"></button></div><div class="msg" id="d-msg"></div></div>
  <div id="d-res"></div></section>
<section id="t-log"><div class="acts" style="margin:0 0 10px"><button class="btn sm" id="l-ref" data-i="btn.actualizar"></button></div><div id="log"></div></section>
</main>
<footer id="pie"></footer>

<dialog id="ed" class="ancho"><div class="dh" id="ed-tit"></div><div class="dbody">
  <label for="ed-nombre" data-i="ed.nombre"></label><input type="text" id="ed-nombre" maxlength="60">
  <label data-i="ed.niveles"></label>
  <div class="ed-niv" id="ed-niv"></div>
  <label data-i="ed.filtro"></label>
  <div id="ed-filtros"></div><button class="btn sm" id="ed-addf" style="margin-top:6px" data-i="ed.addf"></button>
  <label for="ed-plant" data-i="ed.plant"></label><input type="text" id="ed-plant" maxlength="120" data-ip="ed.plant.ph">
  <label class="chk"><input type="checkbox" id="ed-sin"> <span data-i="ed.sin"></span></label>
  <div class="prev" id="ed-prev"></div>
</div><div class="df"><button class="btn" id="ed-borrar" data-i="ed.borrar" style="margin-right:auto"></button><button class="btn" id="ed-cancel" data-i="dlg.cancelar"></button><button class="btn pri" id="ed-ok" data-i="ed.ok"></button></div></dialog>

<dialog id="dlg"><div class="dh" data-i="dlg.titulo"></div><div class="dpath" id="d-ruta"></div><div class="dl" id="d-lista"></div>
<div class="df"><button class="btn" id="d-cancel" data-i="dlg.cancelar"></button><button class="btn pri" id="d-ok" data-i="dlg.usar"></button></div></dialog>

<script>
const LANG="__LANG__";
const TXT={
es:{"tab.nav":"Navegación","n.intro":"Así se ve tu música en el WiiM (o en cualquier app DLNA). Hay dos formas de navegar y se pueden usar juntas.",
"n.modos":"Dos formas de navegar","n.carp.t":"📁 Carpetas","n.ya":"al instante","n.carp.d":"Tal cual están en tu disco. No necesita escaneo: cada carpeta se lee en el momento en que la abres, siempre fiel a lo que hay.",
"n.tags.t":"🏷 Vistas por etiquetas","n.tags.d":"Artistas, álbumes, compositores… se arman con los tags de tus archivos. Se llenan solas mientras el servidor lee la biblioteca en segundo plano (y al abrir carpetas). En <b>Carpetas</b> eliges cuáles entran (Indexar tags).",
"n.prog":"{p}% leído","n.completa":"completas","n.lect":"Pistas con tags leídos: <b>{a}</b> · en las vistas: <b>{b}</b>. Lectura en segundo plano: {e}.",
"n.vistas":"Vistas en la raíz","n.vistas.d":"Marca las que quieres ver en el reproductor y ordénalas con las flechas. Las vistas por tag (compositor, sello…) solo muestran algo si tus archivos tienen ese tag.",
"n.nueva":"+ Nueva vista personalizada","btn.editar":"Editar","v.custom":"personalizada","subir":"Subir","bajar":"Bajar",
"vd.artistas":"Artista del álbum › álbumes › pistas","vd.interpretes":"Cada artista de pista (featuring, compilaciones) › sus álbumes › sus pistas","vd.albumes":"Todos los álbumes, de la A a la Z",
"vd.generos":"Género › artistas › álbumes","vd.decadas":"Década › álbumes por año","vd.recientes":"Los últimos 200 álbumes que llegaron a tu disco (ENCODINGTIME si existe; si no, fecha de creación del archivo)",
"vd.modificados":"Los últimos 200 álbumes cuyos archivos cambiaron (tags editados, archivos reemplazados)","vd.tocadas":"Las últimas 100 pistas que sonaron en tus reproductores","vd.mas_tocadas":"Las 100 pistas que más has escuchado","vd.hires":"Álbumes de más de 16 bits o 48 kHz","vd.aleatorio":"100 pistas al azar (cambian cada 10 minutos)","vd.playlists":"Las playlists (.m3u, .m3u8, .pls) de tus carpetas de música, con su portada (Nombre.jpg junto a Nombre.m3u8) y la opción «Por álbum». Solo aparece si hay alguna",
"v.discos":"Álbumes de varios discos: una carpeta por disco antes de sus pistas («» Supersonic», o «» Disco 2» si no tiene subtítulo) <span class=\"hint\">con su portada (Supersonic.jpg); para abrir o encolar un disco solo. Play sigue tocando el álbum completo</span>",
"v.numseg":"Álbumes de varios discos: numerar las pistas seguido (1…N) en vez de por disco <span class=\"hint\">para reproductores que mezclan los discos al ordenar, como mconnect</span>",
"vd.compositores":"Compositor › álbumes › pistas","vd.directores":"Director de orquesta › álbumes › pistas","vd.sellos":"Sello discográfico › álbumes › pistas","vd.calidad":"DSD / Hi-Res / CD / comprimido › artistas › álbumes",
"vd.tag":"tag {tag}: {n} pistas","vd.tag0":"tag {tag}: ninguna pista todavía",
"n.titulos":"Cómo se ven los títulos","n.titulos.d":"Elige un formato o escribe el tuyo. Lo que va entre llaves se reemplaza; si un dato no existe, desaparece junto con sus paréntesis.",
"pl.raiz":"Entradas de la raíz","pl.cont":"Artistas, géneros, carpetas…","pl.album":"Álbumes","pl.custom":"Personalizado","pl.ej":"Ejemplo:",
"pl.pista":"Pistas","v.carpig":"Si dos álbumes se verían con el mismo título, agregar el nombre de su carpeta <span class=\"hint\">p. ej. dos ediciones de «21 [2011]»</span>","v.alpista":"Las pistas muestran el álbum con este mismo formato <span class=\"hint\">para reproductores que muestran el álbum en cada pista (BubbleUPnP, upplay)</span>",
"n.artmost":"Nombre de artista que se muestra","n.artmost.d":"Si tus archivos traen el crédito tal como está impreso en el disco (p. ej. Picard: <code>DISPLAY ARTIST</code>), se muestra ese; si no, el de ARTIST. Las listas de Artistas siguen usando cada artista por separado. Escribe los tags separados por comas, el primero que tenga valor gana.",
"v.tart":"En pistas","v.taart":"En álbumes",
"h.clave":"Contraseña del panel (opcional)","clave.d":"Para que nadie más en tu red cambie los ajustes. Desde esta misma computadora no se pide nunca, y los reproductores no la necesitan. Si la olvidas, borra el archivo datos/clave.txt.","clave.actual":"Contraseña actual","clave.nueva":"Contraseña nueva","btn.clave":"Guardar contraseña","btn.quitarclave":"Quitar contraseña","btn.salir":"Cerrar sesión","clave.ok":"Contraseña guardada.","clave.quitada":"El panel ya no pide contraseña.","clave.on":"🔒 Protegido con contraseña.",
"h.export":"Exportar / importar ajustes","export.d":"Guarda todos tus ajustes (carpetas, vistas, formatos…) en un archivo, o cárgalos de otro. Al importar se conservan el puerto y la carpeta de caché de esta instalación.",
"btn.exportar":"Exportar ajustes","btn.importar":"Importar ajustes…","import.mal":"Ese archivo no parece un archivo de ajustes de SupaVic.",
"pl.campos":"Campos: <code>{nombre}</code> <code>{n}</code> el número elegido abajo · <code>{pistas}</code> <code>{albumes}</code> · en álbumes: <code>{album}</code> <code>{año}</code> <code>{años}</code> (original/edición: 1975/2005) <code>{artista}</code> <code>{calidad}</code> (24/96, DSD64, MP3 320k) <code>{formato}</code> <code>{genero}</code> <code>{originaldate}</code> <code>{date}</code> y cualquier tag de tus archivos, como <code>{label}</code> o <code>{composer}</code> · <code>.decade</code> / <code>.year</code> sobre cualquier fecha: <code>{originaldate.decade}</code> → 1960s · en pistas: <code>{titulo}</code> <code>{numero}</code> <code>{disco}</code> <code>{artista}</code> <code>{album}</code> <code>{año}</code> · <b>&lt;…&gt;</b> = texto opcional, solo aparece si alguno de sus campos tiene valor: <code>{album}&lt; • {version}&gt;&lt; ({remasterdate} Remaster)&gt;</code>",
"v.conteo":"Qué cuenta <code>{n}</code>","v.conteo.pistas":"Canciones","v.conteo.elem":"Elementos (álbumes de un artista, artistas de un género…)","v.agr":"A partir de <span class=\"hint\">elementos en la lista</span>","n.listas":"Listas largas","n.listas.d":"Artistas, álbumes, compositores, sellos y tus vistas. Completa: todo en una sola lista (desplazas o buscas). Por letra: primero A, B, C… y adentro los de esa letra, más una entrada \"Todos\" con la lista completa.","v.modo":"Cómo mostrarlas","v.modo.lista":"Lista completa","v.modo.letras":"Agrupadas por letra (A, B, C…)",
"n.port":"Portadas","n.port.d":"El servidor busca la imagen en este orden y usa la primera que encuentra. Tus archivos nunca se modifican.",
"pr.pista":"Cada pista","pr.pista.l":"<li>Su portada embebida (frontal)</li><li>La imagen de su disco: <code>&lt;subtítulo del disco&gt;.jpg</code></li><li>Otra embebida (etiqueta del CD, ilustración)</li><li>La imagen de su carpeta</li>",
"pr.album":"Álbumes y carpetas","pr.album.l":"<li><code>cover</code>, <code>folder</code> o <code>front</code> (.jpg/.png/.webp)</li><li>Cualquier otra imagen de la carpeta</li><li>La portada embebida de sus pistas</li><li>Contraportada, CD, booklet… (último recurso)</li><li>La primera de sus subcarpetas (si está activado abajo)</li>",
"pr.artista":"Artistas","pr.artista.l":"<li><code>artist.jpg</code> en la carpeta del artista o del álbum</li><li><code>folder.jpg</code> de una carpeta con el nombre del artista</li><li>Foto del artista embebida en sus pistas</li><li>La portada de su primer álbum</li>",
"c.pista":"Cada pista muestra su propia portada embebida <span class=\"hint\">desactívalo para que las pistas de un álbum usen la del álbum: menos imágenes que generar</span>",
"c.sub":"Carpetas sin imagen propia: usar la primera de sus subcarpetas <span class=\"hint\">la carpeta de un artista muestra la portada de uno de sus álbumes</span>",
"c.art":"Imagen de artista en Artistas, Compositores, Directores… <span class=\"hint\">si no, la portada de su primer álbum</span>",
"n.guardar":"Guardar navegación","f.act":"Avisar en este panel cuando haya una versión nueva <span class=\"hint\">tu navegador consulta GitHub; el servidor no se conecta a internet</span>",
"nueva":"Hay una versión nueva: <b>{v}</b> (tienes la {a}). <a class=\"lnk\" href=\"{u}\" target=\"_blank\" rel=\"noopener\">Ver novedades y descargar ↗</a>",
"ed.nuevo":"Nueva vista","ed.editar":"Editar vista","ed.nombre":"Nombre en el reproductor","ed.niveles":"Niveles <span class=\"hint\">después del último siempre vienen los álbumes y luego las pistas</span>",
"ed.nivel.no":"— ninguno","ed.album":"› Álbumes › Pistas","ed.filtro":"Filtro <span class=\"hint\">opcional: solo las pistas que cumplan todas las condiciones</span>","ed.addf":"+ Condición","ed.quitar":"Quitar",
"ed.plant":"Título de los álbumes en esta vista <span class=\"hint\">opcional; vacío = el formato general</span>","ed.plant.ph":"p. ej. {album} — {label}",
"ed.sin":"Mostrar al final \"[Sin …]\" con las pistas que no tienen el tag del primer nivel","ed.borrar":"Borrar vista","ed.ok":"Guardar vista","ed.confirm":"¿Borrar esta vista?",
"ed.prev":"Vista previa del primer nivel","ed.prev.n":"{t} valores · {p} pistas","ed.prev.sin":" · {n} sin ese tag","ed.prev.vacio":"Ninguna pista tiene ese tag todavía (o aún no se han leído sus tags).","ed.calc":"Calculando…","ed.falta":"Elige al menos un nivel.",
"op.es":"es","op.contiene":"contiene","op.no_contiene":"no contiene","op.existe":"existe","op.falta":"no existe","op.mayor":"≥","op.menor":"≤","pistas.n":"{n} pistas",
"cargando":"cargando…","enlinea":"● En línea","sinconexion":"● Sin conexión","tab.estado":"Estado","tab.carpetas":"Carpetas","tab.config":"Ajustes","tab.diag":"Diagnóstico","h.m3u":"Playlists para presets","m3u.d":"Guarda estas direcciones como preset en el WiiM (o en otro reproductor que acepte playlists por URL). «Al azar» trae 100 pistas distintas cada vez que lo tocas.","copiar":"Copiar","copiado":"Copiado ✓","diag.txt":"Revisa tu biblioteca con lo que el índice ya sabe, sin leer los archivos: álbumes partidos por tags que no coinciden, duplicados, pistas faltantes, álbumes sin portada, pistas sin tags y archivos que no se pueden leer.","btn.diag":"Revisar biblioteca","diag.espera":"El índice todavía se está armando. Intenta en un momento.","diag.ok":"Todo en orden en {p} pistas. 🎉","diag.res":"{n} cosas para revisar en {p} pistas y {a} álbumes.","diag.mas":"…y {n} más.",
"diag.partidos":"Álbumes partidos","diag.partidos.d":"Una carpeta con un mismo álbum que sale varias veces: el artista del álbum (albumartist) no es igual en todas sus pistas.",
"diag.duplicados":"Álbumes repetidos","diag.duplicados.d":"El mismo título y artista en varias carpetas: un duplicado o dos ediciones distintas.",
"diag.incompletos":"Pistas faltantes","diag.incompletos.d":"Huecos en la numeración (1, 2, 4… sin la 3).",
"diag.sin_portada":"Sin portada","diag.sin_portada.d":"Ni imagen en su carpeta ni portada embebida en sus pistas.",
"diag.sin_tags":"Pistas sin tags","diag.sin_tags.d":"Les falta título, artista o álbum (se muestran con el nombre del archivo).",
"diag.ilegibles":"Archivos ilegibles","diag.ilegibles.d":"No se pudieron leer: dañados, incompletos o en un formato no soportado. Conviene volver a bajarlos.",
"vd.favoritas":"Las pistas con 4 o 5 estrellas (tags RATING o FMPS_RATING de Picard, foobar2000, MediaMonkey…). Solo aparece si hay alguna",
"bienv.t":"Agrega tu carpeta de música para empezar","bienv.d":"Elige una o varias carpetas; aparecerán en tu reproductor (WiiM Home, BubbleUPnP…) con el nombre de este servidor.","bienv.btn":"Elegir carpetas",
"k.pistas":"Pistas en caché","k.portadas":"Portadas","k.activo":"Activo","k.carpetas":"Carpetas","h.indice":"Biblioteca por etiquetas","h.precarga":"Precarga en reposo","h.disp":"Dispositivos",
"th.ultima":"Última actividad","th.sonando":"Sonando / acción","h.red":"Red",
"carp.idx":"Indexar tags","carp.idx.tip":"Desmarcado: la carpeta se sigue navegando, pero sus pistas no aparecen en Artistas, Álbumes, Géneros, búsqueda ni tus vistas",
"carp.idx.ayuda":"<b>Indexar tags:</b> decide qué carpetas alimentan las vistas por etiquetas (Artistas, Álbumes, Géneros, Recientes, tus vistas y la búsqueda). Desmárcala en carpetas como descargas o playlists para que no llenen esas vistas de álbumes sueltos o repetidos: se siguen navegando igual por carpetas, con tags y portadas, y lo que toques de ellas sigue contando en Reproducido recientemente y Más tocadas. Además, no se leen en segundo plano. Guarda para aplicar: las vistas se actualizan en unos segundos.",
"carp.intro":"Se muestran tal cual en tu reproductor. No hace falta indexar antes: los tags y portadas se leen al abrir cada carpeta, quedan en caché y el resto se precarga cuando el servidor está inactivo.",
"btn.agregar":"Agregar carpeta…","btn.guardar":"Guardar","f.nombre":"Nombre en la red","f.idioma":"Idioma <span class=\"hint\">(reinicia el servidor)</span>","f.idioma.auto":"Automático (idioma del sistema)",
"f.puerto":"Puerto HTTP <span class=\"hint\">(reinicia el servidor)</span>","f.ip":"IP fija <span class=\"hint\">vacío = todas las interfaces (recomendado)</span>","f.ip.ph":"ej. 192.168.1.50",
"f.tam":"Tamaño de portadas (px) <span class=\"hint\">0 = original</span>","f.orden":"Ordenar por número de pista (tags) las carpetas de hasta <span class=\"hint\">canciones. Las más grandes se ordenan por nombre de archivo para que abran al instante · 0 = siempre por nombre</span>",
"f.cache":"Carpeta de caché <span class=\"hint\">tags y portadas; mejor en un SSD (reinicia el servidor)</span>","f.cache.ph":"vacío = carpeta de datos",
"f.crop":"Recortar portadas no cuadradas (miniaturas 16:9 de YouTube)","f.abrir":"Abrir este panel al iniciar el servidor a mano","f.dash":"Iniciar el dashboard junto con el servidor (icono de bandeja)",
"p.titulo":"Precarga en reposo","p.activa":"Llenar la caché (tags y portadas) cuando el servidor esté inactivo","p.min":"Minutos sin navegación para empezar","p.horas":"Repetir pasada completa cada (horas) <span class=\"hint\">detecta música nueva</span>","p.port":"Generar también las portadas",
"v.titulo":"Vistas por etiquetas <span class=\"hint\">aparecen en la raíz, después de las carpetas</span>","v.carp":"Agrupar las carpetas dentro de una entrada \"Carpetas\" en la raíz",
"f.ign":"Ignorar <span class=\"hint\">un patrón por línea, sin distinguir mayúsculas (*.part, .*, nombres exactos)</span>","btn.guardarAj":"Guardar ajustes","h.mant":"Mantenimiento",
"mant.txt":"Borrar la caché obliga a releer tags y portadas al navegar (útil si cambiaste tags sin modificar los archivos).","btn.borrar":"Borrar caché","btn.reiniciar":"Reiniciar servidor","btn.actualizar":"Actualizar",
"dlg.titulo":"Elegir carpeta","dlg.cancelar":"Cancelar","dlg.usar":"Usar esta carpeta","dlg.unidades":"Unidades","dlg.vacia":"Sin subcarpetas",
"ahora":"ahora","hace.min":"hace {n} min","hace.h":"hace {n} h","sinPillow":"sin Pillow","reproduciendo":"reproduciendo","navegando":"navegando","descubrimiento":"descubrimiento","ningunDisp":"Ningún dispositivo se ha conectado todavía.",
"i.pistas":"Pistas","i.albumes":"Álbumes","i.artistas":"Artistas","i.generos":"Géneros","i.construyendo":"Construyendo índice…","i.pendiente":"El índice se construye al abrir una vista en el reproductor.",
"pre.esperando":"Esperando {n} min sin navegación","pre.pausada":"Pausada por actividad · retoma en la siguiente inactividad","pre.escaneando":"Escaneando","pre.completa":"Caché completa","pre.desactivada":"Desactivada",
"pre.transfer":" (esperando a que terminen las transferencias)","pre.ya":"Iniciar ahora","pre.prog":"{a} de {b} carpetas · {p} pistas leídas · {c} portadas generadas en esta pasada","pre.ultima":"Última pasada completa: {x}","nunca":"nunca",
"red.anunciado":"Anunciado en","red.ninguna":"Ninguna interfaz","red.cache":"Caché","red.carpetas":"Carpetas","disponible":"disponible","nodisponible":"no disponible",
"sinCarpetas":"Sin carpetas. Agrega al menos una.","quitar":"Quitar","guardado":"Guardado.","reiniciando":"Reiniciando…","error":"Error","cacheBorrada":"Caché borrada ({n} portadas).","vacio":"(vacío)",
"pie":"SupaVic DLNA {v} · Python {py} · datos en {d}","novedades":"Novedades"},
en:{"tab.nav":"Browsing","n.intro":"This is how your music looks on the WiiM (or any DLNA app). There are two ways to browse, and you can use both.",
"n.modos":"Two ways to browse","n.carp.t":"📁 Folders","n.ya":"instant","n.carp.d":"Exactly as they are on your disk. No scan needed: each folder is read the moment you open it, always true to what's there.",
"n.tags.t":"🏷 Tag views","n.tags.d":"Artists, albums, composers… built from your files' tags. They fill in by themselves while the server reads the library in the background (and as you open folders). Choose which folders take part under <b>Folders</b> (Index tags).",
"n.prog":"{p}% read","n.completa":"complete","n.lect":"Tracks with tags read: <b>{a}</b> · in the views: <b>{b}</b>. Background reading: {e}.",
"n.vistas":"Views at the root","n.vistas.d":"Tick the ones you want on your player and order them with the arrows. Tag views (composer, label…) only show something if your files have that tag.",
"n.nueva":"+ New custom view","btn.editar":"Edit","v.custom":"custom","subir":"Move up","bajar":"Move down",
"vd.artistas":"Album artist › albums › tracks","vd.interpretes":"Every track artist (featuring, compilations) › their albums › their tracks","vd.albumes":"All albums, A to Z",
"vd.generos":"Genre › artists › albums","vd.decadas":"Decade › albums by year","vd.recientes":"The last 200 albums that arrived on your disk (ENCODINGTIME if present; otherwise the file's creation date)",
"vd.modificados":"The last 200 albums whose files changed (edited tags, replaced files)","vd.tocadas":"The last 100 tracks played on your players","vd.mas_tocadas":"The 100 tracks you've listened to the most","vd.hires":"Albums over 16-bit or 48 kHz","vd.aleatorio":"100 random tracks (they change every 10 minutes)","vd.playlists":"The playlists (.m3u, .m3u8, .pls) in your music folders, with their artwork (Name.jpg next to Name.m3u8) and a “By album” option. Only shown when there is one",
"v.discos":"Multi-disc albums: a folder for each disc before its tracks (“» Supersonic”, or “» Disc 2” without a subtitle) <span class=\"hint\">with its own artwork (Supersonic.jpg), to open or queue one disc. Play still plays the whole album</span>",
"v.numseg":"Multi-disc albums: number tracks straight through (1…N) instead of per disc <span class=\"hint\">for players that mix discs up when sorting, like mconnect</span>",
"vd.compositores":"Composer › albums › tracks","vd.directores":"Conductor › albums › tracks","vd.sellos":"Record label › albums › tracks","vd.calidad":"DSD / Hi-Res / CD / compressed › artists › albums",
"vd.tag":"{tag} tag: {n} tracks","vd.tag0":"{tag} tag: no tracks yet",
"n.titulos":"How titles look","n.titulos.d":"Pick a format or write your own. Whatever is in braces gets replaced; if a value doesn't exist, it disappears together with its brackets.",
"pl.raiz":"Root entries","pl.cont":"Artists, genres, folders…","pl.album":"Albums","pl.custom":"Custom","pl.ej":"Example:",
"pl.pista":"Tracks","v.carpig":"If two albums would show the same title, add their folder name <span class=\"hint\">e.g. two editions of “21 [2011]”</span>","v.alpista":"Tracks show their album with this same format <span class=\"hint\">for players that show the album on each track (BubbleUPnP, upplay)</span>",
"n.artmost":"Artist name shown","n.artmost.d":"If your files carry the credit as printed on the release (e.g. Picard: <code>DISPLAY ARTIST</code>), that's what's shown; otherwise ARTIST. The Artists lists still use each artist separately. Enter tags separated by commas; the first one with a value wins.",
"v.tart":"On tracks","v.taart":"On albums",
"h.clave":"Panel password (optional)","clave.d":"So nobody else on your network can change the settings. Never asked on this same computer, and players don't need it. If you forget it, delete the file datos/clave.txt.","clave.actual":"Current password","clave.nueva":"New password","btn.clave":"Save password","btn.quitarclave":"Remove password","btn.salir":"Log out","clave.ok":"Password saved.","clave.quitada":"The panel no longer asks for a password.","clave.on":"🔒 Password protected.",
"h.export":"Export / import settings","export.d":"Save all your settings (folders, views, title formats…) to a file, or load them from one. Importing keeps this install's port and cache folder.",
"btn.exportar":"Export settings","btn.importar":"Import settings…","import.mal":"That file doesn't look like a SupaVic settings file.",
"pl.campos":"Fields: <code>{name}</code> <code>{count}</code> the number chosen below · <code>{tracks}</code> <code>{albums}</code> · for albums: <code>{album}</code> <code>{year}</code> <code>{years}</code> (original/release: 1975/2005) <code>{artist}</code> <code>{quality}</code> (24/96, DSD64, MP3 320k) <code>{format}</code> <code>{genre}</code> <code>{originaldate}</code> <code>{date}</code> and any tag in your files, like <code>{label}</code> or <code>{composer}</code> · <code>.decade</code> / <code>.year</code> on any date: <code>{originaldate.decade}</code> → 1960s · for tracks: <code>{title}</code> <code>{track}</code> <code>{disc}</code> <code>{artist}</code> <code>{album}</code> <code>{year}</code> · <b>&lt;…&gt;</b> = optional text, shown only when one of its fields has a value: <code>{album}&lt; • {version}&gt;&lt; ({remasterdate} Remaster)&gt;</code>",
"v.conteo":"What <code>{count}</code> counts","v.conteo.pistas":"Songs","v.conteo.elem":"Items (an artist's albums, a genre's artists…)","v.agr":"Starting at <span class=\"hint\">items in the list</span>","n.listas":"Long lists","n.listas.d":"Artists, albums, composers, labels and your own views. Full list: everything in one list (scroll or search). By letter: first A, B, C… and inside each one its items, plus an \"All\" entry with the full list.","v.modo":"How to show them","v.modo.lista":"Full list","v.modo.letras":"Grouped by letter (A, B, C…)",
"n.port":"Covers","n.port.d":"The server looks for the image in this order and uses the first one it finds. Your files are never modified.",
"pr.pista":"Each track","pr.pista.l":"<li>Its embedded cover (front)</li><li>Its disc image: <code>&lt;disc subtitle&gt;.jpg</code></li><li>Another embedded image (CD label, illustration)</li><li>Its folder's image</li>",
"pr.album":"Albums and folders","pr.album.l":"<li><code>cover</code>, <code>folder</code> or <code>front</code> (.jpg/.png/.webp)</li><li>Any other image in the folder</li><li>Its tracks' embedded cover</li><li>Back cover, CD, booklet… (last resort)</li><li>The first one in its subfolders (if enabled below)</li>",
"pr.artista":"Artists","pr.artista.l":"<li><code>artist.jpg</code> in the artist's or album's folder</li><li><code>folder.jpg</code> in a folder named like the artist</li><li>An artist photo embedded in their tracks</li><li>The cover of their first album</li>",
"c.pista":"Each track shows its own embedded cover <span class=\"hint\">turn off so an album's tracks use the album cover: fewer images to generate</span>",
"c.sub":"Folders without their own image: use the first one in their subfolders <span class=\"hint\">an artist folder shows one of their album covers</span>",
"c.art":"Artist image in Artists, Composers, Conductors… <span class=\"hint\">otherwise, the cover of their first album</span>",
"n.guardar":"Save browsing","f.act":"Tell me in this panel when there's a new version <span class=\"hint\">your browser checks GitHub; the server never goes online</span>",
"nueva":"A new version is out: <b>{v}</b> (you have {a}). <a class=\"lnk\" href=\"{u}\" target=\"_blank\" rel=\"noopener\">See what's new and download ↗</a>",
"ed.nuevo":"New view","ed.editar":"Edit view","ed.nombre":"Name on the player","ed.niveles":"Levels <span class=\"hint\">after the last one come the albums, then the tracks</span>",
"ed.nivel.no":"— none","ed.album":"› Albums › Tracks","ed.filtro":"Filter <span class=\"hint\">optional: only tracks that match every condition</span>","ed.addf":"+ Condition","ed.quitar":"Remove",
"ed.plant":"Album titles in this view <span class=\"hint\">optional; empty = the general format</span>","ed.plant.ph":"e.g. {album} — {label}",
"ed.sin":"Show \"[No …]\" at the end with the tracks that lack the first level's tag","ed.borrar":"Delete view","ed.ok":"Save view","ed.confirm":"Delete this view?",
"ed.prev":"Preview of the first level","ed.prev.n":"{t} values · {p} tracks","ed.prev.sin":" · {n} without that tag","ed.prev.vacio":"No track has that tag yet (or its tags haven't been read yet).","ed.calc":"Working…","ed.falta":"Pick at least one level.",
"op.es":"is","op.contiene":"contains","op.no_contiene":"doesn't contain","op.existe":"exists","op.falta":"doesn't exist","op.mayor":"≥","op.menor":"≤","pistas.n":"{n} tracks",
"cargando":"loading…","enlinea":"● Online","sinconexion":"● Offline","tab.estado":"Status","tab.carpetas":"Folders","tab.config":"Settings","tab.diag":"Health check","h.m3u":"Playlists for presets","m3u.d":"Save these addresses as presets on the WiiM (or any player that takes playlists by URL). “Random” brings 100 different tracks every time you play it.","copiar":"Copy","copiado":"Copied ✓","diag.txt":"Checks your library with what the index already knows, without reading the files: albums split by mismatched tags, duplicates, missing tracks, albums without artwork, tracks without tags and files that can't be read.","btn.diag":"Check library","diag.espera":"The index is still being built. Try again in a moment.","diag.ok":"All good across {p} tracks. 🎉","diag.res":"{n} things to look at across {p} tracks and {a} albums.","diag.mas":"…and {n} more.",
"diag.partidos":"Split albums","diag.partidos.d":"One folder with the same album showing up several times: the album artist (albumartist) isn't the same on all its tracks.",
"diag.duplicados":"Repeated albums","diag.duplicados.d":"Same title and artist in several folders: a duplicate or two different editions.",
"diag.incompletos":"Missing tracks","diag.incompletos.d":"Gaps in the numbering (1, 2, 4… without 3).",
"diag.sin_portada":"No artwork","diag.sin_portada.d":"No image in its folder and no artwork embedded in its tracks.",
"diag.sin_tags":"Tracks without tags","diag.sin_tags.d":"Missing title, artist or album (shown with the file name).",
"diag.ilegibles":"Unreadable files","diag.ilegibles.d":"Couldn't be read: damaged, incomplete or in an unsupported format. Worth downloading again.",
"vd.favoritas":"Tracks with 4 or 5 stars (RATING or FMPS_RATING tags from Picard, foobar2000, MediaMonkey…). Only shown when there is one",
"bienv.t":"Add your music folder to get started","bienv.d":"Pick one or more folders; they'll show up on your player (WiiM Home, BubbleUPnP…) under this server's name.","bienv.btn":"Choose folders",
"k.pistas":"Tracks cached","k.portadas":"Covers","k.activo":"Uptime","k.carpetas":"Folders","h.indice":"Tag library","h.precarga":"Idle prefetch","h.disp":"Devices",
"th.ultima":"Last activity","th.sonando":"Playing / action","h.red":"Network",
"carp.idx":"Index tags","carp.idx.tip":"Unticked: you can still browse the folder, but its tracks don't show up in Artists, Albums, Genres, search or your views",
"carp.idx.ayuda":"<b>Index tags:</b> choose which folders feed the tag views (Artists, Albums, Genres, Recently added, your own views and search). Untick it for folders like downloads or playlists so they don't fill those views with loose or duplicate albums: you can still browse them by folder, with tags and covers, and what you play from them still counts in Recently played and Most played. They also aren't read in the background. Save to apply: the views update within seconds.",
"carp.intro":"They appear as-is on your player. No up-front indexing: tags and covers are read when each folder is opened, cached, and the rest is prefetched while the server is idle.",
"btn.agregar":"Add folder…","btn.guardar":"Save","f.nombre":"Network name","f.idioma":"Language <span class=\"hint\">(restarts the server)</span>","f.idioma.auto":"Automatic (system language)",
"f.puerto":"HTTP port <span class=\"hint\">(restarts the server)</span>","f.ip":"Fixed IP <span class=\"hint\">empty = all interfaces (recommended)</span>","f.ip.ph":"e.g. 192.168.1.50",
"f.tam":"Cover size (px) <span class=\"hint\">0 = original</span>","f.orden":"Sort by track number (tags) in folders with up to <span class=\"hint\">songs. Bigger folders are sorted by file name so they open instantly · 0 = always by name</span>",
"f.cache":"Cache folder <span class=\"hint\">tags and covers; best on an SSD (restarts the server)</span>","f.cache.ph":"empty = data folder",
"f.crop":"Crop non-square covers (16:9 YouTube thumbnails)","f.abrir":"Open this panel when the server is started manually","f.dash":"Start the dashboard with the server (tray icon)",
"p.titulo":"Idle prefetch","p.activa":"Fill the cache (tags and covers) while the server is idle","p.min":"Minutes without browsing before starting","p.horas":"Repeat a full pass every (hours) <span class=\"hint\">picks up new music</span>","p.port":"Generate covers too",
"v.titulo":"Tag views <span class=\"hint\">shown at the root, after the folders</span>","v.carp":"Put folders inside a \"Folders\" entry at the root",
"f.ign":"Ignore <span class=\"hint\">one pattern per line, case-insensitive (*.part, .*, exact names)</span>","btn.guardarAj":"Save settings","h.mant":"Maintenance",
"mant.txt":"Clearing the cache forces tags and covers to be read again while browsing (useful if you edited tags without changing the files).","btn.borrar":"Clear cache","btn.reiniciar":"Restart server","btn.actualizar":"Refresh",
"dlg.titulo":"Choose folder","dlg.cancelar":"Cancel","dlg.usar":"Use this folder","dlg.unidades":"Drives","dlg.vacia":"No subfolders",
"ahora":"now","hace.min":"{n} min ago","hace.h":"{n} h ago","sinPillow":"no Pillow","reproduciendo":"playing","navegando":"browsing","descubrimiento":"discovery","ningunDisp":"No device has connected yet.",
"i.pistas":"Tracks","i.albumes":"Albums","i.artistas":"Artists","i.generos":"Genres","i.construyendo":"Building index…","i.pendiente":"The index is built when a view is opened on the player.",
"pre.esperando":"Waiting for {n} min without browsing","pre.pausada":"Paused by activity · resumes at the next idle period","pre.escaneando":"Scanning","pre.completa":"Cache complete","pre.desactivada":"Disabled",
"pre.transfer":" (waiting for transfers to finish)","pre.ya":"Start now","pre.prog":"{a} of {b} folders · {p} tracks read · {c} covers generated this pass","pre.ultima":"Last full pass: {x}","nunca":"never",
"red.anunciado":"Announced on","red.ninguna":"No interface","red.cache":"Cache","red.carpetas":"Folders","disponible":"available","nodisponible":"not available",
"sinCarpetas":"No folders yet. Add at least one.","quitar":"Remove","guardado":"Saved.","reiniciando":"Restarting…","error":"Error","cacheBorrada":"Cache cleared ({n} covers).","vacio":"(empty)",
"pie":"SupaVic DLNA {v} · Python {py} · data in {d}","novedades":"What's new"}}[LANG]||{};
const t=(k,o)=>{let s=TXT[k]??k;if(o)for(const x in o)s=s.split("{"+x+"}").join(o[x]);return s};
const LOC=(()=>{const d=LANG==="es"?"es-MX":"en-US",l=navigator.language||"";try{if(l.toLowerCase().startsWith(LANG)){new Intl.NumberFormat(l);return l}}catch(e){}return d})();
const nf=n=>Number(n||0).toLocaleString(LOC);
document.querySelectorAll("[data-i]").forEach(e=>e.innerHTML=t(e.dataset.i));
document.querySelectorAll("[data-ph]").forEach(e=>e.placeholder=t(e.dataset.ph));
document.querySelectorAll("[data-ip]").forEach(e=>e.placeholder=t(e.dataset.ip));
const $=s=>document.querySelector(s);
let cfg=null, carpetas=[], sinIdx=new Set(), dRuta="", vistasDisp=[], vistasX=[], catTags=null, orden=[], marcadas=new Set(), navListo=false, verAvisada=false;
const PRESET_TAG={compositores:"composer",directores:"conductor",sellos:"label"};
const PLANTS={raiz:["{nombre}","{nombre} ({n})"],
  cont:["{nombre} ({n})","{nombre}","{nombre} ({pistas})"],
  album:["{album} [{año}]","{album} [{años}]","{album} ({año})","{año} - {album}","{album}","{album} [{año}] {calidad}","{album} [{años}]< • {version}>","{album} [{año}] ({pistas})"],
  pista:["{titulo}","{titulo} ({subtitle})","{titulo} ({version})","{numero}. {titulo}"]};
const EJ={raiz:{nombre:LANG==="es"?"Artistas":"Artists",n:1234},
  cont:{nombre:"Miles Davis",n:12,pistas:96,albumes:12},
  album:{album:"Kind of Blue",año:1959,años:"1959/1997",año_edicion:1997,artista:"Miles Davis",pistas:5,n:5,calidad:"24/96",formato:"FLAC",genero:"Jazz",label:"Columbia",composer:"Miles Davis",version:"Remaster",fecha:"1997-04-29",fecha_original:"1959-08-17",originalrecordingdate:"1959-03-02"},
  pista:{titulo:"So What",nombre:"So What",numero:1,disco:1,artista:"Miles Davis",album:"Kind of Blue",año:1959,subtitle:"Take 3",version:"Mono"}};
const ALIAS={date:"fecha",originaldate:"fecha_original",original_date:"fecha_original",originalyear:"año",name:"nombre",count:"n",tracks:"pistas",albums:"albumes","álbumes":"albumes",year:"año",anio:"año",years:"años",anios:"años",release_year:"año_edicion",anio_edicion:"año_edicion",artist:"artista",quality:"calidad",format:"formato",genre:"genero","género":"genero","álbum":"album",title:"titulo","título":"titulo",track:"numero",tracknumber:"numero","número":"numero",disc:"disco",discnumber:"disco"};
const MODS={decade:"d",decada:"d","década":"d",year:"y","año":"y",anio:"y"};
function valor(c,k){k=k.trim().toLowerCase();const a=ALIAS[k]||k;if(c[a]!==undefined||!k.includes("."))return c[a];
  const i=k.lastIndexOf("."),md=MODS[k.slice(i+1).trim()];if(!md)return c[a];const b=k.slice(0,i);const m=String(c[ALIAS[b]||b]??"").match(/(?<!\d)(\d{4})(?!\d)/);
  return m?(md==="d"?Math.floor(+m[1]/10)*10+"s":m[1]):""}
function plantilla(tpl,c0){const c=Object.assign({},c0);let ng=0;
  tpl=tpl.replace(/<([^<>]*\{[^{}]{1,40}\}[^<>]*)>/g,(m,g)=>{let hay=false,vac=false;let r=g.replace(/\{([^{}]{1,40})\}/g,(mm,k)=>{const v=valor(c,k);if(v===undefined||v===""||v===0){vac=true;return""}hay=true;return String(v)});
    if(hay&&vac)r=r.replace(/\s*(\(\s*\)|\[\s*\]|\{\s*\})/g,"");const k="\u0001g"+(ng++);c[k]=hay?r:"";return"{"+k+"}"});
  let vacio=false;let s=tpl.replace(/\{([^{}]{1,40})\}/g,(m,k)=>{const v=k.startsWith("\u0001")?c[k]:valor(c,k);k=ALIAS[k.trim().toLowerCase()]||k;if(v===undefined||v===""||v===0){vacio=true;return""}return typeof v==="number"&&["n","pistas","albumes"].includes(k)?nf(v):String(v)});
  if(vacio)s=s.replace(/\s*(\(\s*\)|\[\s*\]|\{\s*\})/g,"").replace(/\s+/g," ").replace(/^[\s\-·•|,;/]+|[\s\-·•|,;/]+$/g,"");return s||c.nombre||c.album||""}
const tagNombre=tg=>(catTags&&catTags.nombres&&catTags.nombres[tg])||tg.replace(/^#/,"");
const tagPistas=tg=>{const x=catTags&&catTags.tags.find(z=>z.tag===tg);return x?x.pistas:0};
function nombreVista(k){const c=(cfg.vistas.personalizadas||[]).find(x=>x.id===k);if(c)return c.nombre;const d=vistasDisp.find(x=>x[0]===k);return d?d[1]:k}
function descVista(k){const c=(cfg.vistas.personalizadas||[]).find(x=>x.id===k);
  if(c){let d=c.niveles.map(tagNombre).join(" › ")+" › "+t("ed.album").replace(/^›\s*/,"");if(c.filtro&&c.filtro.length)d+=" · "+c.filtro.map(f=>`${tagNombre(f.tag)} ${t("op."+f.op)} ${f.valor||""}`.trim()).join(", ");return h(d)}
  let d=h(t("vd."+k));const tg=PRESET_TAG[k];if(tg&&catTags){const n=tagPistas(tg);d+=` · <span class="${n?"":"bad"}">${h(t(n?"vd.tag":"vd.tag0",{tag:tg,n:nf(n)}))}</span>`}return d}
function todasVistas(){const ids=[...vistasDisp.map(x=>x[0]),...(cfg.vistas.personalizadas||[]).map(x=>x.id)];const out=[...cfg.vistas.activas.filter(k=>ids.includes(k))];ids.forEach(k=>{if(!out.includes(k))out.push(k)});return out}
function pintarVistas(){
  if(!cfg||!vistasDisp.length)return;
  if(!navListo){orden=todasVistas();marcadas=new Set(cfg.vistas.activas);navListo=true}
  orden=orden.filter(k=>vistasDisp.some(x=>x[0]===k)||(cfg.vistas.personalizadas||[]).some(x=>x.id===k));
  $("#v-lista").innerHTML=orden.map((k,i)=>{const cu=k[0]==="c"&&(cfg.vistas.personalizadas||[]).some(x=>x.id===k);
    return `<div class="vrow"><input type="checkbox" data-k="${k}" ${marcadas.has(k)?"checked":""} aria-label="${h(nombreVista(k))}">
    <div class="vt"><b>${h(nombreVista(k))}</b>${cu?`<span class="tag scan">${t("v.custom")}</span>`:""}<div>${descVista(k)}</div></div>
    ${cu?`<button class="btn sm" data-ed="${k}">${t("btn.editar")}</button>`:""}
    <div class="mv"><button class="btn sm" data-up="${i}" title="${t("subir")}" ${i?"":"disabled"}>↑</button><button class="btn sm" data-dn="${i}" title="${t("bajar")}" ${i<orden.length-1?"":"disabled"}>↓</button></div></div>`}).join("");
  $("#v-lista").querySelectorAll("input").forEach(x=>x.onchange=()=>{x.checked?marcadas.add(x.dataset.k):marcadas.delete(x.dataset.k)});
  $("#v-lista").querySelectorAll("[data-up]").forEach(b=>b.onclick=()=>{const i=+b.dataset.up;[orden[i-1],orden[i]]=[orden[i],orden[i-1]];pintarVistas()});
  $("#v-lista").querySelectorAll("[data-dn]").forEach(b=>b.onclick=()=>{const i=+b.dataset.dn;[orden[i+1],orden[i]]=[orden[i],orden[i+1]];pintarVistas()});
  $("#v-lista").querySelectorAll("[data-ed]").forEach(b=>b.onclick=()=>abrirEditor(b.dataset.ed));
}
const A_EN={nombre:"name",n:"count",pistas:"tracks",albumes:"albums","año":"year","años":"years","año_edicion":"release_year",artista:"artist",calidad:"quality",formato:"format",genero:"genre",titulo:"title",numero:"track",disco:"disc"};
const locPlant=tp=>LANG==="es"?tp:tp.replace(/\{([^{}]+)\}/g,(m,k)=>"{"+(A_EN[k]||k)+"}");
function prepPlant(id,clave){const sel=$("#pls-"+id),inp=$("#pl-"+id);
  const opts=PLANTS[id].map(locPlant);sel.innerHTML=opts.map(o=>`<option value="${h(o)}">${h(plantilla(o,EJ[id]))}</option>`).join("")+`<option value="">${t("pl.custom")}</option>`;
  const pintar=()=>{sel.value=opts.includes(inp.value)?inp.value:"";$("#ej-"+id).innerHTML=`${t("pl.ej")} <b>${h(plantilla(inp.value,EJ[id]))}</b>`};
  sel.onchange=()=>{if(sel.value)inp.value=sel.value;pintar();if(!sel.value)inp.focus()};inp.oninput=pintar;
  inp.value=locPlant(cfg.vistas.plantillas[clave]||PLANTS[id][0]);pintar()}
function cargarNav(){
  $("#v-carp").checked=cfg.musica.agrupar_carpetas;$("#f-orden").value=cfg.musica.ordenar_por_tags_hasta;
  $("#v-agr").value=cfg.vistas.agrupar_desde||200;$("#v-modo").value=cfg.vistas.agrupar_letras?"letras":"lista";
  const modo=()=>{$("#v-agr-c").style.visibility=$("#v-modo").value==="letras"?"visible":"hidden"};$("#v-modo").onchange=modo;modo();
  $("#v-conteo").value=cfg.vistas.tipo_conteo||"pistas";
  prepPlant("raiz","raiz");prepPlant("cont","contenedor");prepPlant("album","album");prepPlant("pista","pista");
  $("#v-alpista").checked=!!cfg.vistas.album_en_pistas;$("#v-carpig").checked=cfg.vistas.carpeta_en_iguales!==false;$("#v-discos").checked=!!cfg.vistas.discos;$("#v-numseg").checked=!!cfg.vistas.numeracion_seguida;$("#v-tart").value=cfg.vistas.tags_artista||"";$("#v-taart").value=cfg.vistas.tags_artista_album||"";
  $("#pl-campos").innerHTML=t("pl.campos");
  $("#c-pista").checked=cfg.portadas.por_pista;$("#c-sub").checked=cfg.portadas.subcarpetas;$("#c-art").checked=cfg.portadas.artista;
  $("#f-tam").value=cfg.portadas.tamano;$("#f-crop").checked=cfg.portadas.recortar_cuadrado;
  navListo=false;pintarVistas();
}
function leerNav(n){
  n.musica.agrupar_carpetas=$("#v-carp").checked;n.musica.ordenar_por_tags_hasta=+$("#f-orden").value;
  if(navListo)n.vistas.activas=orden.filter(k=>marcadas.has(k));
  n.vistas.agrupar_letras=$("#v-modo").value==="letras";n.vistas.agrupar_desde=Math.max(1,+$("#v-agr").value||200);n.vistas.tipo_conteo=$("#v-conteo").value;
  n.vistas.plantillas={raiz:$("#pl-raiz").value,contenedor:$("#pl-cont").value,album:$("#pl-album").value,pista:$("#pl-pista").value};
  n.vistas.album_en_pistas=$("#v-alpista").checked;n.vistas.carpeta_en_iguales=$("#v-carpig").checked;n.vistas.discos=$("#v-discos").checked;n.vistas.numeracion_seguida=$("#v-numseg").checked;n.vistas.tags_artista=$("#v-tart").value;n.vistas.tags_artista_album=$("#v-taart").value;
  n.portadas.por_pista=$("#c-pista").checked;n.portadas.subcarpetas=$("#c-sub").checked;n.portadas.artista=$("#c-art").checked;
  n.portadas.tamano=+$("#f-tam").value;n.portadas.recortar_cuadrado=$("#f-crop").checked;return n}
async function cargarTags(){try{catTags=await api("/api/tags");pintarVistasForzar()}catch(e){}}
function pintarVistasForzar(){if(cfg&&navListo)pintarVistas()}
// ---- editor de vistas personalizadas
let edId=null,edTimer=null;
function opcionesTag(sel,vacio){const tags=(catTags&&catTags.tags)||[];
  return (vacio?`<option value="">${t("ed.nivel.no")}</option>`:"")+tags.map(x=>`<option value="${h(x.tag)}" ${x.tag===sel?"selected":""}>${h(tagNombre(x.tag))}${x.tag.startsWith("#")||tagNombre(x.tag)===x.tag||["albumartist","artist","genre","year"].includes(x.tag)?"":" ("+h(x.tag)+")"} — ${h(t("pistas.n",{n:nf(x.pistas)}))}</option>`).join("")
    +(sel&&!tags.some(x=>x.tag===sel)?`<option value="${h(sel)}" selected>${h(sel)}</option>`:"")}
function pintarNiveles(niv){$("#ed-niv").innerHTML=[0,1,2].map(i=>`<select data-n="${i}" aria-label="${i+1}">${opcionesTag(niv[i]||"",i>0)}</select>`).join('<span class="fl">›</span>')+`<span class="fl">${t("ed.album")}</span>`;
  $("#ed-niv").querySelectorAll("select").forEach(x=>x.onchange=previaLuego)}
function filaFiltro(f){const d=document.createElement("div");d.className="ed-f";
  d.innerHTML=`<select class="ft">${opcionesTag(f.tag||"",false)}</select><select class="fo">${["es","contiene","no_contiene","existe","falta","mayor","menor"].map(o=>`<option value="${o}" ${o===f.op?"selected":""}>${t("op."+o)}</option>`).join("")}</select><input type="text" class="fv" value="${h(f.valor||"")}" maxlength="80"><button class="btn sm">${t("ed.quitar")}</button>`;
  d.querySelector("button").onclick=()=>{d.remove();previaLuego()};d.querySelectorAll("select,input").forEach(x=>{x.onchange=previaLuego;x.oninput=previaLuego});$("#ed-filtros").appendChild(d)}
function leerEditor(){const niv=[...$("#ed-niv").querySelectorAll("select")].map(x=>x.value).filter(Boolean);
  const filtro=[...$("#ed-filtros").querySelectorAll(".ed-f")].map(d=>({tag:d.querySelector(".ft").value,op:d.querySelector(".fo").value,valor:d.querySelector(".fv").value.trim()})).filter(f=>f.tag);
  return {id:edId||"",nombre:$("#ed-nombre").value.trim(),niveles:niv,filtro,plantilla_album:$("#ed-plant").value.trim(),sin_tag:$("#ed-sin").checked}}
function previaLuego(){clearTimeout(edTimer);edTimer=setTimeout(previa,350)}
async function previa(){const v=leerEditor();if(!v.niveles.length){$("#ed-prev").innerHTML=`<span class="mut">${t("ed.falta")}</span>`;return}
  $("#ed-prev").innerHTML=`<span class="mut">${t("ed.calc")}</span>`;
  const r=await post("/api/vista-previa",v);
  $("#ed-prev").innerHTML=`<b>${t("ed.prev")}</b> <span class="mut">${h(t("ed.prev.n",{t:nf(r.total),p:nf(r.pistas)}))}${r.sin_tag?h(t("ed.prev.sin",{n:nf(r.sin_tag)})):""}</span><br>`+
    (r.valores.length?r.valores.map(([n,c])=>`${h(n)} <span class="mut">(${nf(c)})</span>`).join(" · ")+(r.total>r.valores.length?" …":""):`<span class="mut">${t("ed.prev.vacio")}</span>`)}
async function abrirEditor(id){if(!catTags)await cargarTags();edId=id||null;
  const v=(cfg.vistas.personalizadas||[]).find(x=>x.id===id)||{nombre:"",niveles:["composer"],filtro:[],plantilla_album:"",sin_tag:false};
  $("#ed-tit").textContent=t(id?"ed.editar":"ed.nuevo");$("#ed-nombre").value=v.nombre;pintarNiveles(v.niveles);
  $("#ed-filtros").innerHTML="";(v.filtro||[]).forEach(filaFiltro);$("#ed-plant").value=v.plantilla_album||"";$("#ed-sin").checked=!!v.sin_tag;
  $("#ed-borrar").style.display=id?"":"none";$("#ed").showModal();previa()}
$("#v-nueva").onclick=()=>abrirEditor(null);
$("#ed-addf").onclick=()=>{filaFiltro({tag:"genre",op:"contiene",valor:""});previaLuego()};
$("#ed-cancel").onclick=()=>$("#ed").close();
$("#ed-ok").onclick=async()=>{const v=leerEditor();if(!v.niveles.length){previa();return}
  if(!v.nombre)v.nombre=v.niveles.map(tagNombre).join(" › ");
  const n=leerNav(structuredClone(cfg));const l=n.vistas.personalizadas||(n.vistas.personalizadas=[]);
  if(!v.id){v.id="c"+Math.random().toString(16).slice(2,9);l.push(v);n.vistas.activas.push(v.id)}else l.splice(l.findIndex(x=>x.id===v.id),1,v);
  $("#ed").close();await guardar(n,"#n-msg")};
$("#ed-borrar").onclick=async()=>{if(!edId||!confirm(t("ed.confirm")))return;const n=leerNav(structuredClone(cfg));
  n.vistas.personalizadas=n.vistas.personalizadas.filter(x=>x.id!==edId);n.vistas.activas=n.vistas.activas.filter(x=>x!==edId);$("#ed").close();await guardar(n,"#n-msg")};
$("#n-guardar").onclick=()=>guardar(leerNav(structuredClone(cfg)),"#n-msg");
function cmpVer(a,b){const p=x=>String(x).replace(/^v/,"").split(".").map(n=>parseInt(n)||0);const x=p(a),y=p(b);for(let i=0;i<3;i++){if((x[i]||0)!==(y[i]||0))return (x[i]||0)-(y[i]||0)}return 0}
async function buscarVersion(actual){if(verAvisada||!cfg||cfg.servidor.buscar_actualizaciones===false)return;verAvisada=true;
  try{const r=await fetch("https://api.github.com/repos/nomuraki/supavic-dlna/releases/latest",{headers:{Accept:"application/vnd.github+json"}});if(!r.ok)return;const j=await r.json();
    if(j.tag_name&&cmpVer(j.tag_name,actual)>0){const e=$("#nueva");e.innerHTML=t("nueva",{v:h(j.tag_name),a:h(actual),u:h(j.html_url||"https://github.com/nomuraki/supavic-dlna/releases")});e.style.display="block"}}catch(e){}}
const api=(u,o)=>fetch(u,o).then(r=>r.json());
const post=(u,b)=>api(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
const h=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function dur(s){const d=Math.floor(s/86400),hh=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${hh}h`:hh?`${hh}h ${m}m`:`${m}m`}
function hace(s){return s<60?t("ahora"):s<3600?t("hace.min",{n:Math.floor(s/60)}):t("hace.h",{n:Math.floor(s/3600)})}
function msg(id,txt,ok){const e=$(id);e.className="msg on "+(ok?"ok":"bad");e.innerHTML=txt}
function irA(tab){document.querySelector(`nav button[data-t="${tab}"]`)?.click()}

document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("nav button,section").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");$("#t-"+b.dataset.t).classList.add("on");
  try{localStorage.setItem("tab",b.dataset.t)}catch(e){}
  if(b.dataset.t==="log")cargarLog();
  if(b.dataset.t==="nav"){cargarTags();pintarM3u()}
});
function pintarM3u(){const base=`${location.protocol}//${location.host}`;const vs=["aleatorio","favoritas","recientes","tocadas","mas_tocadas","hires"];
  const en={aleatorio:"random",favoritas:"favourites",recientes:"recent",tocadas:"played",mas_tocadas:"most-played",hires:"hires"};
  const ruta=v=>document.documentElement.lang==="es"?v:en[v];
  const nombres=Object.fromEntries((estadoPanel&&estadoPanel.vistas_disponibles)||[]);
  $("#m3u").innerHTML=vs.map(v=>`<div class="row" style="align-items:center;gap:8px;margin-top:6px"><span style="min-width:150px">${h(nombres[v]||v)}</span><code class="url" style="flex:1;overflow-wrap:anywhere">${h(base)}/pl/${ruta(v)}.m3u8</code><button class="btn sm cp" data-u="${h(base)}/pl/${ruta(v)}.m3u8">${h(t("copiar"))}</button></div>`).join("");
  $("#m3u").querySelectorAll(".cp").forEach(b=>b.onclick=async()=>{try{await navigator.clipboard.writeText(b.dataset.u);b.textContent=t("copiado")}catch(e){prompt("",b.dataset.u)}})}
const DIAG=["partidos","duplicados","incompletos","sin_portada","sin_tags","ilegibles"];
$("#d-run").onclick=async()=>{const m=$("#d-msg");m.className="msg";m.textContent=t("cargando");$("#d-run").disabled=true;
  try{const r=await api("/api/diagnostico");
    if(!r.listo){m.textContent=t("diag.espera");return}
    const tot=DIAG.reduce((a,c)=>a+r.cuenta[c],0);
    m.className="msg "+(tot?"":"ok");m.textContent=tot?t("diag.res",{n:nf(tot),p:nf(r.pistas),a:nf(r.albumes)}):t("diag.ok",{p:nf(r.pistas)});
    $("#d-res").innerHTML=DIAG.filter(c=>r.cuenta[c]).map(c=>`<details class="card diag"><summary><b>${h(t("diag."+c))}</b> <span class="pill">${nf(r.cuenta[c])}</span><div class="mut" style="font-size:13px;margin-top:2px">${h(t("diag."+c+".d"))}</div></summary>`+
      `<ul>${r.items[c].map(x=>`<li><span>${h(x.t)}</span>${x.d?` <span class="mut">· ${h(x.d)}</span>`:""}<div class="mut ruta">${h(x.r)}</div></li>`).join("")}</ul>`+
      (r.cuenta[c]>r.items[c].length?`<p class="mut">${h(t("diag.mas",{n:nf(r.cuenta[c]-r.items[c].length)}))}</p>`:"")+`</details>`).join("")}
  catch(e){m.className="msg err";m.textContent=String(e)}finally{$("#d-run").disabled=false}};
try{const tb=localStorage.getItem("tab");tb&&irA(tb)}catch(e){}
$("#bienv-btn").onclick=()=>{irA("carpetas");$("#c-explorar").click()};

let estadoPanel=null;
async function estado(){
  try{
    const s=await api("/api/estado");
    if(s.error==="auth"){location.reload();return}
    if(!estadoPanel)pintarClave(s);estadoPanel=s;
    $("#nombre").textContent=s.nombre;document.title=`${s.nombre} · SupaVic DLNA`;
    $("#sub").textContent=`SupaVic DLNA ${s.version}${s.pillow?"":" · "+t("sinPillow")}`;
    $("#estado").textContent=t("enlinea");
    $("#pie").innerHTML=h(t("pie",{v:s.version,py:s.python,d:s.datos||""}))+` · <a class="lnk" href="https://github.com/nomuraki/supavic-dlna/blob/main/CHANGELOG.md" target="_blank" rel="noopener">${t("novedades")} ↗</a>`;
    const ld=$("#l-dash");if(s.dashboard&&s.dashboard.activo){ld.style.display="";ld.href=`${location.protocol}//${location.hostname}:${s.dashboard.puerto}/`}else ld.style.display="none";
    $("#bienv").style.display=s.carpetas.length?"none":"block";
    $("#s-pistas").textContent=nf(s.pistas_cache);
    $("#s-port").textContent=`${nf(s.portadas)} · ${s.portadas_mb} MB`;
    $("#s-up").textContent=dur(s.uptime);
    const disp=s.carpetas.filter(c=>c.disponible).length;
    $("#s-carp").innerHTML=`${disp}/${s.carpetas.length}`;
    const acc=a=>a.startsWith("▶")?t("reproduciendo"):(a==="navegando"||a==="descubrimiento")?t(a):a;
    $("#s-cli").innerHTML=s.clientes.length?s.clientes.map(c=>`<tr><td><code>${h(c.ip)}</code><div class="mut" style="font-size:12px">${h(c.ua||"")}</div></td><td>${hace(c.hace)}</td><td>${c.sonando?`<code>${h(c.sonando)}</code>`:""}<div class="mut" style="font-size:12px">${h(acc(c.accion))}</div></td></tr>`).join(""):`<tr><td colspan="3" class="mut">${t("ningunDisp")}</td></tr>`;
    const primera=!vistasDisp.length;vistasDisp=s.vistas_disponibles||vistasDisp;if(primera)pintarVistas();
    const pr0=s.precarga||{},lc=s.lectura||{};
    const pct=pr0.estado==="escaneando"&&pr0.total_carpetas?Math.round(100*pr0.carpetas/pr0.total_carpetas):null;
    $("#n-prog").textContent=pr0.estado==="completa"?t("n.completa"):pct!==null?t("n.prog",{p:pct}):"";$("#n-prog").style.display=$("#n-prog").textContent?"":"none";
    const est=!pr0.estado?"":pr0.estado==="esperando"?t("pre.esperando",{n:Math.ceil((pr0.espera_seg||0)/60)}):t("pre."+pr0.estado);
    $("#n-lect").innerHTML=t("n.lect",{a:nf(lc.cache),b:nf(lc.indice),e:h(est)});
    buscarVersion(s.version);
    const ix=s.indice||{};
    $("#s-idx").innerHTML=ix.listo?[["i.pistas",ix.pistas],["i.albumes",ix.albumes],["i.artistas",ix.artistas],["i.generos",ix.generos]]
      .map(([k,v])=>`<div class="card"><div class="k">${t(k)}</div><div class="v">${nf(v)}</div></div>`).join("")
      :`<div class="card mut">${ix.construyendo?t("i.construyendo"):t("i.pendiente")}</div>`;
    const pr=s.precarga;
    if(pr){
      const est=pr.estado==="esperando"?t("pre.esperando",{n:Math.ceil(pr.espera_seg/60)}):t("pre."+pr.estado);
      const pct=pr.total_carpetas?Math.round(100*pr.carpetas/pr.total_carpetas):0;
      const ult=pr.ultimo_completo?new Date(pr.ultimo_completo*1000).toLocaleString(LOC,{dateStyle:"short",timeStyle:"short"}):t("nunca");
      $("#s-pre").innerHTML=`<div class="prow"><div><b>${h(est)}</b>${pr.estado==="esperando"&&pr.espera_seg===0?t("pre.transfer"):""}</div>
        <button class="btn sm" id="pre-ya" ${pr.estado==="escaneando"||!pr.activa?"disabled":""}>${t("pre.ya")}</button></div>
        ${pr.total_carpetas?`<div class="pbar"><i style="width:${pr.estado==="completa"?100:pct}%"></i></div>
        <div class="mut" style="font-size:13px">${t("pre.prog",{a:nf(pr.estado==="completa"?pr.total_carpetas:pr.carpetas),b:nf(pr.total_carpetas),p:nf(pr.pistas),c:nf(pr.portadas)})}</div>`:""}
        ${pr.actual&&pr.estado==="escaneando"?`<div style="margin-top:6px"><code style="font-size:12px">${h(pr.actual)}</code></div>`:""}
        <div class="mut" style="font-size:12px;margin-top:6px">${t("pre.ultima",{x:h(ult)})}</div>`;
      const b=$("#pre-ya");if(b)b.onclick=async()=>{b.disabled=true;await post("/api/precarga-ahora");setTimeout(estado,1500)};
    }
    $("#s-red").innerHTML=`<div class="k">${t("red.anunciado")}</div>`+(s.ips.map(ip=>`<div><code>http://${h(ip)}:${s.puerto}/</code></div>`).join("")||`<div class="bad">${t("red.ninguna")}</div>`)
      +`<div class="k" style="margin-top:12px">${t("red.cache")}</div><div><code>${h(s.cache)}</code></div>`
      +`<div class="k" style="margin-top:12px">${t("red.carpetas")}</div>`+s.carpetas.map(c=>`<div><code>${h(c.ruta)}</code> <span class="${c.disponible?"ok":"bad"}">${c.disponible?t("disponible"):t("nodisponible")}</span></div>`).join("");
  }catch(e){$("#estado").textContent=t("sinconexion")}
}
function pintarCarpetas(){
  $("#c-lista").innerHTML=carpetas.length?carpetas.map((c,i)=>`<li><code>${h(c)}</code><label class="chk idx" title="${h(t("carp.idx.tip"))}"><input type="checkbox" data-i2="${i}"${sinIdx.has(c)?"":" checked"}> ${t("carp.idx")}</label><button class="btn sm" data-n="${i}">${t("quitar")}</button></li>`).join(""):`<li class="mut">${t("sinCarpetas")}</li>`;
  $("#c-lista").querySelectorAll("button").forEach(b=>b.onclick=()=>{sinIdx.delete(carpetas[+b.dataset.n]);carpetas.splice(+b.dataset.n,1);pintarCarpetas()});
  $("#c-lista").querySelectorAll("input[data-i2]").forEach(x=>x.onchange=()=>{const c=carpetas[+x.dataset.i2];x.checked?sinIdx.delete(c):sinIdx.add(c)});
}
async function cargarConfig(){
  cfg=await api("/api/config");carpetas=[...cfg.musica.carpetas];sinIdx=new Set(cfg.musica.sin_indexar||[]);pintarCarpetas();
  $("#f-nombre").value=cfg.servidor.nombre;$("#f-puerto").value=cfg.servidor.puerto;$("#f-ip").value=cfg.servidor.ip;$("#f-idioma").value=cfg.servidor.idioma||"auto";
  $("#f-cache").value=cfg.cache.directorio;cargarNav();
  $("#f-abrir").checked=cfg.servidor.abrir_navegador;$("#f-dash").checked=cfg.dashboard.con_servidor;$("#f-act").checked=cfg.servidor.buscar_actualizaciones!==false;
  $("#p-activa").checked=cfg.precarga.activa;$("#p-min").value=cfg.precarga.inactividad_min;
  $("#p-horas").value=cfg.precarga.repetir_horas;$("#p-port").checked=cfg.precarga.portadas;$("#f-ign").value=cfg.musica.ignorar.join("\n");
}
async function guardar(nuevo,id){
  const r=await post("/api/config",nuevo);
  if(!r.ok){msg(id,(r.errores||[t("error")]).map(h).join("<br>"),false);return}
  let tx=t("guardado")+(r.avisos?.length?"<br>"+r.avisos.map(h).join("<br>"):"");
  if(r.reiniciar){tx+="<br>"+t("reiniciando");const p=nuevo.servidor.puerto;
    setTimeout(()=>{location.href=`${location.protocol}//${location.hostname}:${p}/`},3500)}
  msg(id,tx,true);cargarConfig();estado();
}
$("#c-guardar").onclick=()=>{const n=structuredClone(cfg);n.musica.carpetas=carpetas;n.musica.sin_indexar=carpetas.filter(c=>sinIdx.has(c));guardar(n,"#c-msg")};
$("#f-guardar").onclick=()=>{const n=structuredClone(cfg);
  n.servidor.nombre=$("#f-nombre").value;n.servidor.puerto=+$("#f-puerto").value;n.servidor.ip=$("#f-ip").value.trim();n.servidor.idioma=$("#f-idioma").value;
  n.cache.directorio=$("#f-cache").value.trim();
  n.servidor.abrir_navegador=$("#f-abrir").checked;n.dashboard.con_servidor=$("#f-dash").checked;n.servidor.buscar_actualizaciones=$("#f-act").checked;
  n.precarga.activa=$("#p-activa").checked;n.precarga.inactividad_min=+$("#p-min").value;
  n.precarga.repetir_horas=+$("#p-horas").value;n.precarga.portadas=$("#p-port").checked;n.musica.ignorar=$("#f-ign").value.split("\n").map(s=>s.trim()).filter(Boolean);
  guardar(n,"#f-msg")};
$("#m-cache").onclick=async()=>{const r=await post("/api/limpiar-cache");msg("#m-msg",r.ok?t("cacheBorrada",{n:nf(r.portadas)}):t("error"),r.ok);estado()};
function pintarClave(s){const p=!!s.protegido;$("#k-actual").hidden=!p||s.local;$("#k-quitar").hidden=!p;$("#k-salir").hidden=!p||s.local;
  if(p)$("#k-msg").textContent=t("clave.on")}
async function claveEnviar(nueva){const r=await post("/api/clave",{actual:$("#k-actual").value,nueva});const m=$("#k-msg");
  m.className="msg "+(r.ok?"ok":"err");m.textContent=r.ok?t(nueva?"clave.ok":"clave.quitada"):(r.error||"Error");
  if(r.ok){$("#k-actual").value="";$("#k-nueva").value="";pintarClave(Object.assign({},estadoPanel||{},{protegido:r.protegido}))}}
$("#k-guardar").onclick=()=>{const v=$("#k-nueva").value;if(v)claveEnviar(v)};
$("#k-quitar").onclick=()=>claveEnviar("");
$("#k-salir").onclick=async()=>{await post("/api/salir");location.reload()};
$("#m-exp").onclick=async()=>{const c=await api("/api/config");const b=new Blob([JSON.stringify(c,null,2)],{type:"application/json"});
  const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=`supavic-settings-${new Date().toISOString().slice(0,10)}.json`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),2000)};
$("#m-imp").onclick=()=>$("#m-imp-f").click();
$("#m-imp-f").onchange=async e=>{const f=e.target.files[0];e.target.value="";if(!f)return;let d;
  try{d=JSON.parse(await f.text());if(!d||typeof d!=="object"||!d.musica||!d.vistas)throw 0}catch(_){msg("#m-imsg",t("import.mal"),false);return}
  const n=structuredClone(d);n.servidor=Object.assign({},n.servidor||{},{puerto:cfg.servidor.puerto});n.cache=Object.assign({},n.cache||{},{directorio:cfg.cache.directorio});
  guardar(n,"#m-imsg")};
$("#m-reini").onclick=async()=>{await post("/api/reiniciar");msg("#m-msg",t("reiniciando"),true);setTimeout(()=>location.reload(),3500)};

async function explorarA(ruta){
  const r=await api("/api/explorar?ruta="+encodeURIComponent(ruta));
  dRuta=r.ruta;$("#d-ruta").textContent=r.ruta||t("dlg.unidades");
  let html=r.padre!==null&&r.padre!==undefined?`<div data-r="${h(r.padre)}">↑ ..</div>`:"";
  html+=r.carpetas.map(c=>`<div data-r="${h(c.ruta)}">📁 ${h(c.nombre)}</div>`).join("");
  if(r.error)html+=`<div class="bad">${h(r.error)}</div>`;
  $("#d-lista").innerHTML=html||`<div class="mut">${t("dlg.vacia")}</div>`;
  $("#d-lista").querySelectorAll("[data-r]").forEach(d=>d.onclick=()=>explorarA(d.dataset.r));
  $("#d-ok").disabled=!dRuta;
}
$("#c-explorar").onclick=()=>{$("#dlg").showModal();explorarA(carpetas[carpetas.length-1]||"")};
$("#d-cancel").onclick=()=>$("#dlg").close();
$("#d-ok").onclick=()=>{if(dRuta&&!carpetas.includes(dRuta))carpetas.push(dRuta);pintarCarpetas();$("#dlg").close()};

async function cargarLog(){const r=await api("/api/log");const e=$("#log");e.textContent=r.lineas.join("\n")||t("vacio");e.scrollTop=e.scrollHeight}
$("#l-ref").onclick=cargarLog;
cargarConfig().then(()=>{if($("#t-nav").classList.contains("on"))cargarTags()});estado();setInterval(estado,5000);
</script></body></html>"""

FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="#c2410c"/>'
               '<g fill="none" stroke="#fff" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M25 45V18l20-4v24"/><circle cx="19.5" cy="45" r="5.5"/><circle cx="39.5" cy="38" r="5.5"/></g>'
               '<g fill="none" stroke="#ffd7bd" stroke-width="3" stroke-linecap="round">'
               '<path d="M50 22a10 10 0 0 1 0 12"/><path d="M54.5 17.5a16 16 0 0 1 0 21"/></g></svg>')


# ═══════════════════════════════════════════════════════════════════════════ main

def configurar_log(verbose):
    DIR_DATOS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(RUTA_LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:            # pythonw.exe no tiene consola
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def ping():
    """Imprime el puerto y devuelve 0 si el servidor responde (lo usan los .bat y launcher.py)."""
    import urllib.request
    try:
        cfg = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig"))
        puerto = int(cfg.get("servidor", {}).get("puerto", 8200))
        urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/estado", timeout=2).read()
        print(puerto)
        return 0
    except Exception:
        return 1


def abrir_panel():
    """Abre el panel en el navegador si servidor.abrir_navegador es true."""
    try:
        cfg = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig")).get("servidor", {})
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("abrir_navegador", True):
        import webbrowser
        webbrowser.open(f"http://localhost:{int(cfg.get('puerto', 8200))}/")
    return 0


class ErrorInicio(Exception):
    """El servidor no pudo arrancar (puerto ocupado, UDP 1900 bloqueado…)."""


def arrancar(verbose=False):
    """Arranca el servidor en hilos y devuelve la App. Lo usan main() y launcher.py (bandeja)."""
    configurar_log(verbose)
    app = App()
    log.info("─── %s %s (Python %s) · data: %s ───", APP, VERSION, sys.version.split()[0], DIR_BASE)
    if Image is None:
        log.warning("Pillow is not installed: covers are sent without resizing")
    if sys.getfilesystemencoding().lower().replace("-", "") != "utf8":
        log.warning("File names are decoded as %s, not UTF-8: names with accents or emoji may not show correctly. "
                    "Set LANG=C.UTF-8 (or run Python with -X utf8)", sys.getfilesystemencoding())
    if not app.cfg["musica"]["carpetas"]:
        log.warning("No music folders configured yet. Add them at http://127.0.0.1:%d/", app.puerto)

    httpd = None
    for intento in range(40):                     # espera a que suelte el puerto una instancia anterior
        try:
            httpd = Servidor(("0.0.0.0", app.puerto), Handler)
            break
        except OSError as e:
            if intento == 39:
                log.error("Could not open port %d: %s", app.puerto, e)
                raise ErrorInicio(f"Port {app.puerto}: {e}")
            time.sleep(0.5)
    httpd.app = app
    app.httpd = httpd
    try:
        app.ssdp = SSDP(app)
    except OSError as e:
        log.error("Could not open UDP 1900 for discovery (%s). Players won't see the server.", e)
        httpd.server_close()
        raise ErrorInicio(f"UDP 1900: {e}")
    RUTA_PID.write_text(str(os.getpid()), encoding="ascii")
    app.eventos.emitir("inicio", version=VERSION, nombre=app.cfg["servidor"]["nombre"], puerto=app.puerto,
                       pid=os.getpid())
    app.ssdp.start()
    app.precarga = Precargador(app)
    if Image is not None and app.cfg["precarga"]["activa"]:
        app.precarga_img = PrecargaImagenes(app)
        app.precarga_img.start()
    app.precarga.start()
    threading.Timer(3, lambda: app.indice.asegurar(espera=0)).start()   # índice listo antes de la 1ª navegación
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("'%s' ready · panel: http://%s:%d/", app.cfg["servidor"]["nombre"],
             (app.ssdp.ips or ["127.0.0.1"])[0], app.puerto)
    log.info("Cache: %s", app.dir_cache)
    for k, (r, _) in app.bib.raices.items():
        log.info("  folder %s %s%s", k, r, "" if os.path.isdir(r) else "  (not available)")
    return app


def detener(app):
    """Apagado ordenado: avisa a los controles (ssdp:byebye) y libera los puertos."""
    log.info("Exiting…")
    try:
        app.ssdp.notificar("ssdp:byebye")
        app.httpd.shutdown()
        app.httpd.server_close()
    except Exception:
        pass
    try:
        RUTA_PID.unlink()
    except OSError:
        pass


def main():
    if "--ping" in sys.argv:
        sys.exit(ping())
    if "--abrir-panel" in sys.argv:
        sys.exit(abrir_panel())
    try:
        app = arrancar("-v" in sys.argv or "--verbose" in sys.argv)
    except ErrorInicio:
        sys.exit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        detener(app)


if __name__ == "__main__":
    main()
