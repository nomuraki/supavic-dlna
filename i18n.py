# -*- coding: utf-8 -*-
"""Textos de SupaVic DLNA en español e inglés (lo que ve el usuario: el WiiM, el panel y la bandeja).

idioma = "auto" usa el idioma de Windows/del sistema; "es" o "en" lo fijan.
Los registros (log) se escriben siempre en inglés.
"""
import locale
import os

IDIOMAS = ("es", "en")
LANG = "en"

TEXTOS = {
    "es": {
        # navegación en el reproductor (WiiM, BubbleUPnP…)
        "vista.artistas": "Artistas",
        "vista.interpretes": "Artistas (por pista)",
        "carpetas": "Carpetas",
        "todos.artistas": "Todos los artistas",
        "todos.interpretes": "Todos los artistas",
        "todos.albumes": "Todos los álbumes",
        "vista.albumes": "Álbumes",
        "vista.generos": "Géneros",
        "vista.decadas": "Décadas",
        "vista.recientes": "Agregados recientemente",
        "vista.hires": "Hi-Res (24 bits / más de 48 kHz)",
        "vista.aleatorio": "Aleatorio (100 pistas)",
        "vista.modificados": "Modificados recientemente",
        "vista.tocadas": "Tocadas recientemente",
        "vista.mas_tocadas": "Más tocadas",
        "vista.compositores": "Compositores",
        "vista.directores": "Directores",
        "vista.sellos": "Sellos discográficos",
        "vista.calidad": "Calidad (DSD, Hi-Res, CD…)",
        "cal.dsd": "DSD",
        "cal.hires": "Hi-Res (más de 16 bits / 48 kHz)",
        "cal.cd": "Calidad CD (16 bits)",
        "cal.lossy": "Comprimido (MP3, AAC, Ogg…)",
        "sin_tag": "[Sin {tag}]",
        "todos.x": "Todos ({n})",
        "tag.albumartist": "artista del álbum", "tag.artist": "artista", "tag.genre": "género", "tag.year": "año",
        "tag.#decada": "década", "tag.#calidad": "calidad", "tag.#resolucion": "resolución", "tag.#formato": "formato",
        "tag.#agregado": "mes agregado", "tag.composer": "compositor", "tag.conductor": "director",
        "tag.label": "sello", "tag.mood": "ánimo", "tag.grouping": "agrupación", "tag.performer": "intérprete",
        "todas": "▶ Todas las pistas ({n})",
        "todas.limite": "▶ Todas las pistas (primeras {lim} de {n})",
        "indexando": "Indexando biblioteca… vuelve en unos segundos",
        "varios": "Varios artistas",
        "desconocido": "Artista desconocido",
        "nombre_defecto": "SupaVic ({host})",
        "descripcion": "Servidor DLNA de música",
        # nombres legibles para el dashboard
        "t.carpeta": "Carpeta",
        "t.album": "Álbum",
        "t.artista": "Artista",
        "t.genero": "Género",
        "t.decada": "Década",
        "t.todas": "▶ Todas",
        "t.busqueda": "Búsqueda",
        # validación de ajustes
        "e.nombre": "El nombre no puede ir vacío.",
        "e.puerto": "Puerto inválido (1024–65535).",
        "e.ip": "IP inválida; déjala vacía para autodetectar.",
        "e.no_existe": "No existe (se mostrará cuando esté disponible): {ruta}",
        "e.orden": "'Ordenar por número de pista hasta' debe ser un número.",
        "e.portada": "Tamaño de portada: 0 (original) o 100–2000 px.",
        "e.precarga": "Precarga: minutos de inactividad (≥ 0.1) y horas para repetir deben ser números.",
        "e.agrupar": "'Agrupar por letra' debe ser un número.",
        "e.cache": "No se pudo crear la carpeta de caché: {err}",
        "e.json": "JSON inválido",
        "e.red_local": "Solo desde la red local",
        "e.no_permitido": "No permitido",
        "e.no_encontrado": "No encontrado",
        # bandeja del sistema
        "b.panel": "Abrir panel de control",
        "b.dashboard": "Abrir dashboard",
        "b.reiniciar": "Reiniciar servidor",
        "b.autoinicio": "Iniciar con Windows",
        "b.datos": "Abrir carpeta de datos",
        "b.salir": "Salir",
        "b.firewall_titulo": "SupaVic DLNA — Firewall",
        "b.firewall": ("Para que tus reproductores (WiiM, BubbleUPnP, etc.) encuentren este servidor, "
                       "Windows debe permitirle recibir conexiones de tu red local.\n\n"
                       "¿Agregar la regla al Firewall de Windows ahora? (pide permiso de administrador una vez)"),
        "b.error_titulo": "SupaVic DLNA",
        "b.error_inicio": "El servidor no pudo iniciar:\n\n{err}\n\nRevisa el log en:\n{log}",
        "b.puerto_ocupado": ("El puerto {puerto} ya lo está usando otro programa (¿otro servidor DLNA, o una "
                             "versión anterior de este que sigue abierta?).\n\nCiérralo y vuelve a abrir SupaVic DLNA, "
                             "o cambia el puerto en config.json."),
        # dashboard (actividad)
        "d.elementos": "{n} elementos · {ms} ms",
        "d.corte": "{estado} tras {mb} MB",
        "d.descubrimiento": "Un dispositivo encontró el servidor",
        "d.inicio": "Servidor iniciado (v{v})",
        "d.puerto": "puerto {p}",
        "d.pre.inicio": "Precarga iniciada (servidor inactivo)",
        "d.pre.pausa": "Precarga pausada por actividad",
        "d.pre.fin": "Precarga completa",
        "d.pre": "Precarga",
        "d.pre.det": "{p} pistas · {c} portadas",
        "d.pre.det_fin": " · {n} carpetas en {m} min",
        "d.config": "Configuración modificada",
        "d.reinicio": "reinicio",
        "d.otra": "Otra fuente",
        "d.completo": "completo",
        "d.cortado": "cortado",
        "d.error": "error",
    },
    "en": {
        "vista.artistas": "Artists",
        "vista.interpretes": "Track artists",
        "carpetas": "Folders",
        "todos.artistas": "All artists",
        "todos.interpretes": "All artists",
        "todos.albumes": "All albums",
        "vista.albumes": "Albums",
        "vista.generos": "Genres",
        "vista.decadas": "Decades",
        "vista.recientes": "Recently added",
        "vista.hires": "Hi-Res (24-bit / over 48 kHz)",
        "vista.aleatorio": "Random (100 tracks)",
        "vista.modificados": "Recently modified",
        "vista.tocadas": "Recently played",
        "vista.mas_tocadas": "Most played",
        "vista.compositores": "Composers",
        "vista.directores": "Conductors",
        "vista.sellos": "Record labels",
        "vista.calidad": "Quality (DSD, Hi-Res, CD…)",
        "cal.dsd": "DSD",
        "cal.hires": "Hi-Res (over 16-bit / 48 kHz)",
        "cal.cd": "CD quality (16-bit)",
        "cal.lossy": "Compressed (MP3, AAC, Ogg…)",
        "sin_tag": "[No {tag}]",
        "todos.x": "All ({n})",
        "tag.albumartist": "album artist", "tag.artist": "artist", "tag.genre": "genre", "tag.year": "year",
        "tag.#decada": "decade", "tag.#calidad": "quality", "tag.#resolucion": "resolution", "tag.#formato": "format",
        "tag.#agregado": "month added", "tag.composer": "composer", "tag.conductor": "conductor",
        "tag.label": "label", "tag.mood": "mood", "tag.grouping": "grouping", "tag.performer": "performer",
        "todas": "▶ All tracks ({n})",
        "todas.limite": "▶ All tracks (first {lim} of {n})",
        "indexando": "Indexing library… check back in a few seconds",
        "varios": "Various Artists",
        "desconocido": "Unknown Artist",
        "nombre_defecto": "SupaVic ({host})",
        "descripcion": "Music DLNA server",
        "t.carpeta": "Folder",
        "t.album": "Album",
        "t.artista": "Artist",
        "t.genero": "Genre",
        "t.decada": "Decade",
        "t.todas": "▶ All",
        "t.busqueda": "Search",
        "e.nombre": "The name can't be empty.",
        "e.puerto": "Invalid port (1024–65535).",
        "e.ip": "Invalid IP; leave it empty to auto-detect.",
        "e.no_existe": "Doesn't exist (it will show up when available): {ruta}",
        "e.orden": "'Sort by track number up to' must be a number.",
        "e.portada": "Cover size: 0 (original) or 100–2000 px.",
        "e.precarga": "Prefetch: idle minutes (≥ 0.1) and repeat hours must be numbers.",
        "e.agrupar": "'Group by letter' must be a number.",
        "e.cache": "Couldn't create the cache folder: {err}",
        "e.json": "Invalid JSON",
        "e.red_local": "Only from the local network",
        "e.no_permitido": "Not allowed",
        "e.no_encontrado": "Not found",
        "b.panel": "Open control panel",
        "b.dashboard": "Open dashboard",
        "b.reiniciar": "Restart server",
        "b.autoinicio": "Start with Windows",
        "b.datos": "Open data folder",
        "b.salir": "Quit",
        "b.firewall_titulo": "SupaVic DLNA — Firewall",
        "b.firewall": ("So your players (WiiM, BubbleUPnP, etc.) can find this server, Windows must allow it "
                       "to receive connections from your local network.\n\n"
                       "Add the rule to Windows Firewall now? (asks for administrator permission once)"),
        "b.error_titulo": "SupaVic DLNA",
        "b.error_inicio": "The server couldn't start:\n\n{err}\n\nCheck the log at:\n{log}",
        "b.puerto_ocupado": ("Port {puerto} is already in use by another program (another DLNA server, or an "
                             "older copy of this one that's still running?).\n\nClose it and open SupaVic DLNA again, "
                             "or change the port in config.json."),
        "d.elementos": "{n} items · {ms} ms",
        "d.corte": "{estado} after {mb} MB",
        "d.descubrimiento": "A device found the server",
        "d.inicio": "Server started (v{v})",
        "d.puerto": "port {p}",
        "d.pre.inicio": "Prefetch started (server idle)",
        "d.pre.pausa": "Prefetch paused by activity",
        "d.pre.fin": "Prefetch complete",
        "d.pre": "Prefetch",
        "d.pre.det": "{p} tracks · {c} covers",
        "d.pre.det_fin": " · {n} folders in {m} min",
        "d.config": "Settings changed",
        "d.reinicio": "restart",
        "d.otra": "Other source",
        "d.completo": "complete",
        "d.cortado": "cut off",
        "d.error": "error",
    },
}


def detectar():
    """Idioma del sistema: 'es' si es español, si no 'en'."""
    try:
        if os.name == "nt":
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "es" if (langid & 0x3FF) == 0x0A else "en"
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var, "")
        if v:
            return "es" if v.lower().startswith("es") else "en"
    try:
        v = locale.getlocale()[0] or ""
    except Exception:
        v = ""
    return "es" if v.lower().startswith(("es", "spanish")) else "en"


def resolver(valor):
    valor = str(valor or "auto").lower()
    return valor if valor in IDIOMAS else detectar()


def fijar(valor):
    global LANG
    LANG = resolver(valor)
    return LANG


def T(clave, **kw):
    s = TEXTOS.get(LANG, TEXTOS["en"]).get(clave) or TEXTOS["en"].get(clave, clave)
    return s.format(**kw) if kw else s
