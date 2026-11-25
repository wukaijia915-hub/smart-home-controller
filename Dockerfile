# syntax=docker/dockerfile:1
# 灵犀后端镜像：单进程 uvicorn + 静态前端托管。
#
# 三个刻意的选择（都来自"能不能真的交付到一台盒子上"这个问题）：
# 1) 非 root 运行（uid 10001）：进程能写的地方只有 /app/data，容器逃逸面最小。
# 2) 健康检查打 /healthz（无认证）：restart:unless-stopped 只能救"进程退出"，
#    救不了"进程活着但事件循环冻结"——所以探针必须能真的被容器打到。
# 3) 依赖保持 requirements.txt 那几行：不装 opencv/faiss 这类重依赖，
#    镜像小、冷启动快、ARM 盒子也能跑。视觉重依赖放 requirements-vision.txt，
#    按需安装且缺失时优雅降级。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    LINGXI_DATA_DIR=/app/data

WORKDIR /app

# tzdata：日报/定时任务按 Asia/Shanghai 计算，缺时区数据会静默退化成 UTC
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# 非 root 用户；数据目录与运行时状态归它所有
RUN useradd --create-home --uid 10001 lingxi \
    && mkdir -p /app/data \
    && chown -R lingxi:lingxi /app

USER lingxi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
