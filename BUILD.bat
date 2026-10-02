@echo off
chcp 65001 >nul
title TwinSweeper - Build ^& Run
echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║                   TWINSWEEPER BUILD TOOL                     ║
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
echo Starting TwinSweeper...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)
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
    exit /b 1
)

REM 2. Create a virtual environment if needed
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

REM 3. Activate virtual environment
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate the virtual environment.
    pause
    exit /b 1
)

REM 4. Install dependencies (PyInstaller lives in requirements-dev.txt)
echo Installing dependencies...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: pip upgrade failed.
    pause
    exit /b 1
)
python -m pip install -r requirements-dev.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies from requirements-dev.txt.
    pause
    exit /b 1
)

REM 5. Build the executable - TwinSweeper.spec is the single build source
echo Building portable EXE with PyInstaller...
python -m PyInstaller --noconfirm TwinSweeper.spec
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

REM 6. Post-check: the executable must exist
if not exist dist\TwinSweeper.exe (
    echo ERROR: Build failed - dist\TwinSweeper.exe was not created.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo BUILD COMPLETE!
echo Your portable app is ready:
echo.
echo    dist\TwinSweeper.exe
echo.
echo This file works on any Windows PC without
echo needing Python installed.
echo ==========================================
pause
goto :eof
