FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user (uid 1000) for security
RUN useradd -m -u 1000 appuser

# Generate gRPC stubs from proto contract (source of truth)
COPY ./catalog.proto ./proto/catalog.proto
RUN python -m grpc_tools.protoc -I ./proto --python_out=. --grpc_python_out=. ./proto/catalog.proto

# Copy application code with correct ownership
COPY main.py .
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
EXPOSE 50051

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]