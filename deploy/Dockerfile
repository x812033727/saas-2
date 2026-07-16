FROM python:3.11-slim

WORKDIR /app
COPY platform/pyproject.toml platform/pyproject.toml
COPY platform/ticloud platform/ticloud
RUN pip install --no-cache-dir -e "./platform[postgres]"

# api: uvicorn ticloud.api.main:app / worker: python -m ticloud.scheduler.worker
CMD ["uvicorn", "ticloud.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
