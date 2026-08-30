@echo off
setlocal
title nvidia2api Launcher
cd /d "%~dp0"

echo =====================================================
echo    nvidia2api  AI API Infrastructure
echo =====================================================

rem ---- [1/4] backend dependencies ----
python -c "import django,rest_framework,uvicorn,httpx,httpx_socks,curl_cffi" >nul 2>&1
if errorlevel 1 (
    echo [1/4] Installing backend dependencies ...
    python -m pip install -r backend\requirements.txt
    if errorlevel 1 (
        echo.
        echo Backend dependency install FAILED.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Backend dependencies OK
)

rem ---- [2/4] database migrations ----
echo [2/4] Applying database migrations ...
pushd backend
python manage.py migrate
if errorlevel 1 (
    echo.
    echo Database migration FAILED.
    pause
    exit /b 1
)
popd

rem ---- [3/4] frontend dependencies ----
if not exist "frontend\node_modules" (
    echo [3/4] Installing frontend dependencies ...
    pushd frontend
    call npm install
    if errorlevel 1 (
        echo.
        echo Frontend dependency install FAILED.
        pause
        exit /b 1
    )
    popd
) else (
    echo [3/4] Frontend dependencies OK
)

rem ---- [4/4] start servers ----
echo [4/4] Starting servers ...
start "nvidia2api Backend :8000" cmd /k "cd /d ""%~dp0backend"" && python -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload"
start "nvidia2api Frontend :3000" cmd /k "cd /d ""%~dp0frontend"" && npm run dev"

echo.
echo   Backend : http://127.0.0.1:8000
echo   Frontend: http://localhost:3000
echo.
echo   Opening browser ...
ping -n 5 127.0.0.1 >nul
start http://localhost:3000
echo   Done. Keep the two server windows open; close them to stop.
echo.
endlocal
