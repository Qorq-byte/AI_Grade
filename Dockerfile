# ═══════════════════════════════════
#  祺orQ — Dockerfile
#  基于 Python 3.11-slim，包含 OCR 中文支持
# ═══════════════════════════════════
FROM python:3.11-slim

LABEL maintainer="AI Grade Team"
LABEL description="AI绩效评价报告生成系统"

# 系统依赖：build-essential (mysqlclient)、tesseract-ocr (图片OCR)、中文语言包
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    libmariadb-dev-compat \
    pkg-config \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件（利用 Docker 缓存层）
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制入口脚本（在 COPY . . 之前，保持层独立）
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 复制项目代码
COPY . .

# Python 路径
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 创建运行时目录 & 非 root 用户
RUN mkdir -p /app/backend/data/chroma_db /app/uploads && \
    groupadd -r appuser && \
    useradd -r -g appuser -d /app appuser && \
    chown -R appuser:appuser /app

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 切换到非 root 用户运行
USER appuser

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]