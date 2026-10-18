@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Dang mo giao dien BookTranslator...  (dong cua so nay de tat)
python -m booktranslator giao-dien
pause
