# Changelog

## [4.0.0] - 2026-05-13

### Added
- 🎬 Automatische Segment-Erkennung (DJI, GoPro, alle Kameras)
- 🔗 Verlustfreies Mergen via ffmpeg concat demuxer (`-c copy`)
- 🎨 D-Log M → Rec.709 Farbkorrektur (automatische Erkennung via ffprobe)
- 🎨 Weißabgleich (grayworld Filter)
- 🎨 Farbkorrektur-Profile: none, dlogm, whitebalance, both
- ⚡ H.265/HEVC als Standard-Codec bei Re-Encoding
- ⚡ GPU-Beschleunigung (NVENC, AMF, QSV, VideoToolbox)
- 🖥️ Moderne Web-UI mit Dark Mode & Glasmorphismus
- 🔄 Auto-Update über GitHub Releases (Downgrade-Schutz)
- 📦 Portable .exe mit eingebettetem ffmpeg (PyInstaller)
- 📋 Drag & Drop, Live-Fortschrittsbalken, Codec/GPU-Auswahl
- 🧪 `--test-color` für Vorschau-Bilder vor/nach Farbkorrektur
- 🔒 Sanity Checks: D-Log M nur wenn erkannt, Weißabglied nur wenn zuverlässig