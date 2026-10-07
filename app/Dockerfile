# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FITS_HOST=0.0.0.0 \
    FITS_PORT=8080

WORKDIR /app

# The service is pure standard library; no third-party packages are needed.
COPY fits_audit ./fits_audit
COPY tests ./tests
COPY scripts ./scripts

RUN useradd --system --uid 10001 --home-dir /app appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
    CMD python -c "import os,sys,urllib.request; \
port=os.environ.get('FITS_PORT','8080'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/health', timeout=2).status==200 else 1)"

CMD ["python", "-m", "fits_audit.server"]
