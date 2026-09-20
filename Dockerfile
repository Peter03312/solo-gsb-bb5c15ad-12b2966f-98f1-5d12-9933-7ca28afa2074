# 字幕时间轴三方合并 API —— 容器镜像
FROM python:3.11-slim

# 不生成 .pyc、日志不缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app ./app

# 以非 root 用户运行
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 容器启动即初始化并对外服务（依赖已在构建阶段装入镜像）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"
