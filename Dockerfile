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

# AOS-CX Ansible collection, pinned to match NetForge's requirements.yml.
# Installed separately from pip because it is a Galaxy collection, not a
# Python package. ANSIBLE_COLLECTIONS_PATH below must match this location.
RUN ansible-galaxy collection install arubanetworks.aoscx:4.5.1 \
    -p /usr/share/ansible/collections

# Copy service source
COPY . .

# Create directories that will be used at runtime.
# firmware/ holds .swi images for the firmware feature; it is a mount point,
# empty in the image.
RUN mkdir -p data/users data configgen firmware

# SSH directory setup — key is mounted at runtime, not baked in
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

EXPOSE 5000

ENV FLASK_DEBUG=false
ENV PORT=5000
ENV ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections

# Run bootstrap tasks (admin user, repo sync, orphan cleanup) then start Gunicorn
# --workers 1 is deliberate and must stay at 1.
#
# Generate, deploy and firmware all run as background jobs whose state lives in
# a process-local dict (_jobs in projects.py, _deploy_jobs in deploy_routes.py,
# _fw_jobs in firmware_routes.py). With more than one worker, the POST that
# starts a job and the GETs that poll it land on different processes, so
# polling 404s and the live output stops — intermittently, depending on which
# worker answers.
#
# One worker costs nothing here: the workload is I/O-bound (subprocesses,
# Docker, SSH), gthread handles concurrent requests within the process, and
# jobs run in their own threads. If genuine concurrency ever demands more
# workers, the prerequisite is shared job state (Redis or SQLite), not simply
# raising this number.
CMD ["sh", "-c", "python run.py --bootstrap && gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 120 'app:create_app()'"]
