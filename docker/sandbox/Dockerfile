FROM python:3.14-slim

LABEL maintainer="RWTH Aachen University - Chair of Information and Automation Systems"
LABEL description="TwinOps Sandbox AAS Server"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY README.md ./
COPY src/ ./src/
COPY models/ ./models/

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Create non-root user
RUN useradd -m -s /bin/bash sandbox && \
    chown -R sandbox:sandbox /app

USER sandbox

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8081/health || exit 1

EXPOSE 8081

# Default command
CMD ["python", "-m", "twinops.sandbox.main"]
