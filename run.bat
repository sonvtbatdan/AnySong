@echo off
setlocal
cd /d "%~dp0"

if not exist "backend\.venv\Scripts\python.exe" (
    echo [1/3] Chua co backend venv - dang cai dat lan dau...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup_backend.ps1"
    if errorlevel 1 goto :error
)

if not exist "engine\ACE-Step-1.5\.venv" (
    echo [2/3] Chua co ACE-Step engine - dang clone + cai dat lan dau ^(tai vai GB, co the mat vai chuc phut^)...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup_engine.ps1"
    if errorlevel 1 goto :error
)

if not exist "gemini_service\.venv\Scripts\python.exe" (
    echo [3/3] Chua co gemini_service venv - dang cai dat lan dau ^(tuy chon, cho tinh nang "Tu dong nho Gemini"^)...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup_gemini_service.ps1"
)

start "" "backend\.venv\Scripts\pythonw.exe" "app_launcher.py"
goto :eof

:error
echo.
echo Cai dat that bai - xem loi o tren.
pause
