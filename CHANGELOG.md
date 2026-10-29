# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [1.3.0] - unreleased

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
