@echo off
REM ============================================================
REM  FlipsiStitch – Release Package Builder (ohne ffmpeg)
REM ============================================================
REM  Erstellt ein ZIP-Release-Paket mit:
REM    - flipsisitch.exe
REM    - README.md
REM    - VERSION
REM  ffmpeg.exe wird NICHT mitgeliefert, sondern beim ersten
REM  Start von FlipsiStitch automatisch heruntergeladen.
REM ============================================================

setlocal enabledelayedexpansion

echo.
echo  === FlipsiStitch Release Builder ===
echo.

REM ── VERSION aus Datei lesen ──
if exist "VERSION" (
    set /p VERSION=<VERSION
) else (
    set VERSION=0.0.0
    echo [WARNUNG] VERSION-Datei nicht gefunden! Verwende 0.0.0
)
echo Version: !VERSION!
set RELEASE_NAME=FlipsiStitch_v!VERSION!_Portable

REM ── Prüfe ob flipsisitch.exe existiert ──
if not exist "dist\flipsisitch.exe" (
    echo [FEHLER] dist\flipsisitch.exe nicht gefunden.
    echo   Bitte zuerst build.bat ausführen!
    exit /b 1
)

REM ── Prüfe ob README.md existiert ──
if not exist "README.md" (
    echo [WARNUNG] README.md nicht gefunden.
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

echo [3/3] Erstelle ZIP-Archiv ...

REM PowerShell für ZIP (Windows 10+ hat das)
powershell -Command "Compress-Archive -Path '!RELEASE_NAME!\*' -DestinationPath '!RELEASE_NAME!.zip' -Force"

if %errorlevel% equ 0 (
    echo.
    echo  ========================================
    echo   RELEASE ERSTELLT!
    echo   !RELEASE_NAME!.zip
    echo  ========================================
    echo.
    echo   Inhalt:
    echo   - flipsisitch.exe
    echo   - README.md
    echo   - VERSION
    echo.
    echo   🚀 Anwendung:
    echo   1. ZIP entpacken
    echo   2. flipsisitch.exe starten
    echo   3. ffmpeg wird beim ersten Start
    echo      automatisch heruntergeladen
    echo.
    echo   Neue Features in v!VERSION!:
    echo   - D-Log M → Rec.709 Konvertierung
    echo   - H.265/HEVC Standard-Codec
    echo   - GPU-Hardware-Beschleunigung
    echo   - Auto-Update via GitHub
    echo   - Premium Web-UI mit Glassmorphism
    echo.
    echo   Dateigröße: ca. 15-20 MB (ohne ffmpeg)
    echo.
    rmdir /s /q "!RELEASE_NAME!"
) else (
    echo.
    echo [FEHLER] ZIP-Erstellung fehlgeschlagen!
    exit /b 1
)

endlocal
