@echo off
setlocal
title nvidia2api Launcher
cd /d "%~dp0"

rem =====================================================
rem  用法:
rem    start.bat        -- 生产模式：构建/复用前端静态产物，由后端同源托管（快）
rem    start.bat dev    -- 开发模式：前端 npm run dev :3000 + 后端 :8000（热更新）
rem  说明：dev 模式下首次进入每个页面 Next.js 才现场编译路由，
rem        体感是"每个页面都要卡几秒"——只在改前端代码时才用它。
rem =====================================================

set MODE=prod
if /i "%~1"=="dev" set MODE=dev

echo =====================================================
if /i "%MODE%"=="dev" (echo    nvidia2api  [DEV 开发模式]) else (echo    nvidia2api  [生产模式])
echo =====================================================

rem ---- [1] 后端依赖 ----
python -c "import django,rest_framework,uvicorn,httpx,httpx_socks,curl_cffi" >nul 2>&1
if errorlevel 1 (
    echo [1] Installing backend dependencies ...
    python -m pip install -r backend\requirements.txt
    if errorlevel 1 (echo Backend dependency install FAILED. & pause & exit /b 1)
) else (
    echo [1] Backend dependencies OK
)

rem ---- [2] 数据库迁移 ----
echo [2] Applying database migrations ...
pushd backend
python manage.py migrate
if errorlevel 1 (echo Database migration FAILED. & pause & exit /b 1)
popd

rem ---- [3] 前端 ----
if /i "%MODE%"=="dev" (
    if not exist "frontend\node_modules" (
        echo [3] Installing frontend dependencies ...
        pushd frontend
        call npm install
        if errorlevel 1 (echo Frontend dependency install FAILED. & pause & exit /b 1)
        popd
    ) else (
        echo [3] Frontend dependencies OK
    )
) else (
    if exist "frontend\out\index.html" (
        echo [3] Frontend static build found ^(frontend\out^)
    ) else (
        echo [3] Building frontend static export ...
        if not exist "frontend\node_modules" (
            pushd frontend
            call npm install
            if errorlevel 1 (echo Frontend dependency install FAILED. & pause & exit /b 1)
            popd
        )
        pushd frontend
        call npm run build
        if errorlevel 1 (echo Frontend build FAILED. & pause & exit /b 1)
        popd
    )
)

rem ---- [4] 启动 ----
echo [4] Starting ...
if /i "%MODE%"=="dev" (
    start "nvidia2api Backend :8000" cmd /k "cd /d "%~dp0backend" && python -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000"
    start "nvidia2api Frontend :3000" cmd /k "cd /d "%~dp0frontend" && npm run dev"
    echo.
    echo   Backend : http://127.0.0.1:8000
    echo   Frontend: http://localhost:3000  ^(dev 编译模式，首进每个页面会慢^)
    echo.
    ping -n 5 127.0.0.1 >nul
    start http://localhost:3000
) else (
    rem 生产模式：单进程同源托管 API + 静态前端
    start "nvidia2api :8000" cmd /k "cd /d "%~dp0backend" && set FRONTEND_DIR=%~dp0frontend\out&& python -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000"
    echo.
    echo   Console: http://127.0.0.1:8000  ^(API + 控制台同源^)
    echo.
    ping -n 5 127.0.0.1 >nul
    start http://127.0.0.1:8000
)
echo   Done. 关闭弹出的窗口即可停止服务。
pause >nul
endlocal
