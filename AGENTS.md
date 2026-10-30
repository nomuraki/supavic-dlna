# SupaVic DLNA — notas para desarrolladores y agentes de IA

Servidor DLNA/UPnP (MediaServer) solo para música. Código e identificadores en español; textos visibles en es/en
(`i18n.py`); logs en inglés. README.md / README.es.md son la documentación de usuario.

## Archivos

| Archivo | Rol |
|---|---|
| `main.py` | Servidor completo: config, tags, caché, biblioteca en vivo, índice por etiquetas, SOAP/DIDL, SSDP, GENA, streaming, panel web (`PANEL_HTML`), `arrancar()` / `detener()` |
| `dashboard.py` | Proceso independiente (solo stdlib + `i18n`): lee `datos/eventos.jsonl` y sondea a los renderers por AVTransport |
| `launcher.py` | Entrada del `.exe` y de los `.bat`: instancia única, bandeja (pystray), autoinicio (HKCU\…\Run), firewall del portable, `--dashboard` |
| `i18n.py` | `T(clave)`; `LANG` se fija desde `servidor.idioma` al cargar la config |
| `start.bat` / `stop.bat` / `uninstall.bat` / `start.sh` | Ejecutar desde el código (ASCII + CRLF los .bat) |
| `build/` | `SupaVicDLNA.spec` (PyInstaller, carpeta), `installer.iss` (Inno Setup 6), `preparar.py` (versión → version_info.txt), `build.bat` |
| `.github/workflows/release.yml` | Tag `vX.Y.Z` (= `VERSION` de main.py) → instalador + zip portable + SHA256SUMS en un Release |

## Rutas de datos (`_dir_app()` en main.py y dashboard.py, deben coincidir)

`SUPAVIC_HOME` si existe → `%LOCALAPPDATA%\SupaVic DLNA` si es ejecutable (frozen) → junto al código.
Ahí van `config.json` y `datos/` (log, pid, eventos, historial y, si `cache.directorio` está vacío, `cache.db` + `portadas/`).

## Arranque y reinicio

- `launcher.py`: si `ping()` responde → abre el panel y sale (instancia única). Si no: `srv.RELANZAR = comando("--reinicio", …)`,
  `srv.arrancar()`, dashboard hijo (si `dashboard.con_servidor` y su puerto está libre), navegador (no con `--inicio`
  salvo que no haya carpetas), bandeja en el hilo principal.
- Cambiar `servidor.*` o `cache.*` en el panel reinicia: `App.reiniciar()` → `AL_SALIR()` (quita el icono) → `Popen(RELANZAR)`
  → `os._exit(0)`. `--reinicio` evita la comprobación de instancia única (el proceso viejo aún responde).
- El dashboard sobrevive al reinicio del servidor; "Salir" lo mata por `datos/dashboard.pid`.
- `main.py` sigue funcionando solo (`python main.py`): sin bandeja ni dashboard.

## Servidor (resumen)

- IDs sin estado: `d<raiz>:<rel>` carpeta, `t<raiz>:<rel>` pista, `<raiz>` = `clave_raiz()`: sha1[:8] del NOMBRE de la
  carpeta configurada (no la ruta: cambiar de unidad no rompe favoritos; nombres repetidos llevan sufijo `#n`).
  La 1.1 usaba la ruta completa (`clave_raiz_vieja`, solo para migrar `reproducciones.db`).
  Vistas: `v:<vista>[@letra]`, `v:ar:`, `v:ia:<id>[:al:<aid>]` (artistas de pista), `v:al:`, `v:ge:[…:ar:]`, `v:de:`,
  `…:todas`, `v:carpeta:<raiz>:<rel>:todas`, `v:carpetas` (contenedor opcional de las carpetas), `v:tocadas`, `v:mas_tocadas`.
- Motor de vistas por niveles (`Indice._x_*`): predefinidas en `PRESETS_X` (`compositores`, `directores`, `sellos`,
  `calidad`) y las del usuario en `vistas.personalizadas` (id `c…`). IDs: `v:<vid>[@letra]`, `v:<vid>:<h0>[:<h1>…][@letra]`,
  `…:al:<aid>`; `<h>` = `_hid(valor)` (10 hex, estable) o `_` = sin ese tag. `_x_preparar` precalcula por construcción
  del índice `vals[nivel][pista]` (tuplas compartidas, memo por dato crudo) e `inv0`; los nodos se cachean en `_x_cache`.
  Niveles calculados: `NIVELES_CALC` (`albumartist`, `#decada`, `#calidad`…); el resto son tags extra (`m["x"]`), y solo
  se cargan en memoria los que usan las vistas activas o las plantillas (`claves_x_necesarias`).
- Títulos: `plantilla()` (compilada y cacheada) con `vistas.plantillas` (`raiz`, `contenedor`, `album`); `Indice.nc()` /
  `Indice.na()`. Campos en español o inglés (`_ALIAS_CAMPO`).
- Álbum = (album, albumartist) o (album, carpeta) sin albumartist; si el mismo par aparece en varias carpetas
  (sin contar subcarpetas `CD1`/`Disc 2`) se separa por carpeta. IDs iguales que antes si no hay homónimos.
- `VERSION_LECTOR` (PRAGMA user_version de cache.db, hoy 3): súbelo si cambia lo que se extrae de los tags; la caché de
  tags se vacía una vez al arrancar (las portadas se conservan). v3 guarda `x` (tags extra normalizados, `tags_extra`),
  `add` (ENCODINGTIME) y `pt` (tipos de imagen embebida si no es solo la frontal); v4 `arts`/`aarts` (ARTISTS /
  ALBUMARTISTS: índices vía `nombres_artistas`; se muestran con `mostrar_artistas` → "A, B & C") y `fecha`/`ry`
  (fecha de la edición → `dc:date` y `{años}`; `year` sigue siendo el año original).
- Portadas: fuente → `clave_portada` (archivo + fecha + tamaño) → tabla `portadas` de cache.db → hash del CONTENIDO
  (+ tamaño/recorte) = nombre del JPEG. Prioridades en `Biblioteca.fuente_portada_pista` / `fuente_portada_carpeta`,
  `Indice.fuente_artista` (`/art/r/<artista>/<album>.jpg`) y `PRIO_IMG_ALBUM` / `PRIO_IMG_ARTISTA`. La URL de cada pista
  se decide sin leer imágenes (`url_portada_pista`).
- "Agregados recientemente": `add` › fecha de creación (Windows, `st_birthtime`/`st_ctime` del scandir del índice) › mtime.
- `Reproducciones` (`datos/reproducciones.db`, fuera de la caché): +1 cuando un reproductor pide el archivo desde el byte 0
  (la misma pista en < 60 s no cuenta doble).
- UDN = uuid5(`servidorMusicaDLNA:<host>:<DIR_BASE>`) — prefijo histórico, no cambiarlo (los reproductores verían otro servidor).
- La carpeta navegada siempre se lee del disco (`listar(fresco=True)`): en Windows la fecha de una carpeta no cambia
  de forma fiable al mover subcarpetas. Conteos/portadas usan caché.
- `CacheTags`: SQLite validado por `mtime_ns` + tamaño; si la ruta cambió, busca por identidad (nombre + tamaño + mtime).
- `Indice`: se construye desde `cache.db` (sin leer audio), en segundo plano, máx. 1/min y solo tras `QUIETO_SEG`
  (15 s) sin navegar (salvo el primero o `invalidar()`); `mantener()` lo llama la precarga. Los bucles largos llaman
  `_ceder()` (sleep corto si alguien navega). Verifica existencia con un `scandir` por carpeta.
- "▶ Todas las pistas" de carpetas: `Biblioteca.pistas_vivas` / `contar_vivo` (disco, con presupuesto de tiempo).
- Paginación: `Biblioteca.hijos()` cachea la lista completa por oid (carpetas 4 s; vistas hasta que cambien índice,
  `cfg_gen` o `update_id`; `VOLATILES` 4 s). `_hijos_calc` es el cálculo real.
- Reconstrucción incremental: `Indice._parseados` (tags ya procesados por ruta + mtime/tamaño + firma de claves extra) y
  `Indice._dirs` (listado por carpeta + mtime de la carpeta). `_norm`, `_orden`, `_hid`, `_letra`, `natkey` con `lru_cache`.
- Docker: `Dockerfile` + `docker-compose.yml` (red del host, `SUPAVIC_HOME=/data`, variables `SUPAVIC_*` solo en el
  primer arranque, ver `cargar_config`). Nombres visibles traducidos (`VISTAS` se rellena en `aplicar_idioma`).
- `Precargador`: en reposo (`precarga.inactividad_min` sin Browse/portadas ni transferencias) recorre todo; cualquier
  actividad la pausa.
- `esc()` / DIDL: `upnp:albumArtURI` con `dlna:profileID="JPEG_SM"` si la portada ≤ 640 px.
- Los POST de `/api/*` solo desde IPs privadas.

## Pruebas

Sin suite automática. Verificación usada:
- Linux: `SUPAVIC_HOME=/tmp/x python launcher.py --sin-bandeja` con una biblioteca de prueba; SOAP Browse a `/ctl/cds`
  (raíz, vistas, "Todas"), `/api/estado`, reinicio por `/api/reiniciar`, SIGTERM.
- Build congelado: `pyinstaller build/SupaVicDLNA.spec` en Linux valida el spec y las rutas frozen (`LOCALAPPDATA=…`).
- UI: Playwright sobre `:8200` y `:8202` en es/en, claro/oscuro, sin errores JS (pestaña Navegación y editor de vistas).
- Rendimiento: biblioteca sintética de 48k pistas (archivos vacíos + filas en cache.db) → índice ~2 s, raíz ~60 ms,
  vistas personalizadas < 10 ms con caché. Comparar contra la versión anterior antes de publicar.
- Windows real (bandeja, registro, MessageBox, instalador) se prueba a mano.

## Convenciones

- Python ≥ 3.9, sin dependencias nuevas sin necesidad (stdlib primero).
- .bat en ASCII + CRLF con pasos numerados; sin VBS.
- Un cambio visible para el usuario → textos en `i18n.py` (servidor) y en los diccionarios `TXT` del panel/dashboard.
- Subir versión: `VERSION` en main.py + CHANGELOG.md + docs/release-notes.md, luego tag `vX.Y.Z`.
