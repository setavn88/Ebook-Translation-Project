@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Chay duoc ca khi chua "pip install": lay ma nguon truc tiep tu thu muc src
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

where python >nul 2>nul
if errorlevel 1 (
  echo [LOI] Chua cai Python. Tai tai https://www.python.org/downloads/
  echo       Khi cai nho tick o "Add Python to PATH", roi chay lai file nay.
  pause
  exit /b 1
)

python -c "import anthropic, openai" >nul 2>nul
if errorlevel 1 (
  echo Lan dau chay: dang cai cac thu vien can thiet, vui long cho 1-2 phut...
  python -m pip install --upgrade pip >nul
  python -m pip install -e ".[openai]"
  if errorlevel 1 (
    echo [LOI] Cai dat that bai. Chup man hinh nay gui lai de duoc ho tro.
    pause
    exit /b 1
  )
)

echo Dang mo giao dien BookTranslator...  (dong cua so nay de tat)
python -m booktranslator giao-dien
pause
