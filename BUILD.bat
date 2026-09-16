@echo off
chcp 65001 >nul
title Duplicater - Build & Run
echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║                   DUPLICATER BUILD TOOL                      ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.
echo [1] Run Application (Development)
echo [2] Build Portable EXE
echo [3] Exit
echo.
set /p choice="Select option (1-3): "

if "%choice%"=="1" goto run
if "%choice%"=="2" goto build
if "%choice%"=="3" exit /b
goto :eof

:run
echo.
echo Starting Duplicater...
python -m pip install -r requirements.txt >nul 2>&1
python main.py
goto :eof

:build
echo.
echo ==========================================
echo Setting up clean build environment...
echo ==========================================

REM 1. Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not found.
    echo Please install Python from https://python.org and add it to PATH.
    pause
    exit /b
)

REM 2. Create a virtual environment if needed
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

REM 3. Activate virtual environment
call venv\Scripts\activate.bat

REM 4. Install dependencies
echo Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt

REM 5. Build the executable
echo Building portable EXE with PyInstaller...
python -m PyInstaller --noconfirm --onefile --windowed --collect-all flet --name "Duplicater" main.py

echo.
echo ==========================================
echo BUILD COMPLETE!
echo Your portable app is ready:
echo.
echo    dist\Duplicater.exe
echo.
echo This file works on any Windows PC without
echo needing Python installed.
echo ==========================================
pause
goto :eof
