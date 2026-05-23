FROM python:3.11-slim

WORKDIR /app

COPY notifications/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user (uid 1000) for security
RUN useradd -m -u 1000 appuser

# Copy application code with correct ownership
COPY --chown=appuser:appuser notifications/worker.py .

USER appuser

# Liveness check: verify the worker process can execute Python
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import pika; print('ok')" || exit 1

CMD ["python", "worker.py"]