FROM python:3.12-slim

# Don't write .pyc files; flush stdout/stderr immediately for clean logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Persisted data (SQLite db + dev email outbox) lives here.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /data /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
