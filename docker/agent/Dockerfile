FROM python:3.14-slim

LABEL maintainer="RWTH Aachen University - Chair of Information and Automation Systems"
LABEL description="TwinOps AI Agent for BaSyx Digital Twins"

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
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /app/audit_logs && \
    chown -R agent:agent /app

USER agent

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

# Default command
CMD ["python", "-m", "twinops.agent.main"]
