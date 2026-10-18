@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Chay duoc ca khi chua "pip install": lay ma nguon truc tiep tu thu muc src
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
rem Thu muc trinh cai Claude Code hay dat claude.exe (co the chua co trong PATH)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

rem ---- 1. Python -------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo [LOI] Chua cai Python. Tai tai https://www.python.org/downloads/
  echo       Khi cai nho tick o "Add Python to PATH", roi chay lai file nay.
  pause
  exit /b 1
)

rem ---- 2. Thu vien Python ----------------------------------------------------
python -c "import anthropic, openai" >nul 2>nul
if errorlevel 1 (
  echo Lan dau chay: dang cai cac thu vien can thiet, vui long cho 1-2 phut...
  python -m pip install --upgrade pip >nul
  python -m pip install -e ".[openai]"
  if errorlevel 1 (
    echo [LOI] Cai dat thu vien that bai. Chup man hinh nay gui lai de duoc ho tro.
    pause
    exit /b 1
  )
)

rem ---- 3. Claude Code (de dich bang goi Claude Pro/Max, khong ton tien API) ---
where claude >nul 2>nul
if errorlevel 1 (
  echo.
  echo Chua co Claude Code tren may. Claude Code giup dich bang goi Claude Pro/Max
  echo cua ban ma khong ton tien API.
  choice /c YN /m "Cai Claude Code ngay bay gio"
  if not errorlevel 2 (
    echo Dang cai Claude Code...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
    where claude >nul 2>nul
    if errorlevel 1 (
      echo [CANH BAO] Chua tim thay lenh claude sau khi cai. Xem tab "Ket noi AI" trong giao dien.
    ) else (
      echo.
      echo Mot cua so moi se mo de DANG NHAP Claude Code:
      echo   - lam theo huong dan, dang nhap bang tai khoan Claude cua ban
      echo   - dang nhap xong thi go /exit roi dong cua so do
      start "Dang nhap Claude Code" cmd /k claude
      echo Dang nhap xong thi quay lai day va
      pause
    )
  )
)

rem ---- 4. Mo giao dien -------------------------------------------------------
echo.
echo Dang mo giao dien BookTranslator...  (dong cua so nay de tat)
python -m booktranslator giao-dien
pause
