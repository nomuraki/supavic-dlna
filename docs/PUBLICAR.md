# Cómo publicar SupaVic DLNA en GitHub

Guía para el dueño del repositorio. Los usuarios no la necesitan.

## 1. Crear el repositorio y subir el código

1. En GitHub: **New repository** → nombre `supavic-dlna`, **Public**, sin README ni licencia (ya vienen aquí).
2. En la carpeta del proyecto:

```bat
git init -b main
git add .
git commit -m "SupaVic DLNA 1.0.0"
git remote add origin https://github.com/<tu-usuario>/supavic-dlna.git
git push -u origin main
```

`.gitignore` deja fuera `.venv\`, `datos\`, `config.json`, `dist\` y los temporales de compilación. Así tu
configuración, tus rutas de música y tu caché nunca se suben.

## 2. Completar la página del repositorio

**About** (engrane ⚙ a la derecha de la página principal):

- *Description*: `Tiny, fast music-only DLNA/UPnP server for WiiM and any UPnP player. Live folders + artist/album/genre views, no indexing wait, no transcoding.`
- *Website*: déjalo vacío o pon el enlace a Releases.
- *Topics*: `dlna` `upnp` `media-server` `music-server` `wiim` `flac` `hi-res-audio` `bubbleupnp` `windows` `python`
- Marca **Releases** y **Packages** (ahí aparece la imagen de Docker); desmarca *Deployments*.

**Social preview** (Settings → General → Social preview → Edit): sube `docs/img/social-preview.png` (1280×640).
Es la imagen que aparece al compartir el enlace en WhatsApp, X, Reddit, etc.

**Opcional:**

- Settings → General → Features → activa **Issues** (las plantillas ya están en `.github/ISSUE_TEMPLATE`) y
  **Discussions** si quieres un foro para preguntas.
- Si el paso *GitHub Release* falla con error 403: Settings → Actions → General → *Workflow permissions* → **Read and write**.

## 3. Publicar la primera versión (instalador + portable)

```bat
git tag v1.0.0
git push origin v1.0.0
```

En la pestaña **Actions** se ejecuta *Build Windows release* (unos 5 minutos). Al terminar, en **Releases** aparece
"SupaVic DLNA 1.0.0" con:

- `SupaVicDLNA-Setup-1.0.0.exe`: el instalador
- `SupaVicDLNA-1.0.0-portable-win64.zip`: la versión portable
- `SHA256SUMS.txt`

El enlace **Download for Windows** del README apunta a `releases/latest`, así que siempre lleva a la última versión.

> Antes del primer tag conviene probar el `.exe` en tu PC: ejecuta `build\build.bat` (o en Actions → *Build Windows
> release* → **Run workflow**, que genera los archivos como *artifacts* sin crear un Release).

## 4. Siguientes versiones

1. Cambia `VERSION = "x.y.z"` en `main.py` y en `dashboard.py`.
2. Agrega la sección nueva en `CHANGELOG.md` y, si quieres, ajusta `docs/release-notes.md`.
3. `git commit`, luego `git tag vx.y.z` y `git push origin main vx.y.z`.

Si la etiqueta no coincide con `VERSION`, el workflow se detiene con un error, a propósito. El texto del Release se
arma solo: la sección de esa versión del `CHANGELOG.md` + `docs/release-notes.md`.

## 5. Imagen de Docker

Con el mismo tag corre otro workflow, *Docker image* (`.github/workflows/docker.yml`), independiente del de Windows:
si uno falla, el otro sale igual. Construye la imagen para PC/NAS (`amd64`) y Raspberry Pi de 64 bits (`arm64`), la
arranca para comprobar que responde y la publica como `ghcr.io/nomuraki/supavic-dlna` con las etiquetas `x.y.z`,
`x.y` y `latest`. Tarda unos 5–10 minutos (la parte de ARM se compila emulada).

**Solo la primera vez:** GitHub crea la imagen como privada. En tu perfil → **Packages** → `supavic-dlna` →
**Package settings** → *Danger Zone* → **Change visibility** → **Public**. Sin esto, nadie más puede descargarla.

Para probarla sin publicar: Actions → *Docker image* → **Run workflow** (solo construye y prueba).

## 6. Firma de código (opcional, más adelante)

Sin firma, Windows SmartScreen muestra "Windows protegió su PC" hasta que el ejecutable gana reputación. Para
quitarlo hace falta un certificado de firma de código. Para proyectos open source existe
[SignPath Foundation](https://signpath.org/), que es gratuito. Mientras tanto, el README explica a los usuarios
cómo continuar y cómo verificar el SHA256.
