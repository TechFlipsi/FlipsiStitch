# FlipsiStitch

[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)](https://github.com/TechFlipsi/FlipsiStitch/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

**Lossless video segment merging – with color correction, GPU acceleration & modern web UI**

DJI cameras (Osmo Action 5 Pro, Pocket 4, drones) intentionally split long recordings into multiple files – reportedly for file safety. FlipsiStitch automatically detects these segments and merges them losslessly into a single file.

## ✨ Features

- 🎬 **Auto Segment Detection** – Recognizes DJI, GoPro and any camera by sequential numbering
- 🔗 **Lossless Merging** – ffmpeg concat demuxer with `-c copy` (no re-encoding)
- 🎨 **Color Correction** – D-Log M → Rec.709, auto white balance, or both combined
- ⚡ **GPU Acceleration** – NVENC (NVIDIA), AMF (AMD), QSV (Intel), VideoToolbox (Mac)
- 📦 **H.265/HEVC** – Modern codec standard for re-encoded output, ~40% smaller files
- 🖥️ **Modern Web UI** – Dark mode, glassmorphism, drag & drop, live progress
- 🔄 **Auto-Update** – GitHub release checking, downgrade protection, automatic updates
- 📋 **Portable** – Single `.exe`, no Python, no installation required

## 🚀 Quick Start

### Portable (Recommended)

1. Download latest `FlipsiStitch_X.X.X_Full.zip` from [Releases](https://github.com/TechFlipsi/FlipsiStitch/releases)
2. Extract
3. Run `flipsisitch.exe`
4. Browser opens automatically → select folder → merge!

### With Python

```bash
git clone https://github.com/TechFlipsi/FlipsiStitch.git
cd FlipsiStitch
python flipsisitch.py
```

Requires: [ffmpeg](https://ffmpeg.org/) in PATH or same directory.

## 🎨 Color Correction

FlipsiStitch offers three correction profiles:

| Profile | Description | Re-Encoding |
|---------|-------------|-------------|
| **None** (Default) | Lossless, no changes | No (`-c copy`) |
| **D-Log M → Rec.709** | Converts DJI D-Log M to normal color space | Yes (H.265) |
| **White Balance** | Auto white balance (grayworld) | Yes (H.265) |
| **D-Log M + White Balance** | Both combined (recommended) | Yes (H.265) |

### Safety

- D-Log M conversion is **only applied** when the video actually contains D-Log M (auto-detected via ffprobe)
- White balance has sanity checks – skipped if results are unreliable
- `--test-color` extracts frames BEFORE/AFTER correction for comparison

## ⚡ GPU Acceleration

Auto-detection with priority:

1. **NVENC** (NVIDIA) – `-c:v hevc_nvenc`
2. **AMF** (AMD) – `-c:v hevc_amf`
3. **QSV** (Intel) – `-c:v hevc_qsv`
4. **VideoToolbox** (macOS) – `-c:v hevc_videotoolbox`
5. **CPU Fallback** – `-c:v libx265`

CLI: `--hwaccel auto|nvenc|amf|qsv|videotoolbox|cpu`

## 🖥️ CLI

```bash
# Scan folder and merge
flipsisitch /path/to/folder

# With color correction
flipsisitch /path --color-profile both

# Dry run (show what would be done)
flipsisitch /path --dry-run

# Choose codec
flipsisitch /path --codec hevc

# Check for updates
flipsisitch --check-update

# Show version
flipsisitch --version
```

## 🔧 Build

```bash
# Portable .exe (without ffmpeg)
build.bat

# Full .exe (with ffmpeg embedded)
build_full.bat

# Release ZIP
build_release.bat
```

Requires: Python 3.8+, PyInstaller, ffmpeg (for full build)

## 📝 Credits

- **Idea:** Fabian Kirchweger
- **Code:** GLM-5.1 (via OpenClaw)
- **License:** MIT

## 🤝 Contributing

Contributions are welcome! Please create an Issue or Pull Request.

## ⚖️ License

[MIT License](LICENSE) – free to use, modify, and distribute.