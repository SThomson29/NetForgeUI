# NetForgeUI

A web-based frontend for [NetForge](https://github.com/SThomson29/NetForge) — the Ansible-based AOS-CX switch configuration generator. NetForgeUI provides project-based workspaces, a browser-based host variable editor, IP pool management, and a config generation interface that runs against a shared read-only clone of the NetForge repo.

---

## Features

- **Projects** — isolated workspaces per network build, each with their own hosts, variables, and generated configs
- **IP pool management** — define unique, point-to-point (/31), and VLAN supernet pools per project with automatic allocation tracking across switches
- **Host variable editor** — browser-based React form covering all AOS-CX template sections with per-section include/exclude toggles
- **Config generation** — runs `ansible-playbook` against the project workspace with optional host and section filtering
- **Common infrastructure** — per-project DNS, NTP, DHCP, RADIUS, and syslog server lists that feed into editor field suggestions
- **Multi-user** — per-user data isolation with an admin panel for user management

---

## Requirements

- Docker and Docker Compose
- A GitHub account

---

## Quick Start

### 1. Configure docker-compose.yml

Set the two required environment variables:

```yaml
SECRET_KEY: "change-me-to-a-long-random-string"
CONFIGGEN_REPO_URL: "https://github.com/SThomson29/NetForge.git"
```

Alternatively generate a secure secret key with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Start the container

```bash
docker compose up -d
```

The NetForge repo is cloned automatically on first boot — check logs for `[repo] Clone successful`.

### 5. First login

Browse to `http://yourhost:5000` and log in with:
- Username: `admin`
- Password: `changeme123` (or whatever you set `ADMIN_PASSWORD` to)

Go to **Account** and change the admin password immediately.

---

## docker-compose.yml

```yaml
services:
  netforge-ui:
    image: ghcr.io/sthomson29/netforge-ui:latest
    container_name: netforge-ui
    ports:
      - "5000:5000"
    environment:
      SECRET_KEY: "change-me-to-a-long-random-string"
      CONFIGGEN_REPO_URL: "git@github.com:SThomson29/NetForge.git"
      # ADMIN_PASSWORD: "changeme123"
      # PORT: "5000"
      # FLASK_DEBUG: "false"
      # ANSIBLE_PLAYBOOK: "ansible-playbook"
    volumes:
      - netforgeui_data:/app/service/data
      - netforgeui_repo:/app/service/configgen
    restart: unless-stopped

volumes:
  netforgeui_data:
  netforgeui_repo:
```

**Volumes:**
- `netforgeui_data` — persists all user and project data across restarts
- `netforgeui_repo` — persists the cloned NetForge repo
- `./deploy_key/cx_configgen_deploy_key` — SSH deploy key mounted read-only

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | `change-me-in-production` | Flask session secret — use a long random string |
| `CONFIGGEN_REPO_URL` | Yes | — | SSH URL of your NetForge repo |
| `ADMIN_PASSWORD` | No | `changeme123` | Initial admin password, applied only on first run |
| `CONFIGGEN_REPO_PATH` | No | `/app/service/configgen` | Where the NetForge repo is cloned inside the container |
| `ANSIBLE_PLAYBOOK` | No | `ansible-playbook` | Path to the ansible-playbook binary |
| `PORT` | No | `5000` | Port the Flask app listens on |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode — never use in production |
| `FLASK_PROXY_FIX` | No | `false` | Set to `true` when running behind a reverse proxy |

---

## Reverse Proxy

NetForgeUI is designed to sit behind a reverse proxy for TLS termination. Set `FLASK_PROXY_FIX=true` when doing so. Do not expose port `5000` directly.

---

## User Workflow

### 1. Create a project

Go to **Projects** → **New project**. Each project has its own switches, variables, IP pools, and generated configs completely isolated from other projects.

### 2. Define IP pools (optional)

Go to the project → **Resources** → **Pools**. Three pool types are supported:

- **Unique** — one IP per allocation, for loopbacks and VTEPs (e.g. `10.255.0.0/24`, prefix `/32`)
- **Point-to-point** — /31 pairs with automatic peer end tracking
- **VLAN supernet** — a supernet pre-carved into equal subnets for SVI addressing (e.g. `10.100.0.0/16` → 256 × `/24`)

All IP fields fall back to free-text entry if no pools are defined.

### 3. Configure common infrastructure (optional)

Go to **Resources** → **Common Infrastructure**. Add project-wide DNS, NTP, DHCP, RADIUS, and syslog servers. These appear as autocomplete suggestions in the relevant editor fields.

### 4. Add switches

Go to **Hosts** → add a switch with its hostname and stacking type (None / VSX / VSF). Host variable files are scaffolded automatically from the NetForge skeleton.

### 5. Edit host variables

Go to **Editor** → select a switch. Fill in the tabs:

- **General** — hostname, NTP, DNS, timezone
- **Management** — management VRF, source interface, local users
- **SNMP** — v2c community or v3 users
- **AAA** — RADIUS servers, dynamic authorisation
- **VRFs / VLANs** — VRFs and VLANs for the switch
- **Interfaces** — physical, LAG, loopback, and VLAN interfaces. Routed IP fields show a pool picker when pools are defined; SVI fields show a subnet picker from VLAN supernet pools
- **Routing** — OSPF instances (multiple supported, VRF-scoped), iBGP neighbours
- **VXLAN** — VTEP loopback, VNI map
- **VSX / VSF** — stacking configuration

Use the include/exclude toggle on each tab to control which files are saved. Click **Save** — IP allocations are tracked automatically.

### 6. Generate configs

Go to **Generate**. Optionally filter by switch or section. Click **Generate** — runs `ansible-playbook` against the project workspace. Download the resulting `.ios` files individually or as a zip.

---

## Updating NetForge Templates

The NetForge repo is pulled automatically each time the container restarts. For a mid-session refresh without restarting, go to **Admin** → **Pull latest**.

Pulling does not affect existing `host_vars` files. Only newly added switches will pick up updated skeletons.

---
