# One image: the UI is built and then served by the backend on a single port.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./backend/
WORKDIR /app/backend
RUN uv sync --frozen --no-dev
WORKDIR /app
COPY backend/ ./backend/
COPY --from=ui /ui/dist ./frontend/dist
WORKDIR /app/backend
ENV KN_API_HOST=0.0.0.0 KN_DATA_DIR=/data KN_DB_URL=sqlite+aiosqlite:////data/kotsin_nse.db
VOLUME ["/data"]
EXPOSE 8500
CMD ["uv", "run", "kotsin-nse"]
