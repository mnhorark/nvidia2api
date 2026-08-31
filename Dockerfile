# nvidia2api all-in-one image: Django backend + built Next.js frontend static export
# Stage 1: build frontend
FROM node:22-slim AS fe
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
# 有 lockfile 就锁死依赖版本，避免 CI 与本地构建出不一致的产物
RUN npm ci
COPY frontend/ .
# 留空 => 前端走同源相对路径（后端同时托管 UI 与 API，见 api/frontend_views.py）
ENV NEXT_PUBLIC_API_BASE_URL=""
RUN npm run build

# Stage 2: backend
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    FRONTEND_DIR=/app/static/frontend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=fe /fe/out /app/static/frontend
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate && python manage.py cleanlogs && uvicorn config.asgi:application --host 0.0.0.0 --port 8000"]
