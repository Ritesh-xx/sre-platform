FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY detector.py .

# Note: we don't run as non-root here because the detector needs to
# run docker commands (docker compose scale) which requires Docker socket access
# In production K8s, this would use a ServiceAccount with RBAC instead

EXPOSE 8001

CMD ["python", "detector.py"]
