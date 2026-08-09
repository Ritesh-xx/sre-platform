# ── Why python:3.11-slim and not python:3.11-alpine ──────────────────────────
# Alpine Linux uses musl libc instead of GNU libc (glibc)
# Some Python packages (numpy, scikit-learn) have C extensions compiled
# against glibc — they either fail to install or behave differently on Alpine
# slim uses glibc, is still small (150MB vs Alpine's 50MB), and is safe
FROM python:3.11-slim

# Set working directory inside the container
# All subsequent commands run from here
# Why /app: convention. Could be anything but /app is standard
WORKDIR /app

# ── Why copy requirements.txt BEFORE app.py ───────────────────────────────────
# Docker builds in layers. Each instruction is a layer.
# If requirements.txt hasn't changed, Docker reuses the cached pip install layer
# This makes rebuilds fast — pip install only runs when requirements change
# If you copied everything at once, any code change would trigger pip install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --no-cache-dir: don't cache pip downloads inside the image (keeps image smaller)

# Now copy the actual application code
COPY app.py .

# ── Security: don't run as root ───────────────────────────────────────────────
# By default Docker runs processes as root inside containers
# If an attacker gets code execution inside your container, root = game over
# Running as a non-root user limits the blast radius
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# Document which port the app uses
# This doesn't actually open the port — docker-compose/K8s does that
# It's documentation for humans reading the Dockerfile
EXPOSE 5000

# ── Why gunicorn instead of python app.py ─────────────────────────────────────
# Flask dev server: single threaded, not production safe, prints warnings
# Gunicorn: production WSGI server, handles concurrent requests, used everywhere
# --workers 2: 2 worker processes (rule of thumb: 2 * CPU cores + 1)
# --bind 0.0.0.0:5000: listen on all interfaces so Docker can route to it
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "30", "app:app"]
