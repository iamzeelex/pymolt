FROM python:3.6

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    make \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory to the target location
WORKDIR /workspace/tests/artifacts/flasgger

# Copy the entire flasgger source code into the image so that
# editable pip installs (-e . and -e etc/flasgger_package) work during build.
COPY artifacts/flasgger /workspace/tests/artifacts/flasgger

# Upgrade pip and install all requirements
RUN pip install -U --no-cache-dir pip && \
    pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt
