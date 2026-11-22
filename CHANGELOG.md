# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-11-22

A major overhaul: faster, lighter, safer and more complete, without making it any harder to use. Several ideas
come from **simbun**'s feedback and sample files, thanks! Nothing has to be set up again: your settings, play
history and covers carry over.

### Highlights
- 🧠 **~30% less memory** and a faster tag index: 100,000 tracks went from 405 MB to 290 MB, and the index builds
  in 3.4 s instead of 4.1 s. Browsing is as instant as before.
- 🆕 **New music shows up in minutes**, even while you listen: a light check compares each folder with what it
  had last time and only reads new or changed files (~0.1 s for 12,000 tracks).
- 📜 **Playlists**, 📀 **disc folders for box sets**, ⭐ **Favourites**, 🩺 **library health check**,
  🔎 **full UPnP search**, 🔒 **optional panel password**.

### Added
- **Playlists.** `.m3u`, `.m3u8` and `.pls` files in your music folders show up in their folder and in a new
  **Playlists** view (only shown when there is one). Each one has its own artwork: an image with the same name
  (`Favourites.jpg` next to `Favourites.m3u8`), or its first track's.
  - Relative and absolute paths, `file://` URLs, UTF-8 or Windows (ANSI) encoding. Playlists made on another
    computer work too: a Windows playlist with `D:\Music\…` paths used from Docker or Linux, or after changing
    the drive letter, is matched to your folders automatically.
  - **» By album** at the top of each playlist: a "500 Greatest Albums" playlist can be browsed as 500 albums, in
    the playlist's order. Pressing Play on the playlist still plays each track once.
  - Fast with big playlists: 6,000 tracks resolve in well under 0.1 s, and the file is only read again when it
    changes.
- **Playlists for presets**: `/pl/random.m3u8`-style addresses (`/pl/aleatorio.m3u8` in Spanish) (random, favourites, recently added, recently
  played, most played, Hi-Res) to save as presets on the WiiM or any player that takes playlists by URL, with
  `#EXTINF` titles. "Random" brings 100 different tracks every time. Listed in **Browsing**, with copy buttons.
- **Disc folders for multi-disc albums** (option): a **» subtitle** folder before each disc, like MinimServer,
  using `DISCSUBTITLE` ("» Disc 2" without one) and its artwork (`Supersonic.jpg`). The album still plays whole
  and each disc can be opened or queued on its own. Works in every view, custom ones included.
- **Straight-through track numbers for multi-disc albums** (option): 1…N across the whole album instead of
  starting again on each disc, for control points that mix discs up when sorting (mconnect, for example).
- **Favourites** view: tracks with 4 or 5 stars from `RATING` or `FMPS_RATING` tags (Picard, foobar2000,
  MediaMonkey…), any scale (1–5, 0–100, 0–1…). Only shown when there is one. MP3 ratings stored only in the
  binary `POPM` frame are not read.
- **Library health check** (new **Health check** tab): albums split by mismatched album artists, repeated albums,
  missing track numbers, albums without artwork, tracks without tags and files that can't be read (damaged or
  incomplete downloads). Uses what the index already knows, no extra reading.
- **Full UPnP search**: `and` / `or` / brackets and every operator (`=`, `!=`, `contains`, `doesNotContain`,
  `derivedfrom`, `exists`, `<`, `>`…), returning tracks, albums, artists or genres as asked, inside the folder or
  view being searched (`ContainerID`), sorted as requested (`SortCriteria`) and with no 500-result cap.
- **Decades from any date tag**: add `.decade` (or `.year`) to any date, in title formats
  (`{originaldate.decade}` → "1960s") and as a custom view level or filter (date tags like
  `originalrecordingdate` are offered "by decade" in the view editor).
- New title format fields: `{originaldate}` (full original date) and `{date}` (this release's date).
- **Optional panel password** (**Settings → Maintenance**). Never asked on the computer running SupaVic, and
  players don't need it. The dashboard uses the same login. Forgot it? Delete `datos/clave.txt`.
- **Covers one level ahead**: when you open an artist (or a genre, a folder, a playlist…), the covers and tracks
  you're about to see are prepared in the background, so opening an album is instant. Never on the first screen,
  it gives way to anything you're browsing, and it stops as soon as you go somewhere else.
- `.webp` images as folder covers.

### Changed
- **Albums from the same year are sorted by full original date**, not by title: Sgt. Pepper's (1967-06-01) now
  comes before Magical Mystery Tour (1967-11-27).
- **Updating no longer empties the tag cache.** When a new version reads more from your tags (this one does, for
  the full original date), tracks are read again in the background and everything keeps working with the old
  data meanwhile. Before, a big library had empty tag views until it was read again from scratch.
- **Play counts are more faithful**: a play counts once half the file has been sent (adding up seeks), so skipped
  tracks and the next track the player fetches early for gapless don't count. Replaying a song counts again, and
  moving or renaming a file keeps its history.
- **Music starts sooner**: the first bytes go out before anything else, and files are sent with the operating
  system's zero-copy `sendfile` (less CPU on a NAS or a Raspberry Pi).
- The panel only accepts changes coming from itself (protection against other web pages sending requests to it).
- `config.json` is checked and tidied when the server starts, the same way as when saving from the panel (hand
  edits, old formats). If something doesn't pass, it's used as is and the log says why.
- New views from an update show up once in your view list; if you remove them, they stay removed.

### Fixed
- Folders, genres and decades told players they had one item less than they list (the "▶ All tracks" entry
  wasn't counted). Players that trust that number could leave the last item out.
- A counter of active streams wasn't safe with several connections at once and could get stuck, stopping the
  background reading for good.
- Lists cached for browsing could, in rare cases, be served from the previous index right after a rebuild.
- A player closing a connection no longer fills the log with error traces.

## [1.6.0] - 2026-11-05

Thanks again to **simbun** for another round of detailed feedback: everything in this release comes from it.

### Added
- **Track title format** in **Browsing → How titles look**, like the ones for albums and containers:
  `{title} ({subtitle})`, `{title} ({version})`, `{track}. {title}`… Fields: `{title}` `{track}` `{disc}`
  `{artist}` `{album}` `{year}` and any tag in your files. As everywhere, text around an empty field disappears,
  so `{title} ({subtitle})` shows no brackets on tracks without a subtitle.
- **Tracks can show their album with your album format** (option under the title formats), for control points
  that show the album on each track, like BubbleUPnP and upplay: "Peter Gabriel [1977]" instead of "Peter Gabriel".
  Off by default, since some players group tracks by that text.
- **Artist name as printed on the release.** If your files carry the credited name, as MusicBrainz Picard writes it
  (`DISPLAY ARTIST`, `DISPLAY ALBUMARTIST`, also `ARTIST DISPLAY` / `ARTIST LABEL`), that's what tracks and albums
  show, while the Artists lists keep using each artist separately (`ARTIST`/`ARTISTS`). Which tags to look for can
  be changed in **Browsing**; leave them empty to always show `ARTIST`.
- **Optional parts in title formats with `<…>`**: whatever is inside only shows when at least one field in it
  has a value, so separators and words around a missing tag go away too:
  `{album}< • {version}>< ({remasterdate} Remaster)>` → "Queen • Deluxe Edition (2011 Remaster)", or just "Queen".
- **Option to never add the folder name** to albums that would show the same title (on by default).
- **Export / import settings** in **Settings → Maintenance**: save everything (folders, views, title formats…) to
  a file and load it in another install. Importing keeps that install's port and cache folder, so you can test a
  new version side by side.

### Changed
- **The folder name is only added to an album's title when it's really needed.** Two albums with the same title
  and artist used to always get " · <folder>". Now it's only added when your title format would still show them
  the same: four self-titled "Peter Gabriel" albums show as "Peter Gabriel [1977]", "[1978]"… and two "21 [2011]"
  editions still show their folder. Any field in your format counts, including your own tags like `{nickname}`.
- Up to 40 extra tags per track are kept (was 24), so heavily tagged files (e.g. from Picard with full credits)
  don't lose tags like `DISPLAY ARTIST`.

### Fixed
- The `{album} [{years}] • {version}` preset no longer leaves a loose "•" when there is no version. It is now
  `{album} [{years}]< • {version}>` and saved settings using the old one are updated automatically.
- Two different files with the same name, size and modification time (batch copies, or USB drives in FAT/exFAT,
  which only keep the time to 2 seconds) could end up showing each other's tags, as if one had been moved. Now a
  file only counts as moved when its old location no longer exists.

## [1.5.0] - 2026-11-03

### Added
- **Index tags, per folder.** Each folder in **Folders** has an **Index tags** box (on by default, so nothing
  changes when you update). Untick it for downloads, playlists or any folder that clutters Artists and Albums with
  loose or duplicate entries: you can still browse it by folder, with tags and covers, but its tracks stay out of
  Artists, Albums, Genres, Decades, Recently added, your own views and search. What you play from it still counts in
  Recently played and Most played. Saved as `musica.sin_indexar` in `config.json`.

### Changed
- Folders with **Index tags** unticked aren't read in the background either (only when you open them), which saves
  disk reading on large download folders. Their cached tags are kept, so ticking the box again brings them back
  within seconds and only what's missing is read.
- Changing your folders in the panel now refreshes the tag views right away.
- **Track covers appear much faster when you open an album.** Each track used to need its own embedded picture
  read from disk (often 1–2 MB per FLAC), even when all the tracks carry the same one, so on players that load
  images while scrolling they showed up one by one. Now, once one track's picture is read, the other tracks
  that carry the same picture (checked from their headers, a few KB each) get it right away: 10 covers went from
  ~0.7 s to under 0.1 s on first open, and to ~0.01 s for albums the background reading has already prepared.
  Tracks with different pictures (playlists, mixed folders) still get their own.
- **No more generic icons while scrolling an album.** Once a cover is prepared, every track that carries it gets
  the same image address (based on the picture's content), so the player downloads it once and the rows that
  appear as you scroll show it straight from the player's own cache. In a playlist or mixed folder each track
  still points to its own picture. Browsing speed is unchanged.

### Fixed
- A folder browsed while its files were still being copied or downloaded could keep a generic folder icon: the
  player asked for the cover before the tracks had their pictures, and "no cover" was remembered. Folders that
  changed in the last 10 minutes are now checked again on every request, straight from disk.
- A folder whose first tracks have no embedded picture now uses the picture of any other track already read.
- **Docker: two instances on the same network showed up as one server** (same name and cached content on the
  WiiM), because the server id came from the container's hostname and `/data`, which are the same in every
  container. Each Docker install now gets its own random id, saved in `/data/datos/udn.txt`. On Windows and Linux
  without Docker the id doesn't change. Docker users will see the server once as a new device after updating,
  so favorites saved on the player need to be added again. Reported by **apwiggins** (#3).
- **Docker: `SUPAVIC_NOMBRE`, `SUPAVIC_MUSICA`, `SUPAVIC_IDIOMA` and `SUPAVIC_PUERTO` were only read on the
  very first start**, so changing them in `docker-compose.yml` later did nothing. They're now applied whenever
  their value changes. Changes made in the control panel are kept while the compose value stays the same.
  (When updating from 1.4, folders you added in the panel are kept: `SUPAVIC_MUSICA` is only applied once you
  change it.)

## [1.4.1] - 2026-10-30

### Added
- **Ready-made Docker image** on GitHub's registry, `ghcr.io/nomuraki/supavic-dlna`, for PC/NAS (`amd64`) and 64-bit
  Raspberry Pi / ARM boards (`arm64`). It's built and published automatically with every release, tagged `latest`,
  with the version (`1.4.1`) and with the series (`1.4`). You no longer need to download the code or build anything:
  get `docker-compose.yml`, set your music path and run `docker compose up -d`. Update with
  `docker compose pull && docker compose up -d`. Suggested by **apwiggins** (#1).
- Before publishing, the image is started and checked to answer, so a broken build never reaches `latest`.

## [1.4.0] - 2026-10-29

### Added
- **Docker support**: `Dockerfile` and `docker-compose.yml` for Linux hosts (NAS, home server, Raspberry Pi). Host
  networking for DLNA discovery, settings and cache on a `/data` volume, music mounted read-only, UTF-8 locale,
  health check, and first-start settings from environment variables (`SUPAVIC_NOMBRE`, `SUPAVIC_MUSICA`,
  `SUPAVIC_IDIOMA`, `SUPAVIC_PUERTO`). Based on the setup contributed by **apwiggins** (#1).

### Changed
- **Much faster paging.** Players ask for long lists in pages of 50–100 items, and every page used to rebuild the
  whole list. Lists are now kept between pages: with 2,000 folders at the root a page went from ~65 ms to ~5 ms,
  and 4,000 albums from ~30 ms to ~2 ms per page. Folders still reflect the disk within seconds, and views refresh
  as soon as the tag index or the settings change.
- **Faster tag index rebuilds** (~1.8–2.9 s → ~1.2–1.6 s for 48,000 tracks): tags parsed in the previous build are
  reused when the file didn't change, folders that didn't change aren't listed again, and repeated text work
  (sorting keys, normalized names, ids) is cached. Fewer disk reads helps a lot on hard drives.
- Looking up cached tags for a folder takes one database query per 800 files instead of one per file.
- Opening a view from the root is instant: its list is already prepared when the root is shown.
- **Much less disk reading while idle.** The background reading used to extract the embedded picture of every
  single track (tens of GB on a large FLAC library). Now it prepares one cover per album folder, and a track's own
  cover is extracted only when a player asks for it; since it's almost always the same picture, the existing file is
  reused. Loose-track folders still get one per track.
- Cover cache cleanup is exact: it drops covers whose source file no longer exists, and keeps the ones created on
  demand (before, those could be deleted and regenerated on every pass).
- **Restarting from the panel on Linux/macOS/Docker** (after changing the name, port, language…) replaces the process
  in place. In Docker, the container used to stop and depend on the restart policy to come back.

## [1.3.1] - 2026-10-29

### Fixed
- **Linux / macOS / Docker:** folders and files whose names contain `:` or `\` (e.g. "Hi-Res Masters: '80s Rock")
  were skipped with `TypeError: stat: path should be string… not NoneType`. Those characters are only off-limits on
  Windows; they're now accepted everywhere else. Reported by **apwiggins** (#2).
- A name that can't be resolved no longer raises an error while rendering a list; it's simply left out.
- If the system decodes file names with something other than UTF-8, the server logs a warning on startup
  (fix: `LANG=C.UTF-8`), and responses replace invalid characters instead of failing.

## [1.3.0] - 2026-10-29

Includes everything in 1.2.0, which was never published on its own. Thanks again to **simbun** for the third round
of feedback: most of this release comes from it.

### Added
- **Full lists by default.** Artists, albums, composers, labels and custom views show every entry in one list. If
  you prefer the old style, **Browsing → Long lists → Grouped by letter (A, B, C…)** splits lists longer than a
  number you choose, with an "All" entry on top.
- `ARTISTS` / `ALBUMARTISTS` (one name per value, as Picard writes them) are used for the Artists, Track artists
  and Genre indexes, while titles keep the full credit from `ARTIST` (e.g. "Gorillaz feat. Elton John & 6LACK").
- `{years}` / `{años}` template field: original and release year, `1975/2005`, or just one when they match; and
  `{release_year}`. The release date (`DATE`/`TDRC`) now goes in `dc:date`, so control points can sort by release.
  Any tag, such as `{version}` for MusicBrainz release comments, works as a field
  (preset: `{album} [{years}] • {version}`).
- **Recently modified** view: albums whose files changed most recently (optional).
- "What's new" link in the control panel footer.

### Changed
- **Music folder ids now come from the folder's name**, not its full path: changing the drive letter or moving the
  library no longer changes track URLs or IDs. ⚠️ Because of this one-time change, favorites and presets saved on
  your player with 1.1 or earlier need to be added again (play counts are carried over).
- Several values of a tag are shown as "A, B & C" instead of "A; B; C".
- **Recently added**: an album's date is when its first track arrived, so adding a bonus track later doesn't move
  it, and albums added at the same time are sorted by title.
- **Browsing always comes first.** The tag index is rebuilt only after 15 s without browsing (or during idle
  prefetch), and a rebuild that's already running yields to browse requests. Before, a rebuild could make opening
  a folder take a second or two, especially while a large library was being read. When the index is far behind
  (new install, re-reading the library) it catches up within seconds of you stopping.
- Opening a folder waits at most about 1 s for tags that aren't cached yet (was 1.5 s); the rest show names from
  the path and finish in the background.
- `ARTISTS`/`ALBUMARTISTS` and the release date are cached too, so the cache is re-read once after updating.

### Fixed
- **Duplicate albums**: loose tracks of an album that live in other folders (playlists, downloads) are now part of
  that album instead of showing up as another "21" with 1 or 2 tracks. Two folders that are both full albums with
  the same title (reissues) stay separate, and the title shows the folder to tell them apart: "21 [2011] · 21
  Deluxe".
- **Wrong artist pictures**: an artist whose only tracks sit in a mixed folder (e.g. a playlist) showed the cover
  of that folder's first track, another artist's. It now shows the cover of their own album.
- "▶ All tracks" in folders is counted and listed straight from disk, so it's right even before the background
  reading has finished (it used to show only the tracks already in the tag index, e.g. "All tracks (1)" for a
  folder with 45 songs).

## [1.2.0] - not published (included in 1.3.0)

The biggest update so far: your own tag views, a new **Browsing** page in the control panel, and smarter covers.
Thanks to **simbun** on the WiiM forum for the detailed testing and the specs behind several of these changes
(artwork priority, ENCODINGTIME, static URLs), and to everyone in the forum thread for the ideas and feedback.

### Added
- **Custom views.** Build your own views in the control panel: up to three levels from any tag in your library
  (composer, conductor, label, mood, `SOURCE`, your own `TXXX`/freeform tags…) or a computed one (decade, quality,
  resolution, format, month added), an optional filter (is / contains / doesn't contain / exists / ≥ / ≤), an
  optional album title format, and a live preview with counts. Albums and tracks always come last, with
  "▶ All tracks" like the built-in views.
- **New ready-made views** (off by default, tick them in **Browsing**): Composers, Conductors, Record labels,
  Quality (DSD / Hi-Res / CD / compressed), **Recently played** and **Most played**. Play counts are kept in
  `datos/reproducciones.db` and survive clearing the cache.
- **Title templates** for root entries, containers (artists, genres, folders…) and albums, with presets:
  `{album} [{year}]`, `{year} - {album}`, `{album} [{year}] {quality}` (→ `24/96`, `DSD64`, `MP3 320k`)…
  Empty values disappear with their brackets, and any tag can be used as a field (`{label}`, `{composer}`).
  Spanish and English field names both work. Your previous count/year settings are carried over.
- **Browsing** tab in the control panel. It explains the two ways to browse (folders: instant, straight from
  disk; tag views: filled by the background reading, with progress), lists every view with a short description
  and how many tracks have its tag, lets you reorder them, and shows the cover priority.
- **Artist images**: `artist.jpg` in the artist's or album's folder, `folder.jpg` in a folder named like the
  artist, or an embedded artist photo (picture types 7/8), before falling back to the first album's cover.
- "All artists" / "All albums" entry at the top of lists grouped by letter.
- The panel tells you when a new version is out (your browser checks GitHub releases; the server itself never
  goes online). Can be turned off in **Settings**.

### Changed
- **Cover priority** (suggested by simbun). Track: embedded front cover › `<disc subtitle>.jpg` › other embedded
  picture (media/CD label…) › folder image. Album/folder: `cover`/`folder`/`front` › any other image › embedded
  cover › back cover/CD/booklet › first subfolder image (optional). Embedded pictures are chosen by type, so a
  back cover or a 32 px file icon is no longer picked by accident.
- **Each track shows its own embedded cover** by default (option **Each track shows its own embedded cover**).
- Covers are now stored **by content**: tracks that share the same embedded picture use one file on disk. Old
  cache files are cleaned up by the next prefetch pass.
- **Recently added** uses `ENCODINGTIME`/`TDEN` when present (suggested by simbun), otherwise the date the file
  arrived on your disk (creation date on Windows). Editing tags no longer moves old albums to the top.
- More tags are read and cached (composer, conductor, label, mood, grouping, disc subtitle, media, rating and any
  custom tag up to 120 characters; lyrics, comments and IDs are skipped). The cache is re-read once after updating.
- Cover size, crop and folder options moved from **Settings** to **Browsing**.

### Fixed
- Folders of loose tracks (playlists, downloads) where some files have an empty album tag were treated as a single
  album, so every track showed the cover of the first one. Each track now shows its own embedded cover, and a
  track without one in such a folder shows none instead of another track's.
- Albums in folders that hold several albums now use their own tracks' cover in tag views.

### Docs
- FAQ: how track URLs are built and why they stay the same (`/m/<folder hash>/<path>`; question from simbun),
  why a tag view can be empty, and where "Recently added" dates come from.

## [1.1.0] - 2026-10-28

Thanks to the WiiM forum community (especially simbun) for the detailed first round of feedback.

### Fixed
- The app no longer crashes if Windows refuses to create the tray icon (`PermissionError: [WinError 5]`, which
  happens when the process starts on a desktop it isn't allowed to create windows on). The server keeps running,
  and the icon shows up as soon as Windows allows it (quick retries, then once a minute).
- Multi-valued tags: every value of repeated `ARTIST=`, `ALBUMARTIST=` and `GENRE=` fields (and multi-value ID3/MP4
  tags) is read, not only the first one.
- Albums with the same title and album artist in different folders (reissues, self-titled albums) are no longer
  merged into one. `CD1`/`Disc 2`-style subfolders still form a single album.
- Album years prefer `originaldate`/`originalyear` over `date`, so reissues sort by their original release.
- One broken file or folder no longer makes a whole page fail to browse: it's skipped and logged.
- The control panel could fail to load the list of views (and then save an empty list) in browsers that report an
  unusual language tag.

### Added
- Year after the album title, `Album [1999]` (on by default, can be turned off).
- Compilations / "Various Artists" albums are sorted by `albumsort` or title instead of year; the compilation flag
  (`COMPILATION`, `TCMP`, `cpil`) is honored.
- Optional "Track artists" view: everyone who appears on a track (featuring, compilations), with only their tracks.
- Count type: songs (default), items (an artist's albums, a genre's artists…; albums get no number) or none.
- Optional "Folders" entry at the root that holds your folders instead of listing them directly at the root.

### Note
- The tag cache is rebuilt once after updating (tags are read again on browse and during idle prefetch); covers
  are kept.

## [1.0.0] - 2026-10-27

First public release.

### Added
- Music-only DLNA/UPnP MediaServer: SSDP on every local interface, ContentDirectory (Browse, Search),
  ConnectionManager and GENA change notifications.
- Live folder browsing with no up-front indexing, a SQLite tag cache, a fast FLAC header reader and idle prefetch
  of tags and covers.
- Tag views: Artists, Albums, Genres, Decades, Recently added, Hi-Res and Random, with A–Z grouping,
  "▶ All tracks" entries and song counts.
- Resized cover art (DLNA `JPEG_SM`), one cover per album, folder images or embedded pictures.
- Original-file streaming with HTTP range requests (no transcoding).
- Web control panel and a separate dashboard (now playing, listening history, transfers, stats).
- English and Spanish interface, including the folder names shown on players.
- Windows tray app, start with Windows, installer and portable builds; one-click scripts to run from source;
  `start.sh` and a systemd unit for Linux.
