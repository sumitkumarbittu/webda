# Use Python 3.11 slim image for smaller size
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PROCESS_INTERVAL_SECONDS=60 \
    CONTAINER_MAX=10000 \
    LOG_LEVEL=INFO

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user and set up directory structure
RUN useradd -m -s /bin/bash app && \
    mkdir -p /app/instance && \
    chown -R app:app /app

WORKDIR /app

# Copy requirements first for better caching
COPY --chown=app:app requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=app:app . .

# Switch to non-root user
USER app

# Health check with better error reporting
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Expose port
EXPOSE 8000

# Debug info
RUN echo "Current directory: $(pwd)" && \
    echo "Files in directory:" && \
    ls -la && \
    echo "Python version:" && \
    python --version && \
    echo "Pip list:" && \
    pip list

# Run the application with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "--chdir", "/app", "app:app"]