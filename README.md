# FlipsiStitch

[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)](https://github.com/TechFlipsi/FlipsiStitch/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

**Verlustfreies Zusammenfügen von Videosegmenten – mit Farbkorrektur, GPU-Beschleunigung & moderner Web-UI**

DJI-Kameras (Osmo Action 5 Pro, Pocket 4, Drohnen) splitten lange Videoaufnahmen absichtlich in mehrere Dateien – angeblich wegen Dateisicherheit. FlipsiStitch erkennt diese Segmente automatisch und fügt sie verlustfrei wieder zu einer Datei zusammen.

## ✨ Features

- 🎬 **Automatische Segment-Erkennung** – Erkennt DJI, GoPro und alle anderen Kameras anhand fortlaufender Nummern
- 🔗 **Verlustfreies Mergen** – ffmpeg concat demuxer mit `-c copy` (kein Re-encoding)
- 🎨 **Farbkorrektur** – D-Log M → Rec.709, automatischer Weißabgleich, oder beides kombiniert
- ⚡ **GPU-Beschleunigung** – NVENC (NVIDIA), AMF (AMD), QSV (Intel), VideoToolbox (Mac)
- 📦 **H.265/HEVC** – Moderner Standard-Codec bei Re-Encoding, ~40% kleinere Dateien
- 🖥️ **Moderne Web-UI** – Dark Mode, Glasmorphismus, Drag & Drop, Live-Fortschritt
- 🔄 **Auto-Update** – GitHub-Releases checken, Downgrade-Schutz, automatisches Update
- 📋 **Portable** – Eine `.exe`, kein Python, keine Installation nötig

## 🚀 Schnellstart

### Portable (Empfohlen)

1. Neueste `FlipsiStitch_X.X.X_Full.zip` von [Releases](https://github.com/TechFlipsi/FlipsiStitch/releases) herunterladen
2. Entpacken
3. `flipsisitch.exe` starten
4. Browser öffnet sich automatisch → Ordner auswählen → Mergen!

### Mit Python

```bash
git clone https://github.com/TechFlipsi/FlipsiStitch.git
cd FlipsiStitch
python flipsisitch.py
```

Voraussetzung: [ffmpeg](https://ffmpeg.org/) im PATH oder im gleichen Ordner.

## 🎨 Farbkorrektur

FlipsiStitch bietet drei Korrektur-Profile:

| Profil | Beschreibung | Re-Encoding |
|--------|-------------|-------------|
| **Keine** (Standard) | Verlustfrei, keine Änderung | Nein (`-c copy`) |
| **D-Log M → Rec.709** | Konvertiert DJI D-Log M zu normalem Farbraum | Ja (H.265) |
| **Weißabgleich** | Automatischer White Balance (grayworld) | Ja (H.265) |
| **D-Log M + Weißabgleich** | Beides kombiniert (empfohlen) | Ja (H.265) |

### Sicherheit

- D-Log M Konvertierung wird **nur angewendet** wenn das Video tatsächlich D-Log M enthält (automatische Erkennung via ffprobe)
- Weißabgleich hat Sanity-Checks – wird übersprungen wenn das Ergebnis nicht verlässlich ist
- `--test-color` extrahiert Frames VOR/NACH der Korrektur zum Vergleich

## ⚡ GPU-Beschleunigung

Automatische Erkennung und Priorität:

1. **NVENC** (NVIDIA) – `-c:v hevc_nvenc`
2. **AMF** (AMD) – `-c:v hevc_amf`
3. **QSV** (Intel) – `-c:v hevc_qsv`
4. **VideoToolbox** (macOS) – `-c:v hevc_videotoolbox`
5. **CPU-Fallback** – `-c:v libx265`

CLI: `--hwaccel auto|nvenc|amf|qsv|videotoolbox|cpu`

## 🖥️ CLI

```bash
# Ordner scannen und mergen
flipsisitch /pfad/zum/ordner

# Mit Farbkorrektur
flipsisitch /pfad --color-profile both

# Nur anzeigen was gemacht würde
flipsisitch /pfad --dry-run

# Codec wählen
flipsisitch /pfad --codec hevc

# Update prüfen
flipsisitch --check-update

# Version anzeigen
flipsisitch --version
```

## 🔧 Build

```bash
# Portable .exe (ohne ffmpeg)
build.bat

# Full .exe (mit ffmpeg eingebettet)
build_full.bat

# Release-ZIP
build_release.bat
```

Voraussetzung: Python 3.8+, PyInstaller, ffmpeg (für Full-Build)

## 📝 Credits

- **Idee:** Fabian Kirchweger
- **Code:** GLM-5.1 (via OpenClaw)
- **Lizenz:** MIT

## 🤝 Beitrag leisten

Beiträge sind willkommen! Bitte erstelle ein Issue oder einen Pull Request.

## ⚖️ Lizenz

[MIT License](LICENSE) – frei verwendbar, modifizierbar und verteilbar.