# Playwright 官方 Python 镜像，已含 Chromium 与系统依赖
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8899 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pack_workflow.py web_app.py config.json ./
COPY web ./web

# 登录态与配置持久化目录
RUN mkdir -p /app/.playwright_profile

EXPOSE 8899

CMD ["python3", "web_app.py"]
