# nvidia2api all-in-one image: Django backend + built Next.js frontend static export
# Stage 1: build frontend
FROM node:22-slim AS fe
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
ENV NEXT_PUBLIC_API_BASE_URL=""
RUN npm run build

# Stage 2: backend
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=fe /fe/out /app/static/frontend
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate && python manage.py cleanlogs && uvicorn config.asgi:application --host 0.0.0.0 --port 8000"]
