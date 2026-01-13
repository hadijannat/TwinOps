FROM python:3.11-slim

LABEL maintainer="RWTH Aachen University - Chair of Information and Automation Systems"
LABEL description="TwinOps Operation Service"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Create non-root user
RUN useradd -m -s /bin/bash opservice && \
    chown -R opservice:opservice /app

USER opservice

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8087/health || exit 1

EXPOSE 8087

# Default command
CMD ["python", "-m", "twinops.opservice.main"]
