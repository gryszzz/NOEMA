FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY noema ./noema

RUN pip install --no-cache-dir .

RUN mkdir -p /data

ENV NOEMA_DB_PATH=/data/noema.db

CMD ["python", "-m", "noema.worker_entry"]
