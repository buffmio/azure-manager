FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    FLASK_APP=/app/azure/app.py

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 azuremanager && \
    useradd --uid 10001 --gid 10001 --create-home --no-log-init azuremanager

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock --no-deps

COPY --chown=azuremanager:azuremanager . .
RUN mkdir -p /app/data && \
    chown -R azuremanager:azuremanager /app/data

EXPOSE 8888

USER azuremanager

# 生产级 WSGI (Gunicorn + 2 Worker 进程 + 2 线程，适配 2C1G 低内存高并发环境)
CMD ["gunicorn", "-w", "2", "--threads", "2", "-b", "0.0.0.0:8888", "--chdir", "/app/azure", "app:app"]
