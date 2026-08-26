FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/service

# Install Python dependencies
# The pip cache is scoped per target platform — a shared cache across
# architectures can serve the wrong wheels during a multi-arch build.
ARG TARGETPLATFORM
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-${TARGETPLATFORM} \
    pip install -r requirements.txt

# Copy service source
COPY . .

# Create directories that will be used at runtime
RUN mkdir -p data/users data configgen

# SSH directory setup — key is mounted at runtime, not baked in
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

EXPOSE 5000

ENV FLASK_DEBUG=false
ENV PORT=5000

# Run bootstrap tasks (admin user, repo sync, orphan cleanup) then start Gunicorn
CMD ["sh", "-c", "python run.py --bootstrap && gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120 'app:create_app()'"]
