# Talent 360i -- one image, three roles (pipeline API, portal, lifecycle worker).
# The command decides the role; see docker-compose.yml and deploy/README.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TALENT360_LOG_DIR=/app/logs \
    HOME=/app/db/home
# HOME on the database volume: the one-time embedding-model download (Agent 1
# matching) is cached there and shared by every container.

WORKDIR /app

# Dependencies first so code changes don't invalidate the (large) dependency layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agents ./agents
COPY app ./app
COPY scripts ./scripts
COPY Talent360i_phase11C_fixed_remote_hybrid ./Talent360i_phase11C_fixed_remote_hybrid
COPY data ./data
COPY start.py .env.example ./

# Non-root; only the database volume and the log directory are writable.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin talent \
    && mkdir -p /app/db /app/logs \
    && chown -R talent:talent /app/db /app/logs
USER talent

EXPOSE 8000 8010
# Default role: the pipeline API. Liveness is /healthz on either server.
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
