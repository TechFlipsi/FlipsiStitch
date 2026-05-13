@echo off
REM ============================================================
REM  FlipsiStitch – Windows Portable .exe Build mit PyInstaller
REM ============================================================
REM  Voraussetzungen:
REM    1. Python 3.8+ installiert
REM    2. pyinstaller:  pip install pyinstaller
REM    3. ffmpeg wird beim ersten Start automatisch heruntergeladen
REM       ODER mit --add-binary in die .exe eingebettet
REM ============================================================

setlocal enabledelayedexpansion

echo.
echo  === FlipsiStitch Build (Portable) ===
echo.

REM ── VERSION aus Datei lesen ──
if exist "VERSION" (
    set /p BUILD_VERSION=<VERSION
    echo Version: !BUILD_VERSION!
) else (
    set BUILD_VERSION=0.0.0
    echo Version: !BUILD_VERSION! (VERSION-Datei nicht gefunden)
)

REM ── Prüfe ob pyinstaller verfügbar ist ──
where pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [FEHLER] pyinstaller nicht gefunden.
    echo   Installiere mit: pip install pyinstaller
    exit /b 1
)

REM ── Prüfe ob flipsisitch.py existiert ──
if not exist "flipsisitch.py" (
    echo [FEHLER] flipsisitch.py nicht im aktuellen Verzeichnis gefunden.
    echo   Fuehre build.bat aus dem flipsisitch-Ordner aus.
    exit /b 1
)

REM ── Finde ffmpeg für Einbettung ──
set FFMPEG_BINARY=
set FFPROBE_BINARY=
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do set FFMPEG_BINARY=%%i
for /f "delims=" %%i in ('where ffprobe 2^>nul') do set FFPROBE_BINARY=%%i

if defined FFMPEG_BINARY (
    echo ffmpeg gefunden: !FFMPEG_BINARY!
) else (
    echo [INFO] ffmpeg nicht im PATH – .exe wird ohne ffmpeg gebaut.
    echo         ffmpeg wird beim ersten Start automatisch heruntergeladen.
)

REM ── Saubere vorherige Builds ──
if exist "build" rmdir /s /q "build"
if exist "dist"  rmdir /s /q "dist"
if exist "*.spec" del /q "*.spec"

echo [1/3] Baue standalone .exe (One-File, Windowed) ...

REM Baue PyInstaller Befehl mit optionalem ffmpeg/ffprobe
set PYINSTALLER_CMD=pyinstaller --onefile --windowed --name flipsisitch --clean --noconfirm

REM Immer diese Daten einbetten
set PYINSTALLER_CMD=%PYINSTALLER_CMD% --add-data "VERSION;." --add-data "README.md;."

REM ffmpeg/ffprobe einbetten falls verfügbar
if defined FFMPEG_BINARY (
    set PYINSTALLER_CMD=%PYINSTALLER_CMD% --add-binary "!FFMPEG_BINARY!;."
)
if defined FFPROBE_BINARY (
    set PYINSTALLER_CMD=%PYINSTALLER_CMD% --add-binary "!FFPROBE_BINARY!;."
)

REM Hidden imports
set PYINSTALLER_CMD=%PYINSTALLER_CMD% --hidden-import json --hidden-import urllib --hidden-import urllib.request --hidden-import urllib.error --hidden-import hashlib --hidden-import zipfile --hidden-import threading --hidden-import webbrowser

REM Hauptdatei
set PYINSTALLER_CMD=%PYINSTALLER_CMD% flipsisitch.py

echo !PYINSTALLER_CMD!
!PYINSTALLER_CMD!

if %errorlevel% neq 0 (
    echo.
    echo [FEHLER] PyInstaller Build fehlgeschlagen!
    exit /b 1
)

echo.
echo [2/3] Setze Produktversion ...

echo.
echo [3/3] Pruefe Ausgabe ...

if exist "dist\flipsisitch.exe" (
    echo.
    echo  ========================================
    echo   BUILD ERFOLGREICH!
    echo   Ausgabe: dist\flipsisitch.exe
    echo   Version: !BUILD_VERSION!
    echo  ========================================
    echo.
    echo   🚀 PORTABLE MODE:
    echo   - Einfach flipsisitch.exe starten – fertig!
    echo   - Beim ersten Start wird ffmpeg automatisch
    echo     heruntergeladen (einmalig, ca. 80 MB)
    echo   - Oder: ffmpeg.exe in den gleichen Ordner
    echo     wie flipsisitch.exe kopieren
    echo   - Web-UI: flipsisitch --web
    echo   - D-Log M: flipsisitch --color-profile both
    echo   - GPU: flipsisitch --hwaccel nvenc
    echo.
    echo   Release-Paket bauen: build_release.bat
    echo   Release mit ffmpeg:   build_full.bat
    echo.
) else (
    echo.
    echo [FEHLER] dist\flipsisitch.exe wurde nicht erstellt!
    exit /b 1
)

endlocal
