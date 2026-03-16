FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    ansible \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/service

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source
COPY . .

# Create directories that will be used at runtime
RUN mkdir -p data/users data configgen

# SSH directory setup — key is mounted at runtime, not baked in
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

EXPOSE 5000

ENV FLASK_DEBUG=false
ENV PORT=5000

CMD ["python", "run.py"]
