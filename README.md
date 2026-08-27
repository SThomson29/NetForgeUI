# NetForgeUI

A web-based frontend for [NetForge](https://github.com/SThomson29/NetForge) — the Ansible-based AOS-CX switch configuration generator. NetForgeUI provides project-based workspaces, a browser-based host variable editor, IP pool management, and a config generation interface that runs against a shared clone of the NetForge repo.

---

## Features

- **Projects** — isolated workspaces per network build, each with their own hosts, variables, and generated configs
- **IP pool management** — define unique, point-to-point (/31), and VLAN supernet pools per project with automatic allocation tracking across switches
- **Host variable editor** — browser-based React form covering all AOS-CX template sections with per-section include/exclude toggles
- **Config generation** — runs Ansible in an ephemeral container against the project workspace, with optional host and section filtering
- **Skeleton sync** — existing projects can pick up host_vars files added to NetForge after the project was created
- **Common infrastructure** — per-project DNS, NTP, DHCP, RADIUS, and syslog server lists that feed into editor field suggestions
- **Deployment** — push generated configs to live switches over SSH, with a diff-first dry run and automatic on-box rollback if a push breaks connectivity
- **Multi-user** — per-user data isolation with an admin panel for user management

---

## How generation works

Worth understanding before deploying, because it shapes the requirements.

NetForgeUI does **not** run `ansible-playbook` inside its own container. When you click Generate, it uses the Docker SDK to start a short-lived `cytopia/ansible` container, mounts the repo and data volumes into it by name, runs the playbook, and removes the container afterwards.

This means:

- The Docker socket must be mounted into the NetForgeUI container (`/var/run/docker.sock`)
- The `netforgeui_repo` and `netforgeui_data` volumes must exist under those exact names, since the ephemeral container mounts them by name — if a volume of that name does not exist, Docker silently creates an empty one and generation fails with a missing-playbook error
- The first generation on a new host pulls `cytopia/ansible`, which takes a moment

Both NetForgeUI and `cytopia/ansible` are published for `linux/amd64` and `linux/arm64`, so this works on Apple Silicon as well as x86.

---

## Requirements

- Docker and Docker Compose
- Access to the Docker socket on the host
- Network access to clone the NetForge repo

---

## Quick Start

### 1. Configure docker-compose.yml

Set the two required environment variables:

```yaml
SECRET_KEY: "change-me-to-a-long-random-string"
CONFIGGEN_REPO_URL: "https://github.com/SThomson29/NetForge.git"
```

Generate a secure secret key with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Start the container

```bash
docker compose up -d
```

The NetForge repo is cloned automatically on first boot. Check it worked:

```bash
docker logs netforge-ui | grep '\[repo\]'
```

You want `[repo] Clone successful`. If the clone is interrupted part-way, the
directory is cleared and re-cloned on the next start rather than wedging.

### 3. First login

Browse to `http://yourhost:5001` and log in with:

- Username: `admin`
- Password: `changeme123` (or whatever you set `ADMIN_PASSWORD` to)

Go to **Account** and change the admin password immediately.

---

## Running the published image

The release workflow publishes a multi-architecture image to GHCR on every
`v*.*.*` tag. To run it without building locally, use the supplied
`docker-compose.ghcr.yml`:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Docker selects the right architecture automatically. To pin a version rather
than tracking `latest`:

```bash
NETFORGEUI_TAG=1.4.2 docker compose -f docker-compose.ghcr.yml up -d
```

`docker-compose.yml` builds from source and is the one to use when developing.

To confirm an image really is multi-arch:

```bash
docker buildx imagetools inspect ghcr.io/sthomson29/netforge-ui:latest
```

---

## docker-compose.yml

```yaml
services:
  netforgeui:
    build: .
    container_name: netforge-ui
    ports:
      - "5001:5000"
    environment:
      SECRET_KEY: "change-me-to-a-long-random-string"
      CONFIGGEN_REPO_URL: "https://github.com/SThomson29/NetForge.git"
      # ADMIN_PASSWORD: "changeme123"
      # PORT: "5000"
      # FLASK_DEBUG: "false"
      # ANSIBLE_IMAGE: "cytopia/ansible:latest"
    volumes:
      - netforgeui_data:/app/service/data
      - netforgeui_repo:/app/service/configgen
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped

volumes:
  netforgeui_data:
    name: netforgeui_data
  netforgeui_repo:
    name: netforgeui_repo
```

**Volumes:**

- `netforgeui_data` — persists all user and project data across restarts
- `netforgeui_repo` — persists the cloned NetForge repo
- `/var/run/docker.sock` — required so generation can start the Ansible container

The explicit `name:` on each volume matters. Compose would otherwise prefix them
with the project directory name, and the ephemeral Ansible container looks them
up by their bare names.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | `change-me-in-production` | Flask session secret — use a long random string |
| `CONFIGGEN_REPO_URL` | Yes | — | Clone URL of your NetForge repo (HTTPS or SSH) |
| `ADMIN_PASSWORD` | No | `changeme123` | Initial admin password, applied only on first run |
| `CONFIGGEN_REPO_PATH` | No | `/app/service/configgen` | Where the NetForge repo is cloned inside the container |
| `ANSIBLE_IMAGE` | No | `cytopia/ansible:latest` | Image used for the ephemeral generation container |
| `REPO_VOLUME_NAME` | No | `netforgeui_repo` | Name of the repo volume to mount into the Ansible container |
| `DATA_VOLUME_NAME` | No | `netforgeui_data` | Name of the data volume to mount into the Ansible container |
| `SSH_KEY_PATH` | No | `/root/.ssh/id_rsa` | Deploy key used for git over SSH |
| `PORT` | No | `5000` | Port the Flask app listens on inside the container |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode — never use in production |
| `FLASK_PROXY_FIX` | No | `false` | Set to `true` when running behind a reverse proxy |

`REPO_VOLUME_NAME` and `DATA_VOLUME_NAME` only need setting if your volumes are
named something other than the defaults — for example when Compose has applied a
project prefix.

---

## Reverse Proxy

NetForgeUI is designed to sit behind a reverse proxy for TLS termination. Set `FLASK_PROXY_FIX=true` when doing so. Do not expose the published port directly.

---

## User Workflow

### 1. Create a project

Go to **Projects** → **New project**. Each project has its own switches, variables, IP pools, and generated configs, completely isolated from other projects.

### 2. Define IP pools (optional)

Go to the project → **Resources** → **Pools**. Three pool types are supported:

- **Unique** — one IP per allocation, for loopbacks and VTEPs (e.g. `10.255.0.0/24`, prefix `/32`)
- **Point-to-point** — /31 pairs with automatic peer end tracking
- **VLAN supernet** — a supernet pre-carved into equal subnets for SVI addressing (e.g. `10.100.0.0/16` → 256 × `/24`)

All IP fields fall back to free-text entry if no pools are defined.

### 3. Configure common infrastructure (optional)

Go to **Resources** → **Common Infrastructure**. Add project-wide DNS, NTP, DHCP, RADIUS, and syslog servers. These appear as autocomplete suggestions in the relevant editor fields.

### 4. Add switches

Go to **Hosts** → add a switch with its hostname and stacking type (None / VSX / VSF). Host variable files are scaffolded automatically from the NetForge skeleton, with VSX and VSF files included only where relevant.

### 5. Edit host variables

Go to **Editor** → select a switch. Fill in the tabs:

- **Device** — hostname, platform, profile, NTP, DNS, timezone
- **Management** — management VRF, source interface, local users
- **SNMP** — v2c community or v3 users
- **Logging & Monitoring** — syslog server and severity, sFlow collector and agent IP
- **AAA** — RADIUS servers, dynamic authorisation
- **VRFs / VLANs** — VRFs and VLANs for the switch
- **Static Routes** — static routing entries
- **Interfaces** — physical, LAG, loopback, and VLAN interfaces. Routed IP fields show a pool picker when pools are defined; SVI fields show a subnet picker from VLAN supernet pools
- **Routing** — OSPF instances (multiple supported, VRF-scoped), iBGP neighbours
- **VXLAN** — VTEP loopback, VNI map
- **Stacking** — VSX or VSF configuration

Use the include/exclude toggle on each section to control which files are saved. Click **Save** — IP allocations are tracked automatically.

Some fields are validated at save time, where an incomplete value would produce
invalid CLI: a syslog server requires a severity, and an sFlow collector requires
an agent IP.

MTU is not offered on LAGs or authenticated ports, as it is not applicable to
either on AOS-CX.

### 6. Generate configs

Go to **Generate**. Optionally filter by switch or by section. Section filters map to Ansible tags, so selecting **Syslog** generates only that part of the config. Click **Generate** — download the resulting `.ios` files individually or as a zip.

---

## Updating NetForge Templates

The NetForge repo is pulled automatically each time the container restarts. For a mid-session refresh without restarting, go to **Admin** → **Pull latest**.

Pulling updates the templates and the skeleton, but does not touch existing
`host_vars` files. New switches added after a pull pick up the new skeleton
straight away; existing switches do not.

### Bringing existing projects up to date

When NetForge gains a new host_vars file — as it did with `syslog.yml` and
`sflow.yml` — projects created before that point will not have it, and the
corresponding editor fields will have nothing to write to.

Go to the project → **Hosts** → **Sync to skeleton**:

1. **Check for missing files** lists what would be added, per host. Nothing is written.
2. **Add them** copies the missing files across.

This is additive only. Existing files are never modified or removed, so values
and comments are preserved, and running it twice does nothing the second time.

It handles whole missing files. If a new *key* is added to an existing skeleton
file, that is not detected — add it by hand.

---

## Development

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -q
python run.py
```

Running `run.py` directly clones the NetForge repo into `./configgen`. That
directory is excluded from the Docker build context via `.dockerignore` — if it
were copied into the image it would seed the repo volume and break the
bootstrap clone.

---

## Deploying to switches

Generated configs can be pushed to live switches over SSH from the project
**Deploy** page. Full detail is in [DEPLOY_README.md](DEPLOY_README.md); the
short version:

1. **Mapping** — build a hostname → management IP table for the project, optionally auto-populated from the project's hosts. Saved as `deploy_mapping.yml`.
2. **Dry run** — enter switch credentials (prompted each time, never stored), then diff the generated config against each switch's running config with `--check --diff`.
3. **Deploy** — after reviewing the diffs, push one switch at a time.

Each push is protected by a dead-man's switch: NetForgeUI takes an on-box
checkpoint, schedules a rollback job before pushing, then cancels that job and
saves to startup-config only once the switch is confirmed reachable. If the push
breaks connectivity, the rollback fires on the switch itself once the timer
expires.

---
