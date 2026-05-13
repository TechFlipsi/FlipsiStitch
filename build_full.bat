@echo off
REM ============================================================
REM  FlipsiStitch – Full Release Builder (MIT ffmpeg)
REM ============================================================
REM  Erstellt ein ZIP-Release-Paket mit:
REM    - flipsisitch.exe
REM    - ffmpeg.exe + ffprobe.exe (eingebettet)
REM    - README.md
REM    - VERSION
REM ============================================================

setlocal enabledelayedexpansion

echo.
echo  === FlipsiStitch Full Release Builder ===
echo.

REM ── VERSION aus Datei lesen ──
if exist "VERSION" (
    set /p VERSION=<VERSION
) else (
    set VERSION=0.0.0
    echo [WARNUNG] VERSION-Datei nicht gefunden! Verwende 0.0.0
)
echo Version: !VERSION!
set RELEASE_NAME=FlipsiStitch_v!VERSION!_Full

REM ── Prüfe ob flipsisitch.exe existiert ──
if not exist "dist\flipsisitch.exe" (
    echo [FEHLER] dist\flipsisitch.exe nicht gefunden.
    echo   Bitte zuerst build.bat ausführen!
    exit /b 1
)

REM ── Finde ffmpeg im PATH oder Config-Verzeichnis ──
set FFMPEG_PATH=
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do set FFMPEG_PATH=%%i
if not defined FFMPEG_PATH (
    if exist "%APPDATA%\FlipsiStitch\ffmpeg.exe" (
        set FFMPEG_PATH=%APPDATA%\FlipsiStitch\ffmpeg.exe
    )
)

if defined FFMPEG_PATH (
    echo ffmpeg gefunden: !FFMPEG_PATH!
) else (
    echo [WARNUNG] ffmpeg nicht im PATH oder Config gefunden.
    echo   Das Release wird ohne ffmpeg.exe erstellt.
    echo   FlipsiStitch lädt ffmpeg beim ersten Start herunter.
    echo.
    echo   Fuer ein echtes Full-Release:
    echo   1. Lade ffmpeg von https://ffmpeg.org/download.html
    echo   2. Kopiere ffmpeg.exe in diesen Ordner
    echo   3. Fuehre build_full.bat erneut aus
)

REM ── Saubere vorheriges Release ──
if exist "!RELEASE_NAME!"     rmdir /s /q "!RELEASE_NAME!"
if exist "!RELEASE_NAME!.zip" del /q "!RELEASE_NAME!.zip"

echo [1/3] Erstelle Release-Ordner: !RELEASE_NAME!
mkdir "!RELEASE_NAME!"

echo [2/3] Kopiere Dateien ...

copy /y "dist\flipsisitch.exe" "!RELEASE_NAME!\" >nul
if exist "README.md"   copy /y "README.md"   "!RELEASE_NAME!\" >nul
if exist "VERSION"     copy /y "VERSION"     "!RELEASE_NAME!\" >nul

REM Kopiere ffmpeg/ffprobe falls verfuegbar
if defined FFMPEG_PATH (
    for %%F in ("!FFMPEG_PATH!") do set FFMPEG_DIR=%%~dpF
    copy /y "!FFMPEG_DIR!\ffmpeg.exe"  "!RELEASE_NAME!\" >nul 2>&1
    copy /y "!FFMPEG_DIR!\ffprobe.exe" "!RELEASE_NAME!\" >nul 2>&1
    
    if exist "!RELEASE_NAME!\ffmpeg.exe" (
        echo   ffmpeg.exe kopiert
    )
    if exist "!RELEASE_NAME!\ffprobe.exe" (
        echo   ffprobe.exe kopiert
    )
)

echo [3/3] Erstelle ZIP-Archiv ...

powershell -Command "Compress-Archive -Path '!RELEASE_NAME!\*' -DestinationPath '!RELEASE_NAME!.zip' -Force"

if %errorlevel% equ 0 (
    echo.
    echo  ========================================
    echo   FULL RELEASE ERSTELLT!
    echo   !RELEASE_NAME!.zip
    echo  ========================================
    echo.
    echo   Inhalt:
    echo   - flipsisitch.exe
    if exist "!RELEASE_NAME!\ffmpeg.exe"   echo   - ffmpeg.exe
    if exist "!RELEASE_NAME!\ffprobe.exe"  echo   - ffprobe.exe
    echo   - README.md
    echo   - VERSION
    echo.
    echo   🚀 Anwendung:
    echo   1. ZIP entpacken
    echo   2. flipsisitch.exe starten – fertig!
    echo.
    echo   Neue Features in v!VERSION!:
    echo   - D-Log M → Rec.709 Konvertierung
    echo   - H.265/HEVC Standard-Codec
    echo   - GPU-Hardware-Beschleunigung
    echo   - Auto-Update via GitHub
    echo   - Premium Web-UI mit Glassmorphism
    echo.
    echo   Dateigröße: ca. 90-100 MB (mit ffmpeg)
    echo.
    rmdir /s /q "!RELEASE_NAME!"
) else (
    echo.
    echo [FEHLER] ZIP-Erstellung fehlgeschlagen!
    exit /b 1
)

endlocal
