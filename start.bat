@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  全屋智能数字人中枢 - Web 3D Demo 启动
echo ============================================
echo [1/2] 检查并安装依赖...
python -m pip install -r requirements.txt
echo [2/2] 启动服务，浏览器打开 http://127.0.0.1:8000
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
pause
