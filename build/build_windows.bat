@echo off
rem Local Windows build (alternative to GitHub Actions).
rem Needs: Python 3.12 in PATH, Inno Setup 6 (https://jrsoftware.org/isdl.php).
chcp 65001 >nul
cd /d "%~dp0\.."
python build\stamp_version.py || goto :err
python -m pip install -r requirements.txt pyinstaller || goto :err
python setup_models.py || goto :err
if not exist build\ffmpeg\ffmpeg.exe (
  powershell -NoProfile -Command "Invoke-WebRequest https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip -OutFile ffmpeg.zip; Expand-Archive -Force ffmpeg.zip ffx; New-Item -ItemType Directory -Force build\ffmpeg | Out-Null; $b=Get-ChildItem ffx -Recurse -Filter ffmpeg.exe | Select -First 1; Copy-Item $b.FullName build\ffmpeg\; Copy-Item (Join-Path $b.DirectoryName ffprobe.exe) build\ffmpeg\" || goto :err
)
pyinstaller --noconfirm --clean --onedir --noconsole --name PoruwaCutEngine --icon build\icon.ico --add-data "web;web" --add-data "models;models" --collect-submodules engine poruwacut_engine.py || goto :err
python build\make_ccx.py || goto :err
set /p VER=<VERSION
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
%ISCC% /DMyVersion=%VER% build\installer.iss || goto :err
echo.
echo DONE: Output\PoruwaCut_Setup_%VER%.exe  and  dist\PoruwaCut_%VER%.ccx
pause
exit /b 0
:err
echo BUILD FAILED
pause
exit /b 1
