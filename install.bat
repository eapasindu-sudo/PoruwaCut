@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===== PoruwaCut install =====
where python >/dev/null 2>nul
if errorlevel 1 (
  echo Python 3.10+ install karanna: https://www.python.org/downloads/  ^(Add to PATH tick karanna^)
  pause
  exit /b 1
)
where ffmpeg >/dev/null 2>nul
if errorlevel 1 (
  echo FFmpeg install karanawa...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  echo FFmpeg install unata passe me window eka close karala install.bat ayeth run karanna.
  pause
  exit /b 0
)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python setup_models.py
echo.
echo Install iwarai. Dan start_engine.bat run karanna.
pause
