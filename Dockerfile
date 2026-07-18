# Multi-stage build: React frontend + Python backend in a single container
# Stage 1: Build React dashboard
FROM node:20-slim AS frontend-build

WORKDIR /app/dashboard-ui
COPY dashboard-ui/package.json dashboard-ui/package-lock.json ./
RUN npm ci
COPY dashboard-ui/ ./
RUN npm run build

# Stage 2: Python application
FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy Python source
COPY smart_trader/ ./smart_trader/

# Copy React build output to a location FastAPI can serve
COPY --from=frontend-build /app/dashboard-ui/dist ./smart_trader/static/

# Expose the single port
EXPOSE 8000

# Healthcheck: verify /api/health responds within 60s of startup
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

# Start the server
CMD ["uvicorn", "smart_trader.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
