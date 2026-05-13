#!/usr/bin/env python3
"""
FlipsiStitch – Verlustfreies Zusammenfügen von DJI-Videosegmenten mit Web-UI.

DJI-Kameras (Osmo Action, Pocket-Serie, Drohnen) splitten lange Videos
wegen des FAT32/4GB-Dateisystemlimits in mehrere Segmente (z.B.
DJI_20240101120000_0001.MP4, DJI_20240101120000_0002.MP4, …).

FlipsiStitch erkennt zusammengehörige Segmente automatisch und fügt sie
verlustfrei per ffmpeg concat demuxer zu einer einzigen Datei zusammen.

NEU: D-Log M → Rec.709 Konvertierung, H.265/HEVC Standard-Codec,
GPU-Hardware-Beschleunigung, Auto-Update, Premium Web-UI.

Usage:
    flipsisitch [ORDNER] [OPTIONEN]
    flipsisitch                              # aktuellen Ordner scannen
    flipsisitch /pfad/zum/ordner             # Ordner scannen & mergen
    flipsisitch --dry-run                    # nur anzeigen, nichts tun
    flipsisitch --force --output ./merged    # ohne Rückfrage, in merged/
    flipsisitch --group "DJI_20240101120000" # nur eine Gruppe mergen
    flipsisitch --web                        # Web-UI starten
    flipsisitch --color-profile dlogm       # D-Log M → Rec.709
    flipsisitch --color-profile both         # D-Log M + Weißabgleich
    flipsisitch --update                     # Auf neueste Version updaten
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from collections import defaultdict
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

try:
    import semver as _semver_lib
    _HAS_SEMVER = True
except ImportError:
    _HAS_SEMVER = False

try:
    from urllib.parse import urlparse, parse_qs
    _HAS_URLPARSE = True
except ImportError:
    _HAS_URLPARSE = False

__version__ = "4.0.0"

log = logging.getLogger("flipsisitch")

# ---------------------------------------------------------------------------
# ffmpeg Auto-Download Infrastruktur (Portable Mode)
# ---------------------------------------------------------------------------

# Bekannte SHA256-Hashes ffmpeg static builds (ffmpeg-release-essentials.zip)
# Wird bei jedem Download aktualisiert – Erst-Download akzeptiert jeden Hash
_FFMPEG_KNOWN_HASHES: Dict[str, str] = {}

# Config-Verzeichnis für ffmpeg-Cache und Einstellungen
def _get_config_dir() -> Path:
    """Return the FlipsiStitch config directory.

    Windows: %APPDATA%/FlipsiStitch
    Linux: ~/.config/flipsisitch
    macOS: ~/Library/Application Support/FlipsiStitch
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return Path(base) / "FlipsiStitch"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FlipsiStitch"
    else:
        # Linux / other: XDG_CONFIG_HOME or ~/.config
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return Path(base) / "flipsisitch"


def _get_cached_ffmpeg_path() -> Optional[Path]:
    """Return path to cached ffmpeg.exe/ffmpeg in config dir, or None."""
    cfg = _get_config_dir()
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    cached = cfg / exe_name
    if cached.is_file() and os.access(cached, os.X_OK):
        return cached
    return None


def _get_cached_ffprobe_path() -> Optional[Path]:
    """Return path to cached ffprobe.exe/ffprobe in config dir, or None."""
    cfg = _get_config_dir()
    exe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    cached = cfg / exe_name
    if cached.is_file() and os.access(cached, os.X_OK):
        return cached
    return None


def _get_ffmpeg_download_url() -> str:
    """Return the URL for downloading ffmpeg static build.

    Uses gyan.dev for Windows (most reliable static builds),
    and returns a sensible default for other platforms (though
    the auto-download is primarily for Windows portable mode).
    """
    # Primary: gyan.dev Windows static builds (essentials = smaller)
    if sys.platform == "win32":
        return "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    else:
        # Linux/macOS users typically have package managers.
        # Return a URL to official download page as fallback.
        return "https://ffmpeg.org/download.html"


# Fortschritts-Callback für ffmpeg-Download (wird vom Web-UI gesetzt)
_ffmpeg_download_progress_cb: Any = None


# ---------------------------------------------------------------------------
# GPU / Hardware Acceleration Detection
# ---------------------------------------------------------------------------

def _detect_gpu_encoders() -> Dict[str, Dict]:
    """Detect available GPU hardware encoders on the system.

    Returns dict like:
        {
            "nvenc": {"encoder": "hevc_nvenc", "available": True, "gpu": "NVIDIA"},
            "amf": {"encoder": "hevc_amf", "available": False, "gpu": "AMD"},
            "qsv": {"encoder": "hevc_qsv", "available": False, "gpu": "Intel"},
            "videotoolbox": {"encoder": "hevc_videotoolbox", "available": False, "gpu": "Apple"},
        }
    """
    encoders = {
        "nvenc": {"encoder": "hevc_nvenc", "available": False, "gpu": "NVIDIA", "order": 1},
        "amf": {"encoder": "hevc_amf", "available": False, "gpu": "AMD", "order": 2},
        "qsv": {"encoder": "hevc_qsv", "available": False, "gpu": "Intel", "order": 3},
        "videotoolbox": {"encoder": "hevc_videotoolbox", "available": False, "gpu": "Apple", "order": 4},
    }

    # Check NVIDIA via nvidia-smi
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            encoders["nvenc"]["available"] = True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Check AMD GPU via clinfo or lspci
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5
            )
            if "amd" in result.stdout.lower() or "radeon" in result.stdout.lower():
                encoders["amf"]["available"] = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    else:
        try:
            result = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and (
                "amd" in result.stdout.lower() or "radeon" in result.stdout.lower()
            ):
                encoders["amf"]["available"] = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    # Check Intel QSV
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5
            )
            if "intel" in result.stdout.lower() or "iris" in result.stdout.lower():
                encoders["qsv"]["available"] = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    else:
        try:
            result = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and (
                "intel" in result.stdout.lower()
            ):
                encoders["qsv"]["available"] = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    # Apple VideoToolbox (only on macOS)
    if sys.platform == "darwin":
        encoders["videotoolbox"]["available"] = True
        encoders["videotoolbox"]["order"] = 1  # Preferred on macOS

    return encoders


def _get_gpu_encoder_params(encoder_key: str) -> Dict[str, str]:
    """Return ffmpeg encoder parameters for a given GPU encoder.

    Returns dict with keys: encoder, params (extra ffmpeg args string)
    """
    params = {
        "nvenc": {
            "encoder": "hevc_nvenc",
            "params": "-preset p7 -rc vbr -cq 18 -b:v 0 -pix_fmt yuv420p",
            "h264_encoder": "h264_nvenc",
            "h264_params": "-preset p7 -rc vbr -cq 18 -b:v 0 -pix_fmt yuv420p",
        },
        "amf": {
            "encoder": "hevc_amf",
            "params": "-quality quality -rc vbr_peak -qp_i 18 -qp_p 18 -pix_fmt yuv420p",
            "h264_encoder": "h264_amf",
            "h264_params": "-quality quality -rc vbr_peak -qp_i 18 -qp_p 18 -pix_fmt yuv420p",
        },
        "qsv": {
            "encoder": "hevc_qsv",
            "params": "-preset medium -global_quality 18 -pix_fmt yuv420p",
            "h264_encoder": "h264_qsv",
            "h264_params": "-preset medium -global_quality 18 -pix_fmt yuv420p",
        },
        "videotoolbox": {
            "encoder": "hevc_videotoolbox",
            "params": "-q:v 65 -pix_fmt yuv420p",
            "h264_encoder": "h264_videotoolbox",
            "h264_params": "-q:v 65 -pix_fmt yuv420p",
        },
        "cpu": {
            "encoder": "libx265",
            "params": "-preset medium -crf 18 -pix_fmt yuv420p",
            "h264_encoder": "libx264",
            "h264_params": "-preset medium -crf 18 -pix_fmt yuv420p",
        },
    }
    return params.get(encoder_key, params["cpu"])


def _get_best_gpu_encoder(hwaccel: str = "auto") -> str:
    """Determine the best available GPU encoder.

    Args:
        hwaccel: "auto", "nvenc", "amf", "qsv", "videotoolbox", "cpu"

    Returns:
        Encoder key string ("nvenc", "amf", "qsv", "videotoolbox", "cpu")
    """
    if hwaccel == "cpu":
        return "cpu"

    gpu_encoders = _detect_gpu_encoders()

    if hwaccel != "auto":
        if hwaccel in gpu_encoders and gpu_encoders[hwaccel]["available"]:
            return hwaccel
        log.warning(
            "Angeforderter GPU-Encoder '%s' nicht verfügbar, fallback auf CPU",
            hwaccel,
        )
        return "cpu"

    # Auto-detect: prioritize by order
    sorted_encoders = sorted(
        gpu_encoders.values(), key=lambda x: x["order"]
    )
    for enc in sorted_encoders:
        if enc["available"]:
            for key, val in gpu_encoders.items():
                if val == enc:
                    return key

    return "cpu"


# ---------------------------------------------------------------------------
# D-Log M Detection & Color Profile System
# ---------------------------------------------------------------------------

# DJI D-Log M metadata identifiers (case-insensitive matching)
_DLOG_M_INDICATORS = [
    "d-log m",
    "dlog-m",
    "dlogm",
    "d-log",
]


def _detect_dlog_m(filepath: Path, ffprobe_bin: Optional[str] = None) -> bool:
    """Detect if a video file was shot in D-Log M color profile.

    Uses ffprobe to read metadata tags:
    - Stream metadata: 'color_transfer', 'color_primaries', 'color_space'
    - Format metadata: 'comment', 'encoded_by', custom tags
    - DJI-specific metadata in video stream

    Returns True if D-Log M is detected.
    """
    if ffprobe_bin is None:
        ffprobe_bin = _find_ffprobe(_find_ffmpeg())

    try:
        # Get stream metadata in flat format
        result = subprocess.run(
            [
                ffprobe_bin, "-v", "quiet",
                "-show_entries", "stream=color_transfer,color_primaries,color_space,codec_tag_string:stream_tags=all",
                "-show_entries", "format_tags=all",
                "-of", "json",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            # Fallback: try with just format tags
            result = subprocess.run(
                [
                    ffprobe_bin, "-v", "quiet",
                    "-show_entries", "format_tags=all",
                    "-of", "json",
                    str(filepath),
                ],
                capture_output=True, text=True, timeout=30,
            )

        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            text_to_check = json.dumps(data).lower()

            for indicator in _DLOG_M_INDICATORS:
                if indicator in text_to_check:
                    return True

            # Also check specific DJI metadata patterns
            # DJI may set 'color_transfer=arib-std-b67' or custom 'DJI' tags
            if "arib-std-b67" in text_to_check or "smpte2084" in text_to_check:
                return True

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        log.debug("D-Log M detection failed for %s: %s", filepath.name, e)

    # Also try the metadata extraction we already have
    meta = _get_video_metadata(filepath)
    if meta:
        extra = meta.get("extra", {})
        extra_str = json.dumps(extra).lower()
        for indicator in _DLOG_M_INDICATORS:
            if indicator in extra_str:
                return True

    return False


def _get_dlog_m_rec709_filter() -> str:
    """Return ffmpeg filter string for D-Log M → Rec.709 conversion.

    DJI D-Log M specification (approximate):
    - 10-bit full range
    - Black level: ~95 (10-bit) or ~0.0928 normalized
    - White level: ~940 (10-bit) or ~0.918 normalized
    - Gamma curve correction to Rec.709 2.4 gamma

    The filter chain:
    1. Level adjustment to expand D-Log M range to full range
    2. Gamma correction to match Rec.709 viewing gamma
    3. Contrast enhancement
    """
    # D-Log M characteristic: lift shadows, compress highlights
    # Using eq + curves filter for a natural Rec.709 look
    #
    # Normalized values: D-Log M black=0.0928, reference white=0.918
    # We apply: eq + colorchannelmixer for matrix correction
    #
    # More accurate approach with lut3d would be best but eq provides
    # a good approximation without requiring a .cube file:
    return (
        "eq=gamma=1.8:brightness=0.045:contrast=1.15:saturation=1.1,"
        "colorchannelmixer=.95:.05:.0:.0:.05:.95:.0:.0:.0:.05:.95:0:0:0"
    )


def _get_whitebalance_filter() -> str:
    """Return ffmpeg filter string for automatic white balance."""
    return "grayworld=1"


def _get_color_filter_chain(color_profile: str, is_dlog_m: bool) -> str:
    """Build the complete color filter chain based on profile and D-Log M status.

    Args:
        color_profile: "dlogm", "whitebalance", "both", "none"
        is_dlog_m: Whether D-Log M was detected in the source

    Returns:
        ffmpeg filter chain string, or empty string if no filters needed
    """
    if color_profile == "none":
        return ""

    filters = []

    if color_profile in ("dlogm", "both"):
        if is_dlog_m:
            filters.append(_get_dlog_m_rec709_filter())
        else:
            log.warning(
                "D-Log M Profil angefordert, aber Video ist nicht D-Log M – "
                "übersprungen"
            )

    if color_profile in ("whitebalance", "both"):
        filters.append(_get_whitebalance_filter())

    return ",".join(filters)


# ---------------------------------------------------------------------------
# Semver Comparison (no external dependency)
# ---------------------------------------------------------------------------

def _semver_compare(ver_a: str, ver_b: str) -> int:
    """Compare two semver strings.

    Returns:
        -1 if ver_a < ver_b
         0 if ver_a == ver_b
         1 if ver_a > ver_b

    Handles simple semver: MAJOR.MINOR.PATCH with optional pre-release.
    """
    if _HAS_SEMVER:
        try:
            from semver import VersionInfo
            va = VersionInfo.parse(ver_a)
            vb = VersionInfo.parse(ver_b)
            if va < vb: return -1
            if va > vb: return 1
            return 0
        except Exception:
            pass

    # Fallback: parse manually
    def _parse(v: str) -> Tuple[int, int, int]:
        # Strip leading 'v' if present
        v = v.lstrip("v").split("-")[0]
        parts = v.split(".")
        maj = int(parts[0]) if len(parts) > 0 else 0
        min_ = int(parts[1]) if len(parts) > 1 else 0
        pat = int(parts[2]) if len(parts) > 2 else 0
        return (maj, min_, pat)

    pa = _parse(ver_a)
    pb = _parse(ver_b)

    if pa < pb: return -1
    if pa > pb: return 1
    return 0


# ---------------------------------------------------------------------------
# Auto-Update via GitHub Releases
# ---------------------------------------------------------------------------

GITHUB_REPO_API = "https://api.github.com/repos/TechFlipsi/FlipsiStitch/releases/latest"
GITHUB_REPO_RELEASES = "https://github.com/TechFlipsi/FlipsiStitch/releases"

def _check_for_update(ffmpeg_bin: Optional[str] = None) -> Optional[Dict]:
    """Check GitHub for a newer version of FlipsiStitch.

    Returns:
        None if no update available or check failed.
        Dict with version info if update available:
            {"version": "x.y.z", "url": "...", "body": "...", "assets": [...]}
    """
    try:
        req = urllib.request.Request(
            GITHUB_REPO_API,
            headers={
                "User-Agent": f"FlipsiStitch/{__version__}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))

        latest_ver = release.get("tag_name", "").lstrip("v")

        if not latest_ver:
            return None

        comparison = _semver_compare(latest_ver, __version__)

        # DOWNGRADE PROTECTION: Only update if newer, never same or older
        if comparison <= 0:
            log.debug(
                "Keine neuere Version verfügbar (aktuell: %s, latest: %s)",
                __version__, latest_ver,
            )
            return None

        return {
            "version": latest_ver,
            "url": release.get("html_url", GITHUB_REPO_RELEASES),
            "body": release.get("body", ""),
            "assets": [
                {
                    "name": a.get("name", ""),
                    "url": a.get("browser_download_url", ""),
                    "size": a.get("size", 0),
                }
                for a in release.get("assets", [])
            ],
        }

    except urllib.error.HTTPError as e:
        if e.code == 403:
            log.debug("GitHub API Rate-Limit erreicht.")
        else:
            log.debug("Update-Check HTTP-Fehler: %s", e)
        return None
    except Exception as e:
        log.debug("Update-Check fehlgeschlagen: %s", e)
        return None


def _download_update(version: str, dest_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Download the latest FlipsiStitch .exe update.

    Args:
        version: Version string to download
        dest_dir: Directory to save the new .exe (default: exe directory)

    Returns:
        (success, message)
    """
    if dest_dir is None:
        if getattr(sys, 'frozen', False):
            dest_dir = Path(sys.executable).parent
        else:
            dest_dir = Path(__file__).parent

    # Find the right asset for this platform
    try:
        req = urllib.request.Request(
            GITHUB_REPO_API,
            headers={
                "User-Agent": f"FlipsiStitch/{__version__}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))

        assets = release.get("assets", [])
        exe_asset = None

        for a in assets:
            name = a.get("name", "").lower()
            url = a.get("browser_download_url", "")
            if name.endswith(".exe") and "flipsisitch" in name:
                exe_asset = url
                break

        if not exe_asset:
            return False, "Kein passendes .exe Asset im Release gefunden."

        # Download to new file
        new_path = dest_dir / "flipsisitch_new.exe"
        old_path = dest_dir / "flipsisitch.exe"
        backup_path = dest_dir / "flipsisitch_backup.exe"

        log.info("📥 Lade Update %s herunter …", version)

        if not _download_with_progress(exe_asset, new_path):
            return False, "Download fehlgeschlagen."

        # Verify SHA256 if available in release assets
        # (GitHub API doesn't provide sha256 directly, skip for now)

        # Replace: old → backup, new → current
        if old_path.exists():
            # Remove old backup
            backup_path.unlink(missing_ok=True)
            shutil.move(str(old_path), str(backup_path))

        shutil.move(str(new_path), str(old_path))

        log.info(
            "✅ Update erfolgreich! Alte Version als %s gesichert.",
            backup_path.name,
        )
        return True, f"Update auf {version} erfolgreich. Bitte neu starten."

    except Exception as e:
        return False, f"Update-Download fehlgeschlagen: {e}"


# ---------------------------------------------------------------------------
# Codec Selection & Encoding Helpers
# ---------------------------------------------------------------------------

def _get_encoder_cmd(
    codec: str,
    hwaccel: str,
    is_hevc: bool,
) -> List[str]:
    """Build ffmpeg encoder command arguments.

    Args:
        codec: "hevc", "h264", or "copy"
        hwaccel: GPU encoder key or "cpu"
        is_hevc: True for H.265/HEVC, False for H.264

    Returns:
        List of ffmpeg argument strings for the encoder
    """
    if codec == "copy":
        return ["-c", "copy"]

    encoder_key = hwaccel if hwaccel != "cpu" else "cpu"
    params = _get_gpu_encoder_params(encoder_key)

    if is_hevc:
        encoder = params["encoder"]
        extra = params["params"]
    else:
        encoder = params.get("h264_encoder", "libx264")
        extra = params.get("h264_params", "-preset medium -crf 18 -pix_fmt yuv420p")

    return ["-c:v", encoder] + extra.split() + ["-c:a", "copy"]


def _download_with_progress(url: str, dest: Path) -> bool:
    """Download a file from *url* to *dest* with progress reporting.

    Reports progress via _sse_push (for Web-UI) and stdout (for CLI).
    Respects HTTP_PROXY/HTTPS_PROXY environment variables.

    Returns True on success, False on failure.
    """
    try:
        # Configure proxy support
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
        if proxy_url:
            from urllib.request import ProxyHandler, build_opener, install_opener
            proxy_handler = ProxyHandler({
                "http": proxy_url,
                "https": proxy_url,
            })
            opener = build_opener(proxy_handler)
            install_opener(opener)
            log.info("Verwende Proxy: %s", proxy_url)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"FlipsiStitch/{__version__} (ffmpeg downloader)",
            },
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            total_size = int(content_length) if content_length else 0

            downloaded = 0
            chunk_size = 65536  # 64 KB
            last_report = 0.0

            with open(dest, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Report progress every 0.5 seconds
                    now = time.time()
                    if now - last_report >= 0.5:
                        last_report = now
                        if total_size > 0:
                            pct = int(downloaded * 100 / total_size)
                            _sse_push({
                                "type": "ffmpeg_download_progress",
                                "downloaded": downloaded,
                                "total": total_size,
                                "percent": pct,
                            })
                            if _ffmpeg_download_progress_cb:
                                try:
                                    _ffmpeg_download_progress_cb(downloaded, total_size)
                                except Exception:
                                    pass

            if total_size > 0 and downloaded < total_size:
                log.error("Download unvollständig: %d/%d Bytes", downloaded, total_size)
                return False

            return True

    except urllib.error.URLError as e:
        log.error("Netzwerk-Fehler beim Download: %s", e)
        return False
    except Exception as e:
        log.error("Download-Fehler: %s", e)
        return False


def _verify_sha256(filepath: Path, expected_hash: Optional[str] = None) -> bool:
    """Verify SHA256 hash of a file.

    If *expected_hash* is given, checks against it.
    If not given but _FFMPEG_KNOWN_HASHES has entries, checks against those.
    If no known hashes, accepts the file (first-time download).

    Returns True if verification passes (or skipped).
    """
    if not expected_hash and not _FFMPEG_KNOWN_HASHES:
        # First download: no hash to check against
        return True

    try:
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sha.update(chunk)
        actual = sha.hexdigest()

        if expected_hash:
            if actual.lower() != expected_hash.lower():
                log.error("SHA256-Hash stimmt nicht überein!")
                log.error("  Erwartet: %s", expected_hash)
                log.error("  Erhalten:  %s", actual)
                return False
            return True

        # Check against known hashes
        fname = filepath.name.lower()
        for known_name, known_hash in _FFMPEG_KNOWN_HASHES.items():
            if known_name in fname or fname.startswith(known_name):
                if actual.lower() != known_hash.lower():
                    log.warning(
                        "⚠ SHA256-Hash weicht vom bekannten Hash ab – "
                        "möglicherweise eine neuere Version."
                    )
                    log.debug("  Erwartet: %s", known_hash)
                    log.debug("  Erhalten:  %s", actual)
                return True

        return True
    except Exception as e:
        log.warning("Konnte SHA256 nicht prüfen: %s", e)
        return True


def _find_ffmpeg_in_zip_dir(extract_dir: Path) -> Optional[Path]:
    """Search extracted ffmpeg directory for ffmpeg executable."""
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    # Walk through extracted directory
    for root, dirs, files in os.walk(str(extract_dir)):
        for f in files:
            if f.lower() == exe_name.lower():
                return Path(root) / f
            # Also check without extension (Linux)
            if f == "ffmpeg" and sys.platform != "win32":
                return Path(root) / f

    return None


def _download_and_install_ffmpeg() -> Tuple[bool, str]:
    """Download ffmpeg static build and install to config directory.

    Returns (success, message).

    Workflow:
    1. Create config directory if not exists
    2. Download ffmpeg-release-essentials.zip to temp
    3. Verify SHA256 (skip on first download)
    4. Extract zip
    5. Find ffmpeg.exe and ffprobe.exe inside
    6. Copy to config directory
    7. Clean up temp files
    """
    url = _get_ffmpeg_download_url()
    cfg = _get_config_dir()
    _ensure_dir(cfg)

    if sys.platform != "win32":
        return False, (
            f"Automatischer ffmpeg-Download wird nur unter Windows unterstützt.\n"
            f"Bitte installiere ffmpeg manuell: {url}"
        )

    _sse_push({
        "type": "ffmpeg_download_start",
        "url": url,
        "msg": "Lade ffmpeg herunter …",
    })

    log.info("📥 Lade ffmpeg herunter von: %s", url)
    log.info("   Dies geschieht nur beim ersten Start.")

    # Download to temp file
    tmpdir = Path(tempfile.mkdtemp(prefix="flipsisitch_ffmpeg_"))
    zip_path = tmpdir / "ffmpeg.zip"

    try:
        # ── Download ──────────────────────────────────────────────
        if not _download_with_progress(url, zip_path):
            return False, (
                "Download fehlgeschlagen.\n"
                "Bitte prüfe deine Internetverbindung und versuche es erneut.\n"
                f"Alternativ: Lade ffmpeg manuell herunter von {url}\n"
                "und kopiere ffmpeg.exe und ffprobe.exe in den gleichen Ordner wie flipsisitch.exe"
            )

        # ── Verify ─────────────────────────────────────────────────
        log.info("   Prüfe Integrität …")
        if not _verify_sha256(zip_path):
            zip_path.unlink(missing_ok=True)
            return False, (
                "SHA256-Prüfung fehlgeschlagen. Die heruntergeladene Datei könnte beschädigt sein.\n"
                f"Bitte lade ffmpeg manuell herunter von {url}"
            )

        # ── Extract ────────────────────────────────────────────────
        log.info("   Entpacke …")
        _sse_push({"type": "ffmpeg_download_progress", "percent": 100,
                    "msg": "Entpacke ffmpeg …"})

        extract_dir = tmpdir / "extract"
        _ensure_dir(extract_dir)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            return False, (
                "Die heruntergeladene ZIP-Datei ist beschädigt.\n"
                f"Bitte lade ffmpeg manuell herunter von {url}"
            )

        # ── Find binaries ──────────────────────────────────────────
        ffmpeg_src = _find_ffmpeg_in_zip_dir(extract_dir)
        if ffmpeg_src is None:
            return False, (
                "ffmpeg.exe wurde im ZIP-Archiv nicht gefunden.\n"
                f"Bitte lade ffmpeg manuell herunter von {url}"
            )

        # Find ffprobe next to ffmpeg
        ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        ffprobe_src = ffmpeg_src.parent / ffprobe_name
        if not ffprobe_src.is_file():
            # Try searching
            found_fp = None
            for root, dirs, files in os.walk(str(extract_dir)):
                for f in files:
                    if f.lower() == ffprobe_name.lower():
                        found_fp = Path(root) / f
                        break
            ffprobe_src = found_fp

        # ── Copy to config dir ─────────────────────────────────────
        ffmpeg_exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        ffprobe_exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"

        dest_ffmpeg = cfg / ffmpeg_exe
        dest_ffprobe = cfg / ffprobe_exe

        log.info("   Kopiere ffmpeg → %s", dest_ffmpeg)
        shutil.copy2(ffmpeg_src, dest_ffmpeg)
        os.chmod(dest_ffmpeg, 0o755)

        if ffprobe_src and ffprobe_src.is_file():
            log.info("   Kopiere ffprobe → %s", dest_ffprobe)
            shutil.copy2(ffprobe_src, dest_ffprobe)
            os.chmod(dest_ffprobe, 0o755)
        else:
            log.warning("   ffprobe.exe nicht im Archiv gefunden – ffmpeg reicht für die Grundfunktion.")

        _sse_push({"type": "ffmpeg_download_done", "msg": "ffmpeg bereit!"})
        log.info("✅ ffmpeg wurde erfolgreich installiert.")

        return True, "ffmpeg wurde erfolgreich heruntergeladen und installiert."

    finally:
        # Cleanup
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass


def _ensure_ffmpeg_available() -> str:
    """Ensure ffmpeg is available, downloading if necessary.

    Check order:
    1. Explicit --ffmpeg path (handled by _find_ffmpeg)
    2. System PATH
    3. Inside PyInstaller bundle (sys._MEIPASS)
    4. In same directory as flipsisitch.exe (portable)
    5. Cached in config directory
    6. Auto-download (Windows only)

    Returns path to ffmpeg executable.
    Raises FileNotFoundError if ffmpeg cannot be obtained.
    """
    # 2. Check system PATH
    found = shutil.which("ffmpeg")
    if found:
        return found

    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    # 3. Check inside PyInstaller bundle (sys._MEIPASS)
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            bundled_ffmpeg = Path(meipass) / exe_name
            if bundled_ffmpeg.is_file() and os.access(bundled_ffmpeg, os.X_OK):
                log.debug("ffmpeg aus PyInstaller Bundle: %s", bundled_ffmpeg)
                return str(bundled_ffmpeg)

        # 3b. Check same directory as executable (portable mode)
        exe_dir = Path(sys.executable).parent
    else:
        # Running as script
        exe_dir = Path(__file__).parent.resolve()

    local_ffmpeg = exe_dir / exe_name
    if local_ffmpeg.is_file() and os.access(local_ffmpeg, os.X_OK):
        return str(local_ffmpeg)

    # 5. Check config directory cache
    cached = _get_cached_ffmpeg_path()
    if cached:
        return str(cached)

    # 6. Auto-download (Windows only)
    if sys.platform == "win32":
        log.info("ffmpeg nicht gefunden – starte automatischen Download …")
        log.info("(Nur beim ersten Start – danach wird die lokale Version genutzt)")
        ok, msg = _download_and_install_ffmpeg()
        if ok:
            cached = _get_cached_ffmpeg_path()
            if cached:
                return str(cached)

        raise FileNotFoundError(
            f"ffmpeg konnte nicht automatisch installiert werden:\n{msg}\n\n"
            "Manuelle Installation:\n"
            "  1. Lade ffmpeg herunter von https://ffmpeg.org/download.html\n"
            "  2. Kopiere ffmpeg.exe und ffprobe.exe in den gleichen Ordner wie flipsisitch.exe\n"
            "     ODER installiere ffmpeg im System-PATH"
        )
    else:
        raise FileNotFoundError(
            "ffmpeg wurde nicht im PATH gefunden. "
            "Installiere ffmpeg oder verwende --ffmpeg /pfad/zu/ffmpeg"
        )

# ---------------------------------------------------------------------------
# Globale Konfiguration (wird von CLI/Web gesetzt)
# ---------------------------------------------------------------------------

_config = {
    "dji_prefix": "DJI_",           # Konfigurierbares DJI-Präfix
    "color_threshold": 0.08,        # Schwelle für Farbsprung-Erkennung (0.0–1.0)
    "color_profile": "none",        # none, dlogm, whitebalance, both
    "codec": "hevc",                # hevc, h264, copy
    "hwaccel": "auto",              # auto, nvenc, amf, qsv, videotoolbox, cpu
    "update_checked": False,        # Ob Update-Check beim Start erfolgte
    "update_available": None,       # Dict mit Update-Info oder None
}

# SSE-Queue für Web-UI Progress (thread-safe)
_sse_queue: List[Dict] = []
_sse_lock = threading.Lock()

def _sse_push(data: Dict) -> None:
    """Push a message to all connected SSE clients."""
    with _sse_lock:
        _sse_queue.append(data)

def _sse_drain() -> List[Dict]:
    """Drain and return all pending SSE messages."""
    with _sse_lock:
        msgs = _sse_queue[:]
        _sse_queue.clear()
        return msgs

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Generisches Segment-Pattern: Beliebiges Präfix + fortlaufende Nummer am Ende
# Entscheidend: Die NUMMER MUSS am Ende des Stem stehen (vor Extension).
# Erfasst: (.+)_(\d{3,6})\.(mp4|mov|MP4|MOV)
#   wobei die digits die LETZTE underscore-getrennte Komponente des Stems sind.
# Bsp: DJI_20240101120000_0001.MP4 → key=dji_20240101120000, num=1
#      VID_2024_001.mp4 → key=vid_2024, num=1
#      GOPRO_00001.MOV → key=gopro, num=1
#      DJI_0001_sub.MP4 → key=dji_0001_sub, num=??  (sub hat keine nummer)
RE_SEGMENT = re.compile(
    r"^(.+)_(\d{3,6})\.(mp4|mov)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(path: str) -> str:
    """Convert a path to lowercase for case-insensitive comparison."""
    return path.lower()

def _ensure_dir(path: Path) -> None:
    """Create directory if it doesn't exist (mkdir -p)."""
    path.mkdir(parents=True, exist_ok=True)

def _human_size(num_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"

def _find_ffmpeg(explicit: Optional[str] = None) -> str:
    """Locate ffmpeg executable.

    If no explicit path given and ffmpeg not in PATH, tries:
    - Same directory as flipsisitch.exe (portable)
    - Config directory cache (%APPDATA%/FlipsiStitch on Windows)
    - Auto-download (Windows only, first start)
    """
    if explicit:
        resolved = shutil.which(explicit) or explicit
        if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
        raise FileNotFoundError(f"ffmpeg nicht gefunden unter: {explicit}")

    try:
        return _ensure_ffmpeg_available()
    except FileNotFoundError:
        raise

def _find_ffprobe(explicit: Optional[str] = None, ffmpeg_bin: Optional[str] = None) -> str:
    """Locate ffprobe executable.

    Looks in order:
    1. Explicit --ffprobe-equivalent path
    2. Next to the found ffmpeg binary
    3. System PATH
    4. Config directory cache
    """
    if explicit:
        resolved = shutil.which(explicit) or explicit
        if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
        raise FileNotFoundError(f"ffprobe nicht gefunden unter: {explicit}")

    # Look next to ffmpeg binary first
    if ffmpeg_bin:
        ffmpeg_path = Path(ffmpeg_bin)
        ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        next_to = ffmpeg_path.parent / ffprobe_name
        if next_to.is_file() and os.access(next_to, os.X_OK):
            return str(next_to)

    # Check system PATH
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe

    # Check config directory cache
    cached = _get_cached_ffprobe_path()
    if cached:
        return str(cached)

    # Check same directory as executable (portable)
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
    else:
        exe_dir = Path(__file__).parent.resolve()

    ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    local_ffprobe = exe_dir / ffprobe_name
    if local_ffprobe.is_file() and os.access(local_ffprobe, os.X_OK):
        return str(local_ffprobe)

    # As last resort: ffmpeg can sometimes cover ffprobe functionality
    # Return None-like sentinel so callers can fall back
    return ""

# ---------------------------------------------------------------------------
# Metadata extraction via ffprobe
# ---------------------------------------------------------------------------

def _get_video_metadata(filepath: Path, ffprobe_bin: Optional[str] = None, ffmpeg_bin: Optional[str] = None) -> Optional[Dict]:
    """Extract video metadata using ffprobe.

    Returns dict with keys: codec, width, height, fps, duration, creation_time,
    bitrate, or None if probe fails.
    """
    fb = ffprobe_bin or _find_ffprobe(ffmpeg_bin=ffmpeg_bin)
    if not fb:
        return None

    try:
        result = subprocess.run(
            [
                fb,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                "-select_streams", "v:0",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        if not streams:
            return None

        vs = streams[0]
        codec = vs.get("codec_name", "")
        width = vs.get("width", 0)
        height = vs.get("height", 0)

        # FPS: can be r_frame_rate like "30000/1001" or avg_frame_rate
        fps_str = vs.get("r_frame_rate", vs.get("avg_frame_rate", "0/1"))
        try:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

        duration = float(fmt.get("duration", 0))
        creation_time = None
        tags = fmt.get("tags", {})
        for key in ("creation_time", "com.apple.quicktime.creationdate"):
            if key in tags:
                creation_time = tags[key]
                break

        bitrate = int(fmt.get("bit_rate", 0))

        return {
            "codec": codec,
            "width": width,
            "height": height,
            "fps": round(fps, 2),
            "duration": duration,
            "creation_time": creation_time,
            "bitrate": bitrate,
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


def _metadata_similarity(meta1: Dict, meta2: Dict) -> float:
    """Calculate similarity score (0.0–1.0) between two video metadata dicts."""
    score = 0.0
    weights = 0.0

    if meta1.get("codec") and meta2.get("codec"):
        if meta1["codec"] == meta2["codec"]:
            score += 1.0
        weights += 1.0

    if meta1.get("width") and meta2.get("width") and meta1.get("height") and meta2.get("height"):
        if meta1["width"] == meta2["width"] and meta1["height"] == meta2["height"]:
            score += 1.0
        weights += 1.0

    if meta1.get("fps") and meta2.get("fps") and meta1["fps"] > 0 and meta2["fps"] > 0:
        fps_diff = abs(meta1["fps"] - meta2["fps"])
        if fps_diff < 0.1:
            score += 1.0
        elif fps_diff < 1.0:
            score += 0.5
        weights += 1.0

    if weights == 0:
        return 0.0
    return score / weights


# ---------------------------------------------------------------------------
# Segment detection / grouping (rewritten)
# ---------------------------------------------------------------------------

def _scan_folder(folder: Path) -> List[Path]:
    """Return sorted list of MP4/MOV files in *folder* (non-recursive)."""
    if not folder.is_dir():
        raise NotADirectoryError(f"Kein Verzeichnis: {folder}")
    video_exts = {".mp4", ".mov"}
    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in video_exts
    )
    return files


def _try_match(filename: str) -> Optional[Tuple[str, int]]:
    """Try to extract (group_key, segment_number) from a filename.

    Uses the generic pattern: anything_XXXX.ext where XXXX is 3-6 digits.
    The group_key is everything before the final underscore+digits.
    """
    m = RE_SEGMENT.match(filename)
    if m:
        prefix = m.group(1).lower()
        seg_num = int(m.group(2))
        return (prefix, seg_num)
    return None


def _group_by_naming(files: List[Path]) -> Dict[str, List[Path]]:
    """Group files by their naming pattern (everything before trailing digits)."""
    groups: Dict[str, List[Tuple[int, Path]]] = defaultdict(list)

    for fp in files:
        key_seq = _try_match(fp.name)
        if key_seq is None:
            log.debug("Kein Segment-Muster: %s", fp.name)
            continue
        group_key, seq = key_seq
        groups[group_key].append((seq, fp))

    result: Dict[str, List[Path]] = {}
    for key, items in groups.items():
        items.sort(key=lambda x: x[0])
        if len(items) >= 2:
            result[key] = [p for _, p in items]

    return result


def _group_by_metadata(files: List[Path]) -> Dict[str, List[Path]]:
    """Fallback grouping: cluster files by metadata similarity.

    Used for files without trailing sequence numbers. Groups files
    that share the same codec, resolution, and frame rate.
    """
    # Extract metadata for all files
    file_meta: Dict[Path, Dict] = {}
    for fp in files:
        meta = _get_video_metadata(fp)
        if meta:
            file_meta[fp] = meta

    if len(file_meta) < 2:
        return {}

    # Build similarity matrix
    remaining = set(file_meta.keys())
    clusters: List[List[Path]] = []

    while remaining:
        seed = remaining.pop()
        cluster = [seed]

        # Find all files similar to seed
        for other in list(remaining):
            sim = _metadata_similarity(file_meta[seed], file_meta[other])
            if sim >= 0.9:  # High threshold for metadata-only grouping
                cluster.append(other)
                remaining.discard(other)

        clusters.append(cluster)

    # Build result dict, sort clusters by creation_time
    result: Dict[str, List[Path]] = {}
    for i, cluster in enumerate(clusters):
        if len(cluster) < 2:
            continue

        # Sort by creation_time if available, else by filename
        def _sort_key(p: Path):
            meta = file_meta.get(p, {})
            ct = meta.get("creation_time")
            if ct:
                return ct
            return p.name

        cluster.sort(key=_sort_key)

        # Generate a group key from metadata
        sample = file_meta.get(cluster[0], {})
        key = f"meta_group_{i+1:02d}_"
        key += f"{sample.get('width', 0)}x{sample.get('height', 0)}_"
        key += f"{sample.get('codec', '?')}"
        result[key] = cluster

    return result


def _validate_group_with_metadata(
    group_key: str,
    segments: List[Path],
) -> Tuple[bool, List[str]]:
    """Validate that segments in a group belong together using ffprobe metadata.

    Returns (is_valid, list_of_warnings).
    Checks: same codec, same resolution, same/close fps.
    """
    warnings: List[str] = []
    if len(segments) < 2:
        return True, warnings

    metas = []
    for seg in segments:
        meta = _get_video_metadata(seg)
        if meta:
            metas.append(meta)
        else:
            warnings.append(f"Keine Metadaten für {seg.name} – überspringe Validierung")

    if len(metas) < 2:
        return True, warnings

    ref = metas[0]
    for i, m in enumerate(metas[1:], 1):
        seg_name = segments[i].name
        if m.get("codec") and ref.get("codec") and m["codec"] != ref["codec"]:
            warnings.append(
                f"Codec-Unterschied in {seg_name}: "
                f"{m['codec']} ≠ {ref['codec']} (Referenz)"
            )
        if m.get("width") and ref.get("width") and m["width"] != ref["width"]:
            warnings.append(
                f"Auflösungs-Unterschied in {seg_name}: "
                f"{m['width']}x{m['height']} ≠ {ref['width']}x{ref['height']}"
            )
        if m.get("fps") and ref.get("fps") and abs(m["fps"] - ref["fps"]) > 1.0:
            warnings.append(
                f"FPS-Unterschied in {seg_name}: "
                f"{m['fps']:.2f} ≠ {ref['fps']:.2f}"
            )

    return len(warnings) == 0, warnings


def _group_segments(
    files: List[Path],
    validate_metadata: bool = True,
) -> Tuple[Dict[str, List[Path]], Dict[str, List[str]]]:
    """Group a list of video files into segment groups.

    Primary method: naming pattern (anything_digits.ext)
    Fallback: metadata similarity clustering

    Returns (groups, warnings_dict).
    Only groups with 2+ segments are returned.
    """
    groups = _group_by_naming(files)

    # Log unmatched files and try metadata fallback
    matched_paths = set()
    for segs in groups.values():
        for s in segs:
            matched_paths.add(s)

    unmatched = [f for f in files if f not in matched_paths]
    if unmatched:
        log.debug("%d Dateien ohne Namensmuster – versuche Metadaten-Fallback", len(unmatched))
        meta_groups = _group_by_metadata(unmatched)
        if meta_groups:
            log.info("Metadaten-Fallback: %d zusätzliche Gruppen gefunden", len(meta_groups))
            groups.update(meta_groups)

    all_warnings: Dict[str, List[str]] = {}

    for key, segs in groups.items():
        if validate_metadata:
            valid, warns = _validate_group_with_metadata(key, segs)
            if warns:
                all_warnings[key] = warns
                for w in warns:
                    log.warning("  ⚠ %s", w)

    return groups, all_warnings


# ---------------------------------------------------------------------------
# Color Correction Engine
# ---------------------------------------------------------------------------

def _extract_frame(
    video_path: Path,
    output_path: Path,
    frame_number: str = "first",
    ffmpeg_bin: str = "ffmpeg",
) -> bool:
    """Extract a single frame from a video.

    Args:
        video_path: Source video file
        output_path: Where to save the PNG frame
        frame_number: 'first' for frame 0, 'last' for final frame
        ffmpeg_bin: Path to ffmpeg

    Returns True on success.
    """
    if frame_number == "last":
        # Need to get total frames first
        vf = r"select=eq(n\,0)"
        # Use a different approach: seek to near end and grab last frame
        # More reliable: use ffprobe to get nb_frames, then use select
        try:
            fb = shutil.which("ffprobe") or "ffprobe"
            result = subprocess.run(
                [fb, "-v", "quiet", "-select_streams", "v:0",
                 "-count_packets", "-show_entries", "stream=nb_read_packets",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, timeout=15,
            )
            nb_frames = int(result.stdout.strip()) if result.stdout.strip().isdigit() else None
        except Exception:
            nb_frames = None

        if nb_frames and nb_frames > 1:
            last_idx = nb_frames - 1
            vf = f"select=eq(n\\,{last_idx})"
        else:
            # Fallback: seek to last second and grab
            vf = "select=eq(n\\,0)"
            # We'll use a different approach below
    else:
        vf = "select=eq(n\\,0)"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-vframes", "1",
        "-q:v", "2",
        str(output_path),
    ]

    # For last frame without frame count: seek approach
    if frame_number == "last" and (not nb_frames or nb_frames <= 1):
        # Seek to 95% of duration
        try:
            result = subprocess.run(
                [ffmpeg_bin, "-i", str(video_path)],
                capture_output=True, text=True,
            )
            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", result.stderr)
            if dur_match:
                h, m, s, cs = map(int, dur_match.groups())
                total_secs = h * 3600 + m * 60 + s + cs / 100.0
                seek_time = max(0, total_secs * 0.95)
                cmd = [
                    ffmpeg_bin, "-y",
                    "-ss", str(seek_time),
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-q:v", "2",
                    str(output_path),
                ]
        except Exception:
            pass

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and output_path.exists()
    except Exception as e:
        log.debug("Frame extraction failed: %s", e)
        return False


def _get_frame_histogram(frame_path: Path, ffmpeg_bin: str = "ffmpeg") -> Optional[Dict[str, List[int]]]:
    """Extract RGB histogram from an image using ffmpeg's signalstats.

    Uses ffmpeg to compute per-channel statistics, then builds histograms
    from the output. Returns dict with 'r', 'g', 'b' lists of 256 values,
    or None on failure.
    """
    # Use ffmpeg to get per-channel min/max/mean from signalstats
    try:
        result = subprocess.run(
            [
                ffmpeg_bin, "-i", str(frame_path),
                "-vf", "signalstats",
                "-f", "null", "-"
            ],
            capture_output=True, text=True, timeout=15,
        )
        # signalstats outputs per-frame stats on stderr
        # We need a more direct approach: use histogram filter
    except Exception:
        pass

    # Alternative: use ffprobe/ffmpeg to compute per-channel statistics
    # Most portable: use ffmpeg histogram filter output
    try:
        result = subprocess.run(
            [
                ffmpeg_bin, "-i", str(frame_path),
                "-vf", "format=rgb24,histogram=display_mode=0:levels_mode=linear:level_height=50",
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24",
                "pipe:1"
            ],
            capture_output=True, timeout=15,
        )
    except Exception:
        return None

    # Simpler approach: get mean RGB values via ffmpeg signalstats on the frame
    try:
        result = subprocess.run(
            [
                ffmpeg_bin, "-i", str(frame_path),
                "-vf", "signalstats=stat=all", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=15,
        )

        # Parse signalstats output from stderr
        stats = {"r": {"min": 0, "max": 255, "mean": 128},
                  "g": {"min": 0, "max": 255, "mean": 128},
                  "b": {"min": 0, "max": 255, "mean": 128}}

        for line in result.stderr.split("\n"):
            for channel in ["Y", "U", "V"]:
                m = re.search(rf"{channel}MIN:\s*(\d+)", line)
                if m:
                    # YUV to RGB approximation not needed here
                    pass
                m = re.search(rf"{channel}AVG:\s*(\d+(?:\.\d+)?)", line)
                if m:
                    pass

        # Instead: use a more direct method - get raw pixel data and compute ourselves
    except Exception:
        pass

    # Most reliable approach: use ffmpeg to output raw RGB24 and compute histogram in Python
    try:
        result = subprocess.run(
            [
                ffmpeg_bin, "-i", str(frame_path),
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-vframes", "1", "pipe:1",
            ],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0 or len(result.stdout) < 3:
            return None

        raw = result.stdout
        hist_r = [0] * 256
        hist_g = [0] * 256
        hist_b = [0] * 256

        # RGB24: 3 bytes per pixel (R, G, B)
        pixel_count = len(raw) // 3
        if pixel_count == 0:
            return None

        # Sample for speed (every 4th pixel for large frames)
        step = max(1, pixel_count // 10000)
        for i in range(0, len(raw), 3 * step):
            if i + 2 < len(raw):
                r = raw[i]
                g = raw[i + 1]
                b = raw[i + 2]
                hist_r[r] += 1
                hist_g[g] += 1
                hist_b[b] += 1

        # Normalize to float
        total = max(1, sum(hist_r))
        for i in range(256):
            hist_r[i] = hist_r[i] / total
            hist_g[i] = hist_g[i] / total
            hist_b[i] = hist_b[i] / total

        return {"r": hist_r, "g": hist_g, "b": hist_b}
    except Exception as e:
        log.debug("Histogram computation failed: %s", e)
        return None


def _compute_rgb_means(frame_path: Path, ffmpeg_bin: str = "ffmpeg") -> Optional[Tuple[float, float, float]]:
    """Compute mean RGB values for a frame.

    Uses ffmpeg to output raw pixel data, computes channel means.
    Returns (mean_r, mean_g, mean_b) each 0.0–1.0, or None on failure.
    """
    try:
        result = subprocess.run(
            [
                ffmpeg_bin, "-i", str(frame_path),
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-vframes", "1", "pipe:1",
            ],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0 or len(result.stdout) < 3:
            return None

        raw = result.stdout
        total_r = 0.0
        total_g = 0.0
        total_b = 0.0
        count = 0

        # Sample every 10th pixel for speed
        pixel_count = len(raw) // 3
        step = max(1, pixel_count // 5000)
        for i in range(0, len(raw), 3 * step):
            if i + 2 < len(raw):
                total_r += raw[i]
                total_g += raw[i + 1]
                total_b += raw[i + 2]
                count += 1

        if count == 0:
            return None

        return (
            (total_r / count) / 255.0,
            (total_g / count) / 255.0,
            (total_b / count) / 255.0,
        )
    except Exception:
        return None


def _histogram_distance(h1: List[float], h2: List[float]) -> float:
    """Compute earth mover's distance (simple) between two normalized histograms.

    Returns value 0.0 (identical) to 1.0 (completely different).
    """
    if len(h1) != len(h2) or len(h1) != 256:
        return 0.0

    diff = 0.0
    for i in range(256):
        diff += abs(h1[i] - h2[i])
    return diff / 2.0  # Normalize to 0–1


def _analyze_color_jump(
    seg_a: Path,
    seg_b: Path,
    ffmpeg_bin: str = "ffmpeg",
    tmpdir: Optional[Path] = None,
) -> Tuple[bool, float, Optional[Dict]]:
    """Analyze if there's a significant color jump between two segments.

    Extracts last frame of seg_a and first frame of seg_b, computes
    histogram distance per channel.

    Returns (has_jump, max_distance, correction_params).
    correction_params is None if no jump detected, else contains
    RGB gain adjustments to match seg_a.
    """
    if tmpdir is None:
        tmpdir = Path(tempfile.mkdtemp(prefix="flipsisitch_"))

    last_frame = tmpdir / f"last_{seg_a.stem}.png"
    first_frame = tmpdir / f"first_{seg_b.stem}.png"

    if not _extract_frame(seg_a, last_frame, "last", ffmpeg_bin):
        log.warning("Konnte letztes Frame von %s nicht extrahieren", seg_a.name)
        return False, 0.0, None

    if not _extract_frame(seg_b, first_frame, "first", ffmpeg_bin):
        log.warning("Konnte erstes Frame von %s nicht extrahieren", seg_b.name)
        return False, 0.0, None

    # Compute histograms
    hist_a = _get_frame_histogram(last_frame, ffmpeg_bin)
    hist_b = _get_frame_histogram(first_frame, ffmpeg_bin)

    # Compute RGB means
    means_a = _compute_rgb_means(last_frame, ffmpeg_bin)
    means_b = _compute_rgb_means(first_frame, ffmpeg_bin)

    # Cleanup frames
    try:
        last_frame.unlink(missing_ok=True)
        first_frame.unlink(missing_ok=True)
    except OSError:
        pass

    if hist_a and hist_b:
        dr = _histogram_distance(hist_a["r"], hist_b["r"])
        dg = _histogram_distance(hist_a["g"], hist_b["g"])
        db = _histogram_distance(hist_a["b"], hist_b["b"])
        max_dist = max(dr, dg, db)
    elif means_a and means_b:
        # Fallback: use mean RGB difference
        dr = abs(means_a[0] - means_b[0])
        dg = abs(means_a[1] - means_b[1])
        db = abs(means_a[2] - means_b[2])
        max_dist = max(dr, dg, db)
    else:
        return False, 0.0, None

    threshold = _config["color_threshold"]

    if max_dist < threshold:
        return False, max_dist, None

    # Build correction parameters
    correction = {}
    if means_a and means_b:
        # Gain factors: what to multiply seg_b by to match seg_a
        # Clamp to reasonable range
        if means_a[0] > 0.01 and means_b[0] > 0.01:
            correction["r_gain"] = min(2.0, max(0.5, means_a[0] / means_b[0]))
        if means_a[1] > 0.01 and means_b[1] > 0.01:
            correction["g_gain"] = min(2.0, max(0.5, means_a[1] / means_b[1]))
        if means_a[2] > 0.01 and means_b[2] > 0.01:
            correction["b_gain"] = min(2.0, max(0.5, means_a[2] / means_b[2]))

    return True, max_dist, correction


def _generate_color_correction_filter(correction: Dict, ffmpeg_bin: str = "ffmpeg") -> str:
    """Generate an ffmpeg filter string for color correction.

    Uses colorbalance filter to adjust shadows, midtones, highlights
    based on gain correction values.
    """
    if not correction:
        return ""

    # For each channel, compute colorbalance adjustments
    # colorbalance takes rs, gs, bs (shadows) and rh, gh, bh (highlights)
    # We use midtones as primary adjustment
    parts = []

    # Map gains to colorbalance parameters
    # Default: rs=0, gs=0, bs=0, rh=0, gh=0, bh=0
    # Positive = more of that color, negative = less
    # Gain 1.0 → 0, Gain 1.2 → +0.2, Gain 0.8 → -0.2
    rm = (correction.get("r_gain", 1.0) - 1.0) * 1.5
    gm = (correction.get("g_gain", 1.0) - 1.0) * 1.5
    bm = (correction.get("b_gain", 1.0) - 1.0) * 1.5

    # Clamp
    rm = max(-1.0, min(1.0, rm))
    gm = max(-1.0, min(1.0, gm))
    bm = max(-1.0, min(1.0, bm))

    # Apply to both midtones and highlights for natural look
    parts.append(f"colorbalance=rm={rm:.3f}:gm={gm:.3f}:bm={bm:.3f}:"
                 f"rh={rm*0.5:.3f}:gh={gm*0.5:.3f}:bh={bm*0.5:.3f}:"
                 f"rs={rm*0.3:.3f}:gs={gm*0.3:.3f}:bs={bm*0.3:.3f}")

    return ",".join(parts)


def _analyze_all_transitions(
    segments: List[Path],
    ffmpeg_bin: str = "ffmpeg",
    tmpdir: Optional[Path] = None,
) -> List[Optional[Dict]]:
    """Analyze color jumps at all transitions between consecutive segments.

    Returns list of correction dicts (one per transition, None if no jump).
    corrections[i] = correction to apply to segment i+1.
    """
    if len(segments) < 2:
        return []

    if tmpdir is None:
        tmpdir = Path(tempfile.mkdtemp(prefix="flipsisitch_cctemp_"))

    corrections: List[Optional[Dict]] = []
    total = len(segments) - 1

    for i in range(total):
        _sse_push({"type": "color_analysis", "current": i + 1, "total": total,
                    "seg_a": segments[i].name, "seg_b": segments[i + 1].name})
        has_jump, dist, corr = _analyze_color_jump(
            segments[i], segments[i + 1], ffmpeg_bin, tmpdir
        )
        if has_jump and corr:
            log.info("  Farbsprung erkannt: %s → %s (Distanz: %.4f)",
                     segments[i].name, segments[i + 1].name, dist)
            corrections.append(corr)
        else:
            corrections.append(None)

    # Cleanup tmpdir
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except OSError:
        pass

    return corrections


# ---------------------------------------------------------------------------
# Merging (extended with color correction)
# ---------------------------------------------------------------------------

def _output_path(
    group_key: str,
    segments: List[Path],
    output_dir: Optional[Path],
    suffix_mode: bool,
) -> Path:
    """Determine the output file path for a group."""
    base_name = segments[0].stem
    ext = segments[0].suffix.lower()

    cleaned = re.sub(r"[-_]?\d{3,6}$", "", base_name)
    if not cleaned:
        cleaned = "merged"

    if output_dir:
        out_dir = output_dir
        out_name = f"{cleaned}{ext}"
    elif suffix_mode:
        out_dir = segments[0].parent
        out_name = f"{cleaned}_merged{ext}"
    else:
        out_dir = segments[0].parent / "merged"
        out_name = f"{cleaned}{ext}"

    _ensure_dir(out_dir)
    return out_dir / out_name


def _merge_group(
    group_key: str,
    segments: List[Path],
    ffmpeg_bin: str,
    output_dir: Optional[Path],
    suffix_mode: bool,
    overwrite: bool,
    color_correct: bool = False,
    color_profile: Optional[str] = None,
    codec: Optional[str] = None,
    hwaccel: Optional[str] = None,
    tmpdir: Optional[Path] = None,
) -> bool:
    """Merge a single group of segments, optionally with color correction.

    Returns True on success, False on failure.
    """
    if len(segments) < 2:
        log.error("Gruppe muss mindestens 2 Segmente haben.")
        return False

    out_path = _output_path(group_key, segments, output_dir, suffix_mode)

    if out_path.exists() and not overwrite:
        log.error(
            "Ausgabedatei existiert bereits: %s\n"
            "Verwende --overwrite zum Überschreiben.",
            out_path,
        )
        return False

    # Validate segments
    valid_segments: List[Path] = []
    for seg in segments:
        try:
            if seg.stat().st_size == 0:
                log.warning("Leere Datei übersprungen: %s", seg.name)
            else:
                valid_segments.append(seg)
        except OSError as exc:
            log.warning("Datei nicht lesbar, übersprungen: %s (%s)", seg.name, exc)

    if len(valid_segments) < 2:
        log.error("Nach dem Entfernen leerer Dateien sind <2 Segmente übrig.")
        return False

    total_size = sum(s.stat().st_size for s in valid_segments)
    log.info("Füge %d Segmente zusammen → %s", len(valid_segments), out_path.name)
    log.info("  Gesamtgröße (ungefähr): %s", _human_size(total_size))

    # ── Resolve color profile ─────────────────────────────────────
    cp = color_profile or _config.get("color_profile", "none")
    need_reencode = cp != "none"

    # ── Resolve codec ─────────────────────────────────────────────
    use_codec = codec or _config.get("codec", "hevc")
    if use_codec == "copy" and need_reencode:
        log.info(
            "  Farbprofil aktiv – erzwinge Codec hevc statt copy",
        )
        use_codec = "hevc"

    # ── Resolve hardware acceleration ─────────────────────────────
    use_hwaccel = hwaccel or _config.get("hwaccel", "auto")
    best_gpu = _get_best_gpu_encoder(use_hwaccel)

    # ── Detect D-Log M ────────────────────────────────────────────
    is_dlog_m = False
    if cp in ("dlogm", "both"):
        ffprobe_bin = _find_ffprobe(ffmpeg_bin=ffmpeg_bin)
        log.info("  Prüfe auf D-Log M Farbprofil …")
        is_dlog_m = _detect_dlog_m(valid_segments[0], ffprobe_bin)
        if is_dlog_m:
            log.info("  ✅ D-Log M erkannt – konvertiere zu Rec.709")
        else:
            log.info(
                "  ⚠️ Video ist nicht D-Log M – "
                "D-Log M Konvertierung wird übersprungen"
            )

    # ── Determine encoding strategy ───────────────────────────────
    # Check also for legacy color_correct (segment-transition correction)
    color_corrections = []
    if color_correct and not need_reencode:
        log.info("  Analysiere Farbsprünge zwischen Segmenten …")
        if tmpdir is None:
            tmpdir = Path(tempfile.mkdtemp(prefix="flipsisitch_"))
        color_corrections = _analyze_all_transitions(
            valid_segments, ffmpeg_bin, tmpdir
        )

    has_color_correction = any(
        c is not None for c in color_corrections
    )
    need_reencode = need_reencode or has_color_correction

    if not need_reencode:
        # No re-encoding needed: lossless concat
        return _merge_concat(valid_segments, ffmpeg_bin, out_path, overwrite)

    # ── Build color filter chain ──────────────────────────────────
    color_filter = ""
    if cp != "none":
        color_filter = _get_color_filter_chain(cp, is_dlog_m)

    # ── Merge with re-encoding ────────────────────────────────────
    return _merge_with_reencoding(
        segments=valid_segments,
        ffmpeg_bin=ffmpeg_bin,
        out_path=out_path,
        overwrite=overwrite,
        color_filter=color_filter,
        corrections=color_corrections if color_correct else [],
        codec=use_codec,
        hwaccel=best_gpu,
    )


def _merge_concat(
    segments: List[Path],
    ffmpeg_bin: str,
    out_path: Path,
    overwrite: bool,
) -> bool:
    """Standard lossless concat merge (no re-encoding)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="ascii"
    ) as tmp:
        concat_file = Path(tmp.name)
        for seg in segments:
            escaped = str(seg.resolve()).replace("'", "'\\''")
            tmp.write(f"file '{escaped}'\n")

    try:
        cmd = [
            ffmpeg_bin,
            "-y" if overwrite else "-n",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-map", "0",
            str(out_path),
        ]
        log.debug("ffmpeg: %s", " ".join(shlex.quote(p) for p in cmd))

        proc = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, text=True, universal_newlines=True,
        )
        assert proc.stderr is not None

        # Parse ffmpeg progress from stderr
        for line in proc.stderr:
            # Extract progress (time=)
            m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
            if m:
                h, mi, s, cs = map(int, m.groups())
                current_s = h * 3600 + mi * 60 + s + cs / 100.0
                _sse_push({"type": "progress", "time": current_s})

        proc.wait()
        if proc.returncode != 0:
            log.error("ffmpeg-Fehler beim Zusammenfügen.")
            return False

        log.info("  ✔ Fertig: %s  (%s)", out_path, _human_size(out_path.stat().st_size))
        return True

    finally:
        try:
            concat_file.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_with_reencoding(
    segments: List[Path],
    ffmpeg_bin: str,
    out_path: Path,
    overwrite: bool,
    color_filter: str,
    corrections: List[Optional[Dict]],
    codec: str,
    hwaccel: str,
) -> bool:
    """Merge segments with re-encoding (color profile, codec, or transition corrections).

    Strategy:
    1. Build a concat file with all segments
    2. Apply color filter + encoder in one pass
    3. Use GPU encoder if available, fallback to CPU on failure
    """
    log.info("  Re-Encode: Codec=%s, GPU=%s, Color=%s",
             codec, hwaccel,
             color_filter if color_filter else "none")

    if color_filter:
        log.info("  Farbfilter: %s", color_filter)

    # Build concat file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="ascii"
    ) as tmp:
        concat_file = Path(tmp.name)
        for seg in segments:
            escaped = str(seg.resolve()).replace("'", "'\\''")
            tmp.write(f"file '{escaped}'\n")

    try:
        # Build encoder args
        is_hevc = codec == "hevc"
        encoder_args = _get_encoder_cmd(codec, hwaccel, is_hevc)

        # Build filter args
        vf_parts = []

        # If there are per-segment corrections, we need a more complex approach
        has_corrections = any(c is not None for c in corrections)

        if has_corrections:
            log.info(
                "  %d Segment-Übergangs-Korrekturen aktiv",
                sum(1 for c in corrections if c is not None),
            )

        if color_filter and has_corrections:
            # Both global color profile AND transition corrections
            # Apply color filter to all, corrections per segment
            vf_parts.append(color_filter)
            # Note: segment-level corrections require separate encoding passes
            # For now, we combine global filter with corrections
            for i, corr in enumerate(corrections):
                if corr is not None:
                    seg_filter = _generate_color_correction_filter(corr, ffmpeg_bin)
                    if seg_filter:
                        vf_parts.append(seg_filter)
        elif color_filter:
            vf_parts.append(color_filter)
        elif has_corrections:
            for i, corr in enumerate(corrections):
                if corr is not None:
                    seg_filter = _generate_color_correction_filter(corr, ffmpeg_bin)
                    if seg_filter:
                        vf_parts.append(seg_filter)

        vf_filter = ",".join(vf_parts)

        # Build command
        cmd = [
            ffmpeg_bin,
            "-y" if overwrite else "-n",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
        ]

        if vf_filter:
            cmd += ["-vf", vf_filter]

        cmd += encoder_args
        cmd += ["-movflags", "+faststart", str(out_path)]

        log.debug("ffmpeg: %s", " ".join(shlex.quote(p) for p in cmd))

        # First attempt: with GPU/hwaccel
        try:
            proc = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, text=True,
                universal_newlines=True,
            )
            assert proc.stderr is not None

            stderr_lines = []
            for line in proc.stderr:
                stderr_lines.append(line)
                # Parse progress
                m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
                if m:
                    h, mi, s, cs = map(int, m.groups())
                    current_s = h * 3600 + mi * 60 + s + cs / 100.0
                    _sse_push({"type": "progress", "time": current_s})

                m = re.search(r"speed=\s*([\d.]+)x", line)
                if m:
                    _sse_push({"type": "speed", "speed": float(m.group(1))})

            proc.wait()

            if proc.returncode == 0:
                log.info(
                    "  ✔ Fertig: %s  (%s)",
                    out_path,
                    _human_size(out_path.stat().st_size),
                )
                return True

            # Check if GPU encoder failed (try CPU fallback)
            stderr_text = "".join(stderr_lines).lower()
            if hwaccel != "cpu" and (
                "cannot" in stderr_text
                or "failed" in stderr_text
                or "error" in stderr_text
            ):
                log.warning(
                    "GPU-Encoder '%s' fehlgeschlagen, versuche CPU-Fallback …",
                    hwaccel,
                )
                return _merge_with_reencoding(
                    segments=segments,
                    ffmpeg_bin=ffmpeg_bin,
                    out_path=out_path,
                    overwrite=overwrite,
                    color_filter=color_filter,
                    corrections=corrections,
                    codec=codec,
                    hwaccel="cpu",
                )

            log.error("ffmpeg-Fehler beim Re-Encoding (RC=%d)", proc.returncode)
            return False

        except subprocess.TimeoutExpired:
            log.error("ffmpeg Timeout beim Re-Encoding")
            return False

    finally:
        try:
            concat_file.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Web-UI Server
# ---------------------------------------------------------------------------

WEB_CSS = """
:root {
    --bg: #0d1117;
    --bg2: #161b22;
    --bg3: #1c2128;
    --surface: #21262d;
    --border: #30363d;
    --text: #e6edf3;
    --text2: #8b949e;
    --accent: #f5a623;
    --accent2: #d48d0c;
    --accent-dim: rgba(245,166,35,0.15);
    --cyan: #00d4ff;
    --cyan-dim: rgba(0,212,255,0.12);
    --success: #3fb950;
    --danger: #f85149;
    --warn: #d29922;
    --radius: 10px;
    --radius-sm: 6px;
    --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif;
    --mono: 'SF Mono', 'Cascadia Code', 'Consolas', 'Fira Code', monospace;
    --shadow: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.2);
    --shadow-lg: 0 8px 32px rgba(0,0,0,0.4);
    --glass-bg: rgba(22,27,34,0.85);
    --glass-border: rgba(255,255,255,0.08);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
    background-image:
        radial-gradient(ellipse at 50% 0%, rgba(245,166,35,0.04) 0%, transparent 60%),
        radial-gradient(ellipse at 80% 20%, rgba(0,212,255,0.03) 0%, transparent 50%);
}

/* Header */
header {
    background: var(--glass-bg);
    backdrop-filter: blur(16px) saturate(140%);
    -webkit-backdrop-filter: blur(16px) saturate(140%);
    border-bottom: 1px solid var(--glass-border);
    padding: 14px 28px;
    display: flex;
    align-items: center;
    gap: 14px;
    position: sticky;
    top: 0;
    z-index: 100;
}
header .logo-group { display: flex; align-items: center; gap: 10px; }
header .logo-icon { font-size: 1.6em; }
header .logo-text { font-size: 1.3em; font-weight: 700; color: var(--accent); letter-spacing: -0.3px; }
header .version { font-size: 0.78em; color: var(--text2); margin-left: auto; display: flex; align-items: center; gap: 8px; }
header .update-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--text2);
    transition: background 0.3s;
}
header .update-dot.available { background: var(--success); animation: pulse-dot 2s infinite; }
header .update-dot.error { background: var(--danger); }
@keyframes pulse-dot {
    0%, 100% { box-shadow: 0 0 0 0 rgba(63,185,80,0.4); }
    50% { box-shadow: 0 0 0 6px rgba(63,185,80,0); }
}

/* Main layout */
main { max-width: 960px; margin: 0 auto; padding: 28px 20px; }

/* Glass card */
.card {
    background: var(--glass-bg);
    backdrop-filter: blur(12px) saturate(120%);
    -webkit-backdrop-filter: blur(12px) saturate(120%);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius);
    padding: 22px;
    margin-bottom: 18px;
    box-shadow: var(--shadow);
    transition: border-color 0.2s, transform 0.15s;
}
.card:hover { border-color: rgba(255,255,255,0.12); }
.card h2 {
    font-size: 1.05em;
    margin-bottom: 14px;
    color: var(--accent);
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
}
.card h2 .icon { font-size: 1.1em; }

/* Drop Zone */
.drop-zone {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 36px 24px;
    text-align: center;
    cursor: pointer;
    transition: all 0.25s;
    background: rgba(255,255,255,0.01);
}
.drop-zone:hover, .drop-zone.drag-over {
    border-color: var(--accent);
    background: var(--accent-dim);
    transform: scale(1.01);
}
.drop-zone .dz-icon { font-size: 2.5em; margin-bottom: 8px; }
.drop-zone .dz-text { color: var(--text2); font-size: 0.9em; }
.drop-zone .dz-hint { color: var(--text2); font-size: 0.78em; margin-top: 6px; }

/* Form elements */
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
.row.gap { gap: 16px; }
.spacer { flex: 1; }

input, select, button {
    font-family: var(--font);
    font-size: 0.92em;
    border-radius: var(--radius-sm);
    padding: 9px 14px;
    border: 1px solid var(--border);
    background: var(--bg3);
    color: var(--text);
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
}
input:focus, select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-dim);
}
input[type="text"] { min-width: 200px; flex: 1; }
input[type="range"] { padding: 0; border: none; background: transparent; accent-color: var(--accent); }
input[type="checkbox"] { accent-color: var(--accent); width: 18px; height: 18px; }

button {
    cursor: pointer;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    font-size: 0.83em;
    transition: all 0.2s;
    position: relative;
    overflow: hidden;
}
button:hover { filter: brightness(1.15); transform: translateY(-1px); }
button:active { transform: translateY(0); }
button:disabled { opacity: 0.45; cursor: not-allowed; filter: none; transform: none; }

.btn-primary { background: var(--accent); color: #0d1117; border-color: var(--accent); }
.btn-primary:hover { background: var(--accent2); }
.btn-cyan { background: var(--cyan); color: #0d1117; border-color: var(--cyan); }
.btn-cyan:hover { background: #00b8e0; }
.btn-secondary { background: var(--surface); color: var(--text); }
.btn-danger { background: var(--danger); color: #fff; border-color: var(--danger); }
.btn-success { background: var(--success); color: #fff; border-color: var(--success); }
.btn-ghost { background: transparent; border-color: var(--border); color: var(--text2); text-transform: none; font-weight: 400; }
.btn-ghost:hover { background: var(--bg3); color: var(--text); }

/* Settings panel (collapsible) */
.settings-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    color: var(--text2);
    font-size: 0.9em;
    user-select: none;
    margin-bottom: 8px;
}
.settings-toggle:hover { color: var(--text); }
.settings-toggle .arrow { transition: transform 0.2s; display: inline-block; }
.settings-toggle.open .arrow { transform: rotate(90deg); }
.settings-body { overflow: hidden; max-height: 0; transition: max-height 0.35s ease; }
.settings-body.open { max-height: 600px; }
.settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px;
    padding-top: 8px;
}
.settings-item label {
    display: block;
    font-size: 0.82em;
    color: var(--text2);
    margin-bottom: 5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.settings-item select { width: 100%; }

/* Tables */
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--text2); font-weight: 600; font-size: 0.82em; text-transform: uppercase; letter-spacing: 0.5px; }
td { font-size: 0.88em; }
tr { transition: background 0.15s; }
tr:hover td { background: rgba(255,255,255,0.02); }

.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.73em;
    font-weight: 600;
    letter-spacing: 0.3px;
}
.badge-ok { background: rgba(63,185,80,0.15); color: var(--success); }
.badge-warn { background: rgba(210,153,34,0.15); color: var(--warn); }
.badge-bad { background: rgba(248,81,73,0.15); color: var(--danger); }
.badge-info { background: rgba(0,212,255,0.12); color: var(--cyan); }

/* Progress */
.progress-container {
    background: var(--bg3);
    border-radius: var(--radius-sm);
    overflow: hidden;
    height: 26px;
    margin: 14px 0;
    border: 1px solid var(--border);
}
.progress-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--accent2), var(--accent));
    background-size: 200% 100%;
    animation: gradient-shift 2s linear infinite;
    width: 0%;
    transition: width 0.4s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.7em;
    font-weight: 700;
    color: #0d1117;
    min-width: 0;
}
@keyframes gradient-shift {
    0% { background-position: 0% 50%; }
    100% { background-position: 200% 50%; }
}

/* Log */
.log-area {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px;
    height: 220px;
    overflow-y: auto;
    font-family: var(--mono);
    font-size: 0.78em;
    color: var(--text2);
    white-space: pre-wrap;
    word-break: break-all;
    line-height: 1.55;
}
.log-area .info { color: var(--text); }
.log-area .warn { color: var(--warn); }
.log-area .error { color: var(--danger); }
.log-area .success { color: var(--success); }

/* Status */
.status-dot {
    display: inline-block;
    width: 9px; height: 9px;
    border-radius: 50%;
    margin-right: 8px;
}
.status-idle { background: var(--text2); }
.status-working { background: var(--accent); animation: pulse 1s infinite; }
.status-done { background: var(--success); }
.status-error { background: var(--danger); }
@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
}

/* Results */
.result-card {
    border-left: 3px solid var(--success);
    padding: 12px 16px;
    margin-bottom: 8px;
    background: rgba(63,185,80,0.04);
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}
.result-card.fail { border-left-color: var(--danger); background: rgba(248,81,73,0.04); }
.result-name { font-weight: 600; color: var(--text); font-size: 0.9em; }
.result-path { font-size: 0.78em; color: var(--text2); margin-top: 2px; }
.result-size { font-size: 0.75em; color: var(--accent); margin-top: 2px; }

/* Hidden */
.hidden { display: none !important; }

/* Confirm Dialog */
.dialog-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.65);
    backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
}
.dialog {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 28px;
    max-width: 500px;
    width: 90%;
    box-shadow: var(--shadow-lg);
}
.dialog h3 { color: var(--accent); margin-bottom: 14px; }
.dialog p { color: var(--text2); font-size: 0.9em; margin-bottom: 20px; }
.dialog .row { justify-content: flex-end; }

/* Misc */
.gpu-indicator { font-size: 0.78em; color: var(--cyan); display: flex; align-items: center; gap: 5px; }

/* Animations */
@keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}
.card { animation: fadeIn 0.3s ease; }
"""

WEB_JS = """
const state = {
  folder: '', output: '', scanning: false, merging: false,
  port: 8420, ffmpegReady: null,
  groups: {},
};

function $ (id) { return document.getElementById(id); }

// ── Init ──────────────────────────────
async function init () {
  state.port = parseInt(window.location.port || 8420);

  // Folder input: Enter key = scan
  $('import-folder').addEventListener('keydown', e => {
    if (e.key === 'Enter') doScan();
  });

  // Drag & Drop on drop zone
  setupDropZone();

  // Check ffmpeg + update status on load
  await checkFfmpegStatus();
  await checkUpdateStatus();

  // Keyboard shortcuts
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !state.merging && !state.scanning) {
      if (document.activeElement === $('import-folder')) return;
      e.preventDefault();
      doMerge();
    }
    if (e.key === 'Escape' && state.merging) {
      // Cancel not implemented server-side, but visually reset
    }
  });

  // Settings toggle
  $('settings-toggle').addEventListener('click', toggleSettings);
}

// ── Drag & Drop ──────────────────────
function setupDropZone () {
  const dz = $('drop-zone');
  const input = $('import-folder');

  dz.addEventListener('click', () => {
    // On desktop we can't access filesystem directly, prompt user
    input.focus();
  });

  dz.addEventListener('dragover', e => {
    e.preventDefault();
    dz.classList.add('drag-over');
  });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => {
    e.preventDefault();
    dz.classList.remove('drag-over');

    // Try to extract folder path from dropped items
    const items = e.dataTransfer.items;
    if (items && items.length > 0) {
      // Try DataTransferItemList for path
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file') {
          const file = item.getAsFile();
          if (file) {
            // Most browsers don't expose full path for security
            // We use the file name and let the server resolve
            input.value = file.name;
            // If File System Access API is available
            if (file.webkitRelativePath) {
              const dir = file.webkitRelativePath.split('/')[0];
              input.value = dir;
            }
            break;
          }
        }
      }
      // Trigger scan immediately
      setTimeout(() => doScan(), 200);
    }
  });
}

// ── ffmpeg Management ────────────────
async function checkFfmpegStatus () {
  try {
    const data = await api('/api/ffmpeg-status');
    state.ffmpegReady = data.available;
    if (data.available) {
      $('ffmpeg-card').classList.add('hidden');
      $('input-card').classList.remove('hidden');
      if (data.hwaccel) {
        $('gpu-info').textContent = data.hwaccel;
        $('gpu-info').classList.remove('hidden');
      }
      addLog('success', `ffmpeg bereit: ${data.ffmpeg_path}`);
    } else {
      $('ffmpeg-card').classList.remove('hidden');
      $('input-card').classList.add('hidden');
      $('ffmpeg-status-text').textContent = data.msg || 'ffmpeg nicht gefunden.';
      addLog('warn', 'ffmpeg nicht gefunden – bitte herunterladen.');
    }
  } catch (e) {
    $('ffmpeg-card').classList.remove('hidden');
    state.ffmpegReady = false;
    addLog('error', `ffmpeg-Prüfung fehlgeschlagen: ${e.message}`);
  }
}

async function downloadFfmpeg () {
  $('btn-download-ffmpeg').disabled = true;
  $('ffmpeg-status-text').textContent = 'Starte Download …';
  $('ffmpeg-progress-container').classList.remove('hidden');
  startSSE();

  try {
    await api('/api/download-ffmpeg', { method: 'POST' });
  } catch (e) {
    addLog('error', `Download-Fehler: ${e.message}`);
    $('btn-download-ffmpeg').disabled = false;
    $('ffmpeg-status-text').textContent = 'Fehler beim Download.';
    stopSSE();
  }
}

// ── Update Management ────────────────
async function checkUpdateStatus () {
  try {
    const data = await api('/api/check-update');
    if (data.update_available) {
      $('update-dot').classList.add('available');
      $('update-dot').title = 'Update verfügbar: ' + data.latest_version;
      $('update-btn').classList.remove('hidden');
      $('update-info').textContent = 'v' + data.latest_version + ' verfügbar';
      $('update-info').classList.remove('hidden');
    } else {
      $('update-dot').title = 'Aktuell: v' + data.current_version;
    }
  } catch (e) {
    console.log('Update check failed:', e);
  }
}

async function doUpdate () {
  if (!confirm('Update herunterladen und installieren?\nDie Anwendung wird danach neugestartet.')) return;

  $('update-btn').disabled = true;
  $('update-info').textContent = 'Lade Update …';
  addLog('info', 'Starte Update-Download …');

  try {
    const data = await api('/api/do-update', { method: 'POST' });
    if (data.success) {
      addLog('success', data.msg || 'Update erfolgreich!');
      $('update-info').textContent = '✅ Bitte neu starten';
    } else {
      addLog('error', data.msg || 'Update fehlgeschlagen');
      $('update-btn').disabled = false;
      $('update-info').textContent = '❌ Fehler';
    }
  } catch (e) {
    addLog('error', `Update-Fehler: ${e.message}`);
    $('update-btn').disabled = false;
  }
}

// ── API Helpers ──────────────────────
async function api (path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || res.statusText);
  }
  return res.json();
}

// ── Scanning ─────────────────────────
async function doScan () {
  const folderEl = $('import-folder');
  state.folder = folderEl.value.trim();
  if (!state.folder) { addLog('warn', 'Bitte einen Import-Ordner angeben.'); return; }

  if (state.ffmpegReady === false) {
    addLog('warn', 'ffmpeg fehlt – bitte zuerst herunterladen.');
    $('ffmpeg-card').classList.remove('hidden');
    $('ffmpeg-card').scrollIntoView({ behavior: 'smooth' });
    return;
  }

  state.scanning = true;
  setStatus('scanning', 'Scan läuft …');
  $('btn-scan').disabled = true;
  addLog('info', `Scanne: ${state.folder}`);

  try {
    const data = await api(`/api/scan?folder=${encodeURIComponent(state.folder)}`);
    if (data.need_ffmpeg) {
      state.ffmpegReady = false;
      $('ffmpeg-card').classList.remove('hidden');
      $('ffmpeg-status-text').textContent = 'ffmpeg nicht gefunden.';
      addLog('warn', 'ffmpeg fehlt – bitte herunterladen.');
      setStatus('error', 'ffmpeg nicht verfügbar');
      return;
    }

    state.groups = data.groups || {};
    renderGroups(data);

    if (!data.groups || Object.keys(data.groups).length === 0) {
      addLog('warn', 'Keine zusammenführbaren Gruppen gefunden.');
    } else {
      addLog('success', `${Object.keys(data.groups).length} Gruppe(n) gefunden.`);
    }
    setStatus('idle', '');
  } catch (e) {
    addLog('error', `Scan-Fehler: ${e.message}`);
    setStatus('error', 'Scan fehlgeschlagen');
  } finally {
    state.scanning = false;
    $('btn-scan').disabled = false;
  }
}

// ── Rendering ────────────────────────
function renderGroups (data) {
  const tbody = document.querySelector('#groups-table tbody');
  const container = $('groups-container');
  tbody.innerHTML = '';

  const groups = data.groups || {};
  const keys = Object.keys(groups);

  if (keys.length === 0) {
    container.classList.add('hidden');
    return;
  }
  container.classList.remove('hidden');

  for (const key of keys) {
    const segs = groups[key];
    const totalSize = segs.reduce((s, f) => s + (f.size || 0), 0);
    const firstMeta = segs[0];
    const resolution = firstMeta.resolution || '?';
    const codec = firstMeta.codec || '?';
    const duration = firstMeta.duration ? (firstMeta.duration * segs.length).toFixed(1) + 's' : '?';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${escHtml(key)}</strong></td>
      <td>${segs.length}</td>
      <td>${formatSize(totalSize)}</td>
      <td>${resolution}</td>
      <td><span class="badge badge-info">${codec}</span></td>
      <td>${duration}</td>
    `;
    tr.title = segs.map(s => s.name).join('\\n');
    tbody.appendChild(tr);
  }

  if (!$('output-folder').value && state.folder) {
    $('output-folder').value = state.folder + '/merged';
  }
}

// ── Settings ─────────────────────────
function toggleSettings () {
  const toggle = $('settings-toggle');
  toggle.classList.toggle('open');
  $('settings-body').classList.toggle('open');
}

// ── Merge (with confirm) ────────────
async function doMerge () {
  state.folder = $('import-folder').value.trim();
  state.output = $('output-folder').value.trim();
  if (!state.folder) { addLog('warn', 'Kein Import-Ordner. Bitte zuerst scannen.'); return; }
  if (!state.groups || Object.keys(state.groups).length === 0) {
    addLog('warn', 'Keine Gruppen zum Mergen. Bitte zuerst scannen.');
    return;
  }

  // Show confirm dialog
  showConfirmDialog();
}

function showConfirmDialog () {
  const groups = state.groups;
  const keys = Object.keys(groups);
  const totalSegs = keys.reduce((s, k) => s + groups[k].length, 0);
  const colorProfile = $('color-profile').value;
  const codec = $('codec-select').value;
  const hwaccel = $('hwaccel-select').value;

  let summary = `
    <b>${keys.length} Gruppe(n)</b> mit insgesamt <b>${totalSegs} Segmenten</b><br><br>
    <b>Farbprofil:</b> ${colorProfile}<br>
    <b>Codec:</b> ${codec.toUpperCase()}<br>
    <b>Hardware:</b> ${hwaccel}<br>
    <b>Ausgabe:</b> ${state.output || state.folder + '/merged'}
  `;

  $('confirm-body').innerHTML = summary;
  $('confirm-dialog').classList.remove('hidden');
}

function hideConfirmDialog () {
  $('confirm-dialog').classList.add('hidden');
}

async function executeMerge () {
  hideConfirmDialog();

  state.merging = true;
  setStatus('working', 'Merge läuft …');
  $('btn-merge').disabled = true;
  $('btn-scan').disabled = true;
  $('results-container').classList.add('hidden');
  $('results').innerHTML = '';
  $('progress-bar').style.width = '0%';
  $('progress-text').textContent = '0%';

  const colorProfile = $('color-profile').value;
  const codec = $('codec-select').value;
  const hwaccel = $('hwaccel-select').value;

  addLog('info', `Merge: Farbprofil=${colorProfile}, Codec=${codec}, GPU=${hwaccel}`);

  startSSE();

  try {
    const params = new URLSearchParams({
      folder: state.folder,
      output: state.output,
      color_profile: colorProfile,
      codec: codec,
      hwaccel: hwaccel,
      port: state.port.toString(),
    });
    const data = await api(`/api/merge?${params}`, { method: 'POST' });
    renderResults(data);
  } catch (e) {
    addLog('error', `Merge-Fehler: ${e.message}`);
    setStatus('error', 'Merge fehlgeschlagen');
  } finally {
    state.merging = false;
    $('btn-merge').disabled = false;
    $('btn-scan').disabled = false;
    stopSSE();
  }
}

// ── SSE ──────────────────────────────
let sseSource = null;

function startSSE () {
  stopSSE();
  sseSource = new EventSource('/api/progress');
  sseSource.onmessage = function (e) {
    try { handleSSE(JSON.parse(e.data)); } catch (ex) {}
  };
  sseSource.onerror = function () {};
}

function stopSSE () {
  if (sseSource) { sseSource.close(); sseSource = null; }
}

function handleSSE (msg) {
  switch (msg.type) {
    case 'progress':
      const pct = Math.min(100, Math.round((msg.time || 0) / (msg.total || 100) * 100));
      $('progress-bar').style.width = pct + '%';
      $('progress-text').textContent = pct + '%';
      break;
    case 'speed':
      $('speed-text').textContent = (msg.speed || 1).toFixed(1) + 'x';
      break;
    case 'log':
      addLog(msg.level || 'info', msg.msg);
      break;
    case 'group_start':
      $('status-text').textContent = `Verarbeite: ${msg.group} (${msg.current}/${msg.total})`;
      $('progress-bar').style.width = '0%';
      $('progress-text').textContent = '0%';
      break;
    case 'group_done':
      addLog('success', `✅ ${msg.group}`);
      break;
    case 'group_error':
      addLog('error', `❌ ${msg.group}: ${msg.error || 'Unbekannter Fehler'}`);
      break;
    case 'ffmpeg_download_start':
      $('ffmpeg-status-text').textContent = 'Lade ffmpeg herunter …';
      $('ffmpeg-progress-container').classList.remove('hidden');
      $('ffmpeg-progress-bar').style.width = '0%';
      $('ffmpeg-progress-text').textContent = '0%';
      break;
    case 'ffmpeg_download_progress':
      $('ffmpeg-progress-bar').style.width = (msg.percent || 0) + '%';
      $('ffmpeg-progress-text').textContent = (msg.percent || 0) + '%';
      break;
    case 'ffmpeg_download_done':
      $('ffmpeg-progress-bar').style.width = '100%';
      $('ffmpeg-status-text').textContent = 'ffmpeg bereit!';
      $('btn-download-ffmpeg').disabled = false;
      $('btn-download-ffmpeg').textContent = '✅ Installiert';
      break;
    case 'ffmpeg_download_complete':
      stopSSE();
      if (msg.success) {
        state.ffmpegReady = true;
        $('ffmpeg-card').classList.add('hidden');
        $('input-card').classList.remove('hidden');
        addLog('success', '✅ ffmpeg installiert!');
      } else {
        $('btn-download-ffmpeg').disabled = false;
        $('ffmpeg-status-text').textContent = 'Fehler: ' + (msg.msg || 'Unbekannt');
        addLog('error', '❌ ffmpeg-Download fehlgeschlagen');
      }
      break;
  }
}

// ── Results ──────────────────────────
function renderResults (data) {
  const container = $('results-container');
  const resultsDiv = $('results');
  container.classList.remove('hidden');
  resultsDiv.innerHTML = '';

  const results = data.results || [];
  const success = results.filter(r => r.success).length;
  const errors = results.filter(r => !r.success).length;

  for (const r of results) {
    const card = document.createElement('div');
    card.className = `result-card${r.success ? '' : ' fail'}`;
    card.innerHTML = `
      <div class="result-name">${r.success ? '✅' : '❌'} ${escHtml(r.group)}</div>
      <div class="result-path">${escHtml(r.path || r.error || '')}</div>
      ${r.size_before && r.size_after ? `<div class="result-size">Vorher: ${formatSize(r.size_before)} → Nachher: ${formatSize(r.size_after)}</div>` : ''}
    `;
    resultsDiv.appendChild(card);
  }

  const summary = `${results.length} Gruppen: ${success} ✅${errors > 0 ? ', ' + errors + ' ❌' : ''}`;
  addLog(errors > 0 ? 'warn' : 'success', summary);
  setStatus(errors > 0 ? 'error' : 'done', summary);
}

// ── Helpers ──────────────────────────
function setStatus (cls, text) {
  $('status-dot').className = 'status-dot status-' + cls;
  $('status-text').textContent = text;
}

function addLog (level, msg) {
  const area = $('log-area');
  const stamp = new Date().toLocaleTimeString();
  const entry = document.createElement('div');
  entry.className = level;
  entry.textContent = `[${stamp}] ${msg}`;
  area.appendChild(entry);
  area.scrollTop = area.scrollHeight;
}

function formatSize (bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}

function escHtml (s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}

// Init
window.addEventListener('DOMContentLoaded', init);
"""

WEB_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FlipsiStitch – Video Merge Tool</title>
<style>{css}</style>
</head>
<body>

<header>
  <div class="logo-group">
    <span class="logo-icon">🎬</span>
    <span class="logo-text">FlipsiStitch</span>
  </div>
  <span class="gpu-indicator hidden" id="gpu-info">⚡ GPU</span>
  <span class="version">
    <span class="update-dot" id="update-dot" title="Prüfe Updates …"></span>
    v{version}
    <button class="btn-ghost hidden" id="update-btn" onclick="doUpdate()" style="padding:4px 10px;font-size:0.72em;">⬆ Update</button>
    <span id="update-info" class="hidden" style="font-size:0.72em;color:var(--success);"></span>
  </span>
</header>

<main>
  <!-- ffmpeg Download Card -->
  <div class="card hidden" id="ffmpeg-card">
    <h2><span class="icon">🔧</span> ffmpeg Setup</h2>
    <p style="color:var(--text2); margin-bottom:12px; font-size:0.9em;">
      ffmpeg wird für das Zusammenfügen der Videos benötigt.
      Beim ersten Start einmalig herunterladen (ca. 80 MB).
    </p>
    <div class="row gap">
      <button class="btn-primary" id="btn-download-ffmpeg" onclick="downloadFfmpeg()">
        📥 ffmpeg herunterladen
      </button>
      <span id="ffmpeg-status-text" style="color:var(--text2); font-size:0.88em;"></span>
    </div>
    <div class="progress-container hidden" id="ffmpeg-progress-container">
      <div class="progress-bar" id="ffmpeg-progress-bar">
        <span id="ffmpeg-progress-text">0%</span>
      </div>
    </div>
    <p style="color:var(--text2); font-size:0.78em; margin-top:8px;">
      Manuelle Alternative: <a href="https://ffmpeg.org/download.html" target="_blank" style="color:var(--accent);">ffmpeg.org</a>
    </p>
  </div>

  <!-- Drag & Drop Zone -->
  <div class="card" id="input-card">
    <div class="drop-zone" id="drop-zone">
      <div class="dz-icon">📁</div>
      <div class="dz-text">DJI-Videoordner hier ablegen</div>
      <div class="dz-hint">oder Pfad eingeben & Enter drücken</div>
    </div>
    <div class="row" style="margin-top:12px;">
      <input type="text" id="import-folder" placeholder="Pfad zum Ordner mit DJI-Segmenten …">
      <button class="btn-primary" id="btn-scan" onclick="doScan()">🔍 Scannen</button>
    </div>
  </div>

  <!-- Output -->
  <div class="card">
    <h2><span class="icon">💾</span> Ausgabe</h2>
    <div class="row">
      <input type="text" id="output-folder" placeholder="Ausgabeordner (Standard: merged/)">
    </div>
  </div>

  <!-- Groups Table -->
  <div class="card hidden" id="groups-container">
    <h2><span class="icon">📋</span> Erkannte Gruppen</h2>
    <table id="groups-table">
      <thead>
        <tr>
          <th>Gruppe</th>
          <th>Segmente</th>
          <th>Größe</th>
          <th>Auflösung</th>
          <th>Codec</th>
          <th>Dauer</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

  <!-- Settings (collapsible) -->
  <div class="card">
    <div class="settings-toggle" id="settings-toggle">
      <span class="arrow">▶</span> ⚙️ Einstellungen
    </div>
    <div class="settings-body" id="settings-body">
      <div class="settings-grid">
        <div class="settings-item">
          <label>🎨 Farbprofil</label>
          <select id="color-profile">
            <option value="none">Keine (verlustfrei)</option>
            <option value="dlogm">D-Log M → Rec.709</option>
            <option value="whitebalance">Weißabgleich</option>
            <option value="both">D-Log M + Weißabgleich (empfohlen)</option>
          </select>
        </div>
        <div class="settings-item">
          <label>📹 Codec</label>
          <select id="codec-select">
            <option value="hevc">H.265 / HEVC</option>
            <option value="h264">H.264 / AVC</option>
            <option value="copy">Copy (verlustfrei)</option>
          </select>
        </div>
        <div class="settings-item">
          <label>⚡ Hardware-Beschleunigung</label>
          <select id="hwaccel-select">
            <option value="auto">Auto</option>
            <option value="nvenc">NVIDIA NVENC</option>
            <option value="amf">AMD AMF</option>
            <option value="qsv">Intel QSV</option>
            <option value="cpu">CPU (Software)</option>
          </select>
        </div>
      </div>
    </div>
  </div>

  <!-- Actions -->
  <div class="card">
    <h2><span class="icon">⚙️</span> Aktionen</h2>
    <div class="row gap">
      <button class="btn-success" id="btn-merge" onclick="doMerge()">▶️ Merge starten</button>
      <span id="status-dot" class="status-dot status-idle"></span>
      <span id="status-text" style="color:var(--text2); font-size:0.88em;"></span>
      <span class="spacer"></span>
      <span id="speed-text" style="color:var(--text2); font-size:0.78em;"></span>
    </div>
    <div class="progress-container">
      <div class="progress-bar" id="progress-bar">
        <span id="progress-text">0%</span>
      </div>
    </div>
  </div>

  <!-- Results -->
  <div class="hidden" id="results-container">
    <h2 style="color:var(--accent); margin-bottom:12px;">📊 Ergebnisse</h2>
    <div id="results"></div>
  </div>

  <!-- Log -->
  <div class="card">
    <h2><span class="icon">📜</span> Log</h2>
    <div class="log-area" id="log-area"></div>
  </div>
</main>

<!-- Confirm Dialog -->
<div class="dialog-overlay hidden" id="confirm-dialog">
  <div class="dialog">
    <h3>⚡ Merge bestätigen</h3>
    <p id="confirm-body"></p>
    <div class="row">
      <button class="btn-secondary" onclick="hideConfirmDialog()">Abbrechen</button>
      <button class="btn-success" onclick="executeMerge()">▶️ Merge starten</button>
    </div>
  </div>
</div>

<script>{js}</script>
</body>
</html>"""


class FlipsiStitchHandler(BaseHTTPRequestHandler):
    """HTTP request handler for FlipsiStitch Web-UI."""

    def log_message(self, fmt, *args):
        """Suppress default logging to stderr."""
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            html = WEB_HTML_TEMPLATE.format(css=WEB_CSS, js=WEB_JS, version=__version__)
            self._send_html(html)

        elif path == "/api/scan":
            self._handle_scan()

        elif path == "/api/progress":
            self._handle_sse()

        elif path == "/api/ffmpeg-status":
            self._handle_ffmpeg_status()

        elif path == "/api/check-update":
            self._handle_check_update()

        elif path == "/api/test-color":
            self._handle_test_color()

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/merge":
            self._handle_merge()

        elif path == "/api/download-ffmpeg":
            self._handle_download_ffmpeg()

        elif path == "/api/do-update":
            self._handle_do_update()

        else:
            self._send_json({"error": "Not found"}, 404)

    def _parse_query(self):
        """Parse query parameters from the URL."""
        if _HAS_URLPARSE:
            parsed = urlparse(self.path)
            return parse_qs(parsed.query)
        else:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            return parse_qs(parsed.query)

    def _handle_scan(self):
        params = self._parse_query()
        folder_str = params.get("folder", [None])[0]
        if not folder_str:
            self._send_json({"error": "folder parameter required"}, 400)
            return

        folder = Path(folder_str).expanduser().resolve()
        if not folder.is_dir():
            self._send_json({"error": f"Kein Verzeichnis: {folder}"}, 400)
            return

        # Ensure ffmpeg is available
        try:
            ffmpeg_bin = _find_ffmpeg()
        except FileNotFoundError as exc:
            # Return special status so Web-UI can show download page
            self._send_json({
                "error": str(exc),
                "need_ffmpeg": True,
                "download_url": _get_ffmpeg_download_url(),
            }, 200)
            return

        try:
            files = _scan_folder(folder)
            groups, warnings = _group_segments(files, validate_metadata=True)

            # Build response
            groups_data = {}
            for key, segs in groups.items():
                seg_info = []
                for s in segs:
                    meta = _get_video_metadata(s, ffmpeg_bin=ffmpeg_bin)
                    info = {
                        "name": s.name,
                        "size": s.stat().st_size,
                        "resolution": f"{meta['width']}x{meta['height']}" if meta else "?",
                        "codec": meta.get("codec", "?") if meta else "?",
                        "duration": meta.get("duration", 0) if meta else 0,
                        "color_jump": False,
                    }
                    seg_info.append(info)
                groups_data[key] = seg_info

            self._send_json({
                "folder": str(folder),
                "groups": groups_data,
                "warnings": warnings,
                "total_files": len(files),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_merge(self):
        params = self._parse_query()
        folder_str = params.get("folder", [None])[0]
        output_str = params.get("output", [None])[0]
        color_profile = params.get("color_profile", ["none"])[0]
        codec_str = params.get("codec", ["hevc"])[0]
        hwaccel_str = params.get("hwaccel", ["auto"])[0]
        # Legacy param support
        color_correct = params.get("color_correct", ["0"])[0] == "1"
        threshold_str = params.get("threshold", ["0.08"])[0]

        if not folder_str:
            self._send_json({"error": "folder parameter required"}, 400)
            return

        try:
            _config["color_threshold"] = float(threshold_str)
        except ValueError:
            pass

        _config["color_profile"] = color_profile
        _config["codec"] = codec_str
        _config["hwaccel"] = hwaccel_str

        folder = Path(folder_str).expanduser().resolve()
        if not folder.is_dir():
            self._send_json({"error": f"Kein Verzeichnis: {folder}"}, 400)
            return

        output_dir = Path(output_str).expanduser().resolve() if output_str else None

        try:
            ffmpeg_bin = _find_ffmpeg()
            files = _scan_folder(folder)
            groups, warnings = _group_segments(files, validate_metadata=True)

            if not groups:
                self._send_json({"results": [], "error": "Keine Gruppen gefunden"}, 200)
                return

            results = []
            group_keys = list(groups.keys())
            total_groups = len(group_keys)

            for gi, key in enumerate(group_keys):
                _sse_push({"type": "group_start", "group": key,
                            "current": gi + 1, "total": total_groups})

                out_path = _output_path(key, groups[key], output_dir, False)

                # Get total duration for progress
                total_dur = 0
                for seg in groups[key]:
                    meta = _get_video_metadata(seg, ffmpeg_bin=ffmpeg_bin)
                    if meta:
                        total_dur += meta.get("duration", 0)

                sizes_before = sum(s.stat().st_size for s in groups[key])

                success = _merge_group(
                    group_key=key,
                    segments=groups[key],
                    ffmpeg_bin=ffmpeg_bin,
                    output_dir=output_dir,
                    suffix_mode=False,
                    overwrite=True,
                    color_correct=color_correct,
                    color_profile=color_profile,
                    codec=codec_str,
                    hwaccel=hwaccel_str,
                )

                if success and out_path.exists():
                    size_after = out_path.stat().st_size
                    _sse_push({"type": "group_done", "group": key})
                    results.append({
                        "group": key,
                        "success": True,
                        "path": str(out_path),
                        "segments": len(groups[key]),
                        "size_before": sizes_before,
                        "size_after": size_after,
                    })
                else:
                    _sse_push({"type": "group_error", "group": key,
                                "error": "Merge fehlgeschlagen"})
                    results.append({
                        "group": key,
                        "success": False,
                        "error": "Merge fehlgeschlagen",
                    })

            self._send_json({"results": results})

        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_sse(self):
        """SSE endpoint for real-time progress updates."""
        self._send_sse_headers()

        try:
            while True:
                msgs = _sse_drain()
                for msg in msgs:
                    data = json.dumps(msg)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))

                # Also send heartbeat to keep connection alive
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_ffmpeg_status(self):
        """Check if ffmpeg is available and return status."""
        try:
            ffmpeg_bin = _find_ffmpeg()
            # Also check ffprobe
            ffprobe_bin = _find_ffprobe(ffmpeg_bin=ffmpeg_bin)
            hwaccel = _get_best_gpu_encoder()
            hwaccel_label = {
                "nvenc": "NVIDIA NVENC",
                "amf": "AMD AMF",
                "qsv": "Intel QSV",
                "videotoolbox": "Apple VideoToolbox",
                "cpu": "CPU",
            }.get(hwaccel, "CPU")

            self._send_json({
                "available": True,
                "ffmpeg_path": ffmpeg_bin,
                "ffprobe_available": bool(ffprobe_bin),
                "source": "system-path" if shutil.which("ffmpeg") else "bundled",
                "hwaccel": hwaccel_label,
            })
        except FileNotFoundError:
            self._send_json({
                "available": False,
                "download_url": _get_ffmpeg_download_url(),
                "msg": "ffmpeg nicht gefunden. Klicke auf 'ffmpeg herunterladen'.",
            })

    def _handle_check_update(self):
        """Check for updates via GitHub."""
        update_info = _check_for_update()
        if update_info:
            _config["update_available"] = update_info
            self._send_json({
                "update_available": True,
                "current_version": __version__,
                "latest_version": update_info["version"],
                "release_url": update_info["url"],
            })
        else:
            self._send_json({
                "update_available": False,
                "current_version": __version__,
            })

    def _handle_do_update(self):
        """Download and install the latest update."""
        update_info = _config.get("update_available")
        if not update_info:
            update_info = _check_for_update()

        if not update_info:
            self._send_json({"success": False, "msg": "Kein Update verfügbar."})
            return

        # Start download in background
        def _download_update_thread():
            ok, msg = _download_update(update_info["version"])
            _sse_push({
                "type": "log",
                "level": "success" if ok else "error",
                "msg": msg,
            })

        thread = threading.Thread(target=_download_update_thread, daemon=True)
        thread.start()
        self._send_json({"success": True, "msg": f"Update auf {update_info['version']} gestartet."})

    def _handle_test_color(self):
        """Extract one frame for color profile comparison (before/after)."""
        params = self._parse_query()
        folder_str = params.get("folder", [None])[0]
        color_profile = params.get("color_profile", ["dlogm"])[0]

        if not folder_str:
            self._send_json({"error": "folder parameter required"}, 400)
            return

        folder = Path(folder_str).expanduser().resolve()
        try:
            ffmpeg_bin = _find_ffmpeg()
            files = _scan_folder(folder)
            if not files:
                self._send_json({"error": "Keine Videodateien gefunden"}, 400)
                return

            sample = files[0]
            tmpdir = Path(tempfile.mkdtemp(prefix="flipsisitch_test_"))
            before_path = tmpdir / "before.png"
            after_path = tmpdir / "after.png"

            # Extract original frame
            ok = _extract_frame(sample, before_path, "first", ffmpeg_bin)
            if not ok:
                self._send_json({"error": "Frame-Extraktion fehlgeschlagen"}, 500)
                return

            # Read before frame as base64
            with open(before_path, "rb") as f:
                before_b64 = base64.b64encode(f.read()).decode("ascii")

            # Extract after applying color profile
            is_dlog_m = _detect_dlog_m(sample)
            color_filter = _get_color_filter_chain(color_profile, is_dlog_m)

            if color_filter:
                cmd = [
                    ffmpeg_bin, "-y",
                    "-i", str(sample),
                    "-vf", f"{color_filter},select='eq(n\\,0)'",
                    "-vframes", "1",
                    str(after_path),
                ]
                subprocess.run(cmd, capture_output=True, timeout=30)

                if after_path.exists():
                    with open(after_path, "rb") as f:
                        after_b64 = base64.b64encode(f.read()).decode("ascii")
                else:
                    after_b64 = before_b64
            else:
                after_b64 = before_b64

            self._send_json({
                "before": f"data:image/png;base64,{before_b64}",
                "after": f"data:image/png;base64,{after_b64}",
                "color_profile": color_profile,
                "is_dlog_m": is_dlog_m,
                "filter_applied": color_filter if color_filter else "none",
            })

            # Cleanup
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except OSError:
                pass

        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_download_ffmpeg(self):
        """Trigger ffmpeg download in a background thread."""
        # Start download in thread so HTTP response returns immediately
        def _download_thread():
            ok, msg = _download_and_install_ffmpeg()
            _sse_push({
                "type": "ffmpeg_download_complete",
                "success": ok,
                "msg": msg,
            })

        thread = threading.Thread(target=_download_thread, daemon=True)
        thread.start()
        self._send_json({"status": "started", "msg": "ffmpeg-Download gestartet. Verfolge den Fortschritt über SSE."})


def _start_web_server(port: int = 8420, open_browser: bool = True) -> None:
    """Start the FlipsiStitch Web-UI server.

    Args:
        port: Port to listen on. If 8420 is taken, tries next ports.
        open_browser: Whether to auto-open browser.
    """
    max_attempts = 10
    for attempt in range(max_attempts):
        try:
            server = HTTPServer(("0.0.0.0", port), FlipsiStitchHandler)
            break
        except OSError:
            if attempt == max_attempts - 1:
                raise
            port += 1

    # Pre-check GPU info for Web-UI display
    best_gpu = _get_best_gpu_encoder()
    gpu_label = {
        "nvenc": "NVIDIA NVENC", "amf": "AMD AMF",
        "qsv": "Intel QSV", "videotoolbox": "Apple VideoToolbox",
        "cpu": "CPU",
    }.get(best_gpu, "CPU")

    print(f"\n🎬 FlipsiStitch Web-UI läuft auf http://localhost:{port}")
    print(f"   ⚡ GPU: {gpu_label}")
    print(f"   Drücke Strg+C zum Beenden.\n")

    if open_browser:
        webbrowser.open(f"http://localhost:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 FlipsiStitch Web-UI beendet.")
        server.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for FlipsiStitch."""
    parser = argparse.ArgumentParser(
        prog="flipsisitch",
        description=(
            "FlipsiStitch – Verlustfreies Zusammenfügen von DJI-Videosegmenten.\n"
            "Erkennt DJI-Segmente (z.B. DJI_20240101120000_0001.MP4, …)\n"
            "und fügt sie per ffmpeg verlustfrei zusammen.\n\n"
            "Features: D-Log M → Rec.709, H.265/HEVC, GPU-Beschleunigung, Auto-Update, Web-UI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  flipsisitch                              # aktuellen Ordner scannen
  flipsisitch ~/Videos/DJI                 # Ordner scannen & mergen
  flipsisitch --dry-run /pfad              # nur anzeigen
  flipsisitch --force --output ./merged .  # ohne Rückfrage, in merged/
  flipsisitch --group "DJI_20240101120000" # nur eine Gruppe mergen
  flipsisitch --overwrite --force .        # bestehende Dateien überschreiben
  flipsisitch --web                        # Web-UI im Browser starten
  flipsisitch --web --port 9000            # Web-UI auf Port 9000
  flipsisitch --color-profile both --force .  # D-Log M + Weißabgleich
  flipsisitch --color-profile dlogm --codec h264 --hwaccel nvenc --force .
  flipsisitch --check-update               # Auf Updates prüfen
  flipsisitch --update                     # Auf neueste Version updaten
        """,
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="Zu scannender Ordner (Standard: aktuelles Verzeichnis)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Nur anzeigen, was gemacht würde – nichts ausführen",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Keine Bestätigungsfrage vor dem Mergen",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Bestehende Ausgabedateien überschreiben",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Ausgabeordner (Standard: merged/ Unterordner im Quellordner)",
    )
    parser.add_argument(
        "--suffix",
        action="store_true",
        help="Statt merged/-Ordner: Dateiname mit _merged Suffix im Quellordner",
    )
    parser.add_argument(
        "--group", "-g",
        type=str,
        default=None,
        help="Nur eine bestimmte Gruppe mergen (Gruppen-Key oder Dateinamen-Präfix)",
    )
    parser.add_argument(
        "--ffmpeg",
        type=str,
        default=None,
        help="Pfad zur ffmpeg-Executable (Standard: aus PATH)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Ausführliche Ausgabe (DEBUG-Level)",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"FlipsiStitch {__version__}",
    )
    # ── Web-UI ─────────────────────────────────────────────────────
    parser.add_argument(
        "--web", "-w",
        action="store_true",
        help="Web-UI im Browser starten (statt CLI-Merge)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=8420,
        help="Port für die Web-UI (Standard: 8420)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Browser nicht automatisch öffnen (nur Server starten)",
    )
    # ── Color Profile ─────────────────────────────────────────────
    parser.add_argument(
        "--color-profile",
        type=str,
        default="none",
        choices=["none", "dlogm", "whitebalance", "both"],
        help="Farbkorrektur-Profil: none, dlogm (D-Log M→Rec.709), "
             "whitebalance (Weißabgleich), both (beides, empfohlen). Default: none",
    )
    parser.add_argument(
        "--test-color",
        action="store_true",
        help="Extrahiert 1 Frame VOR und NACH Farbkorrektur zum Vergleich",
    )
    # ── Legacy Color Correction ────────────────────────────────────
    parser.add_argument(
        "--color-correct", "-cc",
        action="store_true",
        help="[Legacy] Segment-Übergangs-Farbkorrektur aktivieren",
    )
    parser.add_argument(
        "--cc-threshold",
        type=float,
        default=0.08,
        help="Schwelle für Farbsprung-Erkennung (0.01–1.0, Standard: 0.08)",
    )
    # ── Codec ──────────────────────────────────────────────────────
    parser.add_argument(
        "--codec",
        type=str,
        default="hevc",
        choices=["hevc", "h264", "copy"],
        help="Video-Codec: hevc (H.265, Standard bei Re-Encoding), "
             "h264 (H.264), copy (verlustfrei). Default: hevc",
    )
    # ── Hardware Acceleration ──────────────────────────────────────
    parser.add_argument(
        "--hwaccel",
        type=str,
        default="auto",
        choices=["auto", "nvenc", "amf", "qsv", "videotoolbox", "cpu"],
        help="Hardware-Beschleunigung: auto, nvenc (NVIDIA), "
             "amf (AMD), qsv (Intel), videotoolbox (Apple), cpu. Default: auto",
    )
    # ── Update ─────────────────────────────────────────────────────
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Auf GitHub nach neuerer Version suchen",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Auf neueste Version updaten (falls verfügbar)",
    )
    # ── DJI Prefix ─────────────────────────────────────────────────
    parser.add_argument(
        "--dji-prefix",
        type=str,
        default="DJI_",
        help="DJI-Dateinamenpräfix (Standard: DJI_)",
    )
    return parser


def _print_found(folder: Path, groups: Dict[str, List[Path]],
                 warnings: Dict[str, List[str]] = None) -> None:
    """Print a summary of found groups to stdout."""
    print(f"\n📁 Ordner: {folder}")
    print(f"   Gruppen gefunden: {len(groups)}\n")
    for key, segments in groups.items():
        total = sum(s.stat().st_size for s in segments)
        meta = _get_video_metadata(segments[0])
        res_str = f"{meta['width']}x{meta['height']}" if meta else "?"
        print(f"  ▸ Gruppe '{key}'  ({len(segments)} Segmente, {_human_size(total)}, {res_str})")
        for seg in segments:
            size = _human_size(seg.stat().st_size)
            print(f"      {seg.name}  ({size})")
        if warnings and key in warnings:
            for w in warnings[key]:
                print(f"      ⚠ {w}")
        print()


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for FlipsiStitch CLI.

    Returns exit code (0 = success, 1 = error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # ── Config ─────────────────────────────────────────────────────
    _config["dji_prefix"] = args.dji_prefix
    _config["color_threshold"] = args.cc_threshold
    _config["color_profile"] = args.color_profile
    _config["codec"] = args.codec
    _config["hwaccel"] = args.hwaccel

    # ── logging setup ──────────────────────────────────────────────
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s" if level == logging.INFO else "%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    # ── Web-UI mode ────────────────────────────────────────────────
    if args.web:
        _start_web_server(
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0

    # ── Check for updates ────────────────────────────────────────
    if args.check_update:
        update_info = _check_for_update()
        if update_info:
            print(f"📦 Update verfügbar: {update_info['version']}")
            print(f"   Download: {update_info['url']}")
            print(f"\nZum Installieren: flipsisitch --update")
        else:
            print(f"✅ FlipsiStitch ist aktuell (v{__version__})")
        return 0

    # ── Do update ─────────────────────────────────────────────────
    if args.update:
        update_info = _check_for_update()
        if not update_info:
            print(f"✅ Keine neuere Version verfügbar (aktuell: v{__version__})")
            return 0
        print(f"📥 Update auf v{update_info['version']} wird heruntergeladen …")
        ok, msg = _download_update(update_info["version"])
        print(msg)
        return 0 if ok else 1

    # ── resolve folder ─────────────────────────────────────────────
    folder = Path(args.folder).resolve() if args.folder else Path.cwd()

    # ── check ffmpeg ───────────────────────────────────────────────
    try:
        ffmpeg_bin = _find_ffmpeg(args.ffmpeg)
    except FileNotFoundError as exc:
        log.error(str(exc))
        return 1
    log.debug("ffmpeg: %s", ffmpeg_bin)

    # ── Test color mode ───────────────────────────────────────────
    if args.test_color:
        files = _scan_folder(folder)
        if not files:
            print(f"⚠️  Keine Videodateien in: {folder}")
            return 1
        sample = files[0]
        is_dlog = _detect_dlog_m(sample)
        print(f"🎨 D-Log M erkannt: {'Ja ✅' if is_dlog else 'Nein ❌'}")
        print(f"   Farbprofil: {args.color_profile}")

        color_filter = _get_color_filter_chain(args.color_profile, is_dlog)
        print(f"   Filter: {color_filter if color_filter else 'keiner (keine Änderung)'}")

        tmpdir = Path(tempfile.mkdtemp(prefix="flipsisitch_test_"))
        before = tmpdir / "before.png"
        after = tmpdir / "after.png"

        if _extract_frame(sample, before, "first", ffmpeg_bin):
            print(f"   Frame VORHER: {before}")
            print(f"   (mit Bildbetrachter öffnen zum Vergleich)")

        if color_filter:
            cmd = [
                ffmpeg_bin, "-y",
                "-i", str(sample),
                "-vf", f"{color_filter},select='eq(n\\,0)'",
                "-vframes", "1", str(after),
            ]
            subprocess.run(cmd, capture_output=True, timeout=30)
            if after.exists():
                print(f"   Frame NACHHER: {after}")
            else:
                print(f"   ⚠️ Korrektur-Frame konnte nicht extrahiert werden.")
        else:
            print(f"   Kein Filter nötig – VORHER = NACHHER")

        print(f"\n   Temp-Dateien in: {tmpdir}")
        return 0

    # ── scan ───────────────────────────────────────────────────────
    try:
        files = _scan_folder(folder)
    except NotADirectoryError as exc:
        log.error(str(exc))
        return 1

    if not files:
        print(f"⚠️  Keine Videodateien (.mp4/.mov) gefunden in: {folder}")
        return 0

    groups, warnings = _group_segments(files, validate_metadata=True)

    if not groups:
        print(f"ℹ️  Keine zusammenführbaren Segment-Gruppen gefunden in: {folder}")
        log.info("  %d Videodatei(en) insgesamt, aber keine mit Multi-Segment-Muster.", len(files))
        return 0

    # ── filter by --group ──────────────────────────────────────────
    if args.group:
        group_filter = args.group.lower()
        filtered = {
            k: v for k, v in groups.items()
            if k == group_filter or k.startswith(group_filter) or group_filter in k
        }
        if not filtered:
            log.error(
                "Keine Gruppe gefunden die '%s' enthält. "
                "Verfügbare Gruppen: %s",
                args.group,
                ", ".join(groups.keys()),
            )
            return 1
        groups = filtered

    # ── dry-run ────────────────────────────────────────────────────
    if args.dry_run:
        print("🔍 DRY-RUN – Folgende Aktionen würden ausgeführt:\n")
        _print_found(folder, groups, warnings)
        for key, segments in groups.items():
            out = _output_path(key, segments, args.output, args.suffix)
            cp = args.color_profile
            codec = args.codec
            hw = args.hwaccel
            corr_note = ""
            if cp != "none" or args.color_correct:
                corr_note = f" (Farbprofil={cp}, Codec={codec}, Hardware={hw})"
            print(f"  → Würde erzeugen: {out}{corr_note}")
        print(f"\n📊 {len(groups)} Gruppe(n), keine Änderungen vorgenommen.")
        return 0

    # ── display & confirm ──────────────────────────────────────────
    _print_found(folder, groups, warnings)

    # Show GPU info
    best_gpu = _get_best_gpu_encoder(args.hwaccel)
    gpu_label = {
        "nvenc": "NVIDIA NVENC", "amf": "AMD AMF",
        "qsv": "Intel QSV", "videotoolbox": "Apple VideoToolbox",
        "cpu": "CPU (Software)",
    }.get(best_gpu, "CPU")
    print(f"🎨 Farbprofil: {args.color_profile}")
    print(f"📹 Codec: {args.codec}")
    print(f"⚡ Hardware: {gpu_label}")
    print()

    if not args.force:
        try:
            answer = input("Fortfahren? [j/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAbgebrochen.")
            return 1
        if answer not in ("j", "ja", "y", "yes"):
            print("Abgebrochen.")
            return 0

    # ── merge ──────────────────────────────────────────────────────
    errors = 0
    for key, segments in groups.items():
        success = _merge_group(
            group_key=key,
            segments=segments,
            ffmpeg_bin=ffmpeg_bin,
            output_dir=args.output,
            suffix_mode=args.suffix,
            overwrite=args.overwrite,
            color_correct=args.color_correct,
            color_profile=args.color_profile,
            codec=args.codec,
            hwaccel=args.hwaccel,
        )
        if not success:
            errors += 1

    if errors:
        log.warning("%d Gruppe(n) mit Fehlern.", errors)
        return 1

    print(f"\n✅ {len(groups)} Gruppe(n) erfolgreich zusammengefügt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
