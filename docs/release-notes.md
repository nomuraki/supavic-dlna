## Downloads

| File | What it is |
|---|---|
| `SupaVicDLNA-Setup-{VERSION}.exe` | **Installer for Windows 10/11 (64-bit), recommended.** Adds the firewall rule, Start menu entry and (optional) start with Windows |
| `SupaVicDLNA-{VERSION}-portable-win64.zip` | Portable version: unzip anywhere and run `SupaVicDLNA.exe` |
| `SHA256SUMS.txt` | Checksums for the files above |

**Linux / NAS / Raspberry Pi (64-bit):** Docker image [`ghcr.io/nomuraki/supavic-dlna:{VERSION}`](https://github.com/nomuraki/supavic-dlna/pkgs/container/supavic-dlna)
(also `:latest`). It isn't a file to download: get [`docker-compose.yml`](https://github.com/nomuraki/supavic-dlna/blob/main/docker-compose.yml),
set your music path and run `docker compose up -d`, and Docker pulls the image for you.

After installing, the control panel opens in your browser: add your music folder under **Folders**, and the server
appears on your player (WiiM Home, BubbleUPnP…) as **SupaVic (*your PC's name*)**.

> The executable isn't code-signed yet. If Windows SmartScreen warns you, choose **More info → Run anyway**.

Full history: [CHANGELOG.md](https://github.com/nomuraki/supavic-dlna/blob/main/CHANGELOG.md)
