@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PoruwaCut Engine
start "" http://127.0.0.1:7788
python -m engine serve
pause
