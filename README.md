# NetForgeUI

A web-based interface for generating AOS-CX switch configurations. Provides
per-user workspaces, a browser-based variable editor, and a config generation
interface powered by the
[NetForge](https://github.com/SThomson29/NetForge)
Ansible project.

---

## How It Works

The service runs as a Docker container alongside a read-only clone of the
NetForge repo. Users log in, add their switches, fill in variable
tabs in the editor, and generate `.ios` config files which can be downloaded
directly from the browser.

Each user has a completely isolated workspace — they only see their own hosts
and generated configs. A shared admin account manages the config repo and user
accounts.

---

## Quick Start

### 1. Generate a deploy key

The service clones the NetForge repo using an SSH deploy key. Generate
one in the same directory as your `docker-compose.yml`:

```bash
ssh-keygen -t ed25519 -f ./deploy_key -N ""
```

This creates two files:
- `deploy_key` — private key, mounted into the container
- `deploy_key.pub` — public key, added to GitHub

### 2. Add the public key to GitHub

1. Go to your **NetForge** repo on GitHub
2. Settings → Deploy keys → Add deploy key
3. Paste the contents of `deploy_key.pub`
4. Leave **Allow write access** unchecked
5. Click Add key

### 3. Configure docker-compose.yml

Edit the environment variables in `docker-compose.yml`:

```yaml
SECRET_KEY: "change-me-to-a-long-random-string"
CONFIGGEN_REPO_URL: "https://github.com/SThomson29/NetForge.git"
```

Generate a secure secret key with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 4. Start the container

```bash
docker compose up -d
```

### 5. First login

Browse to `http://yourhost:5000` and log in with:
- Username: `admin`
- Password: `changeme123` (or whatever you set `ADMIN_PASSWORD` to)

Go to **Admin** → **Pull** to clone the NetForge repo, then go to
**Account** and change the admin password.

---

## docker-compose.yml

```yaml
services:
  netforge-ui:
    build: .
    container_name: netforge-ui
    ports:
      - "5000:5000"
    environment:
      SECRET_KEY: "change-me-to-a-long-random-string"
      CONFIGGEN_REPO_URL: "https://github.com/SThomson29/NetForge.git"
      # ADMIN_PASSWORD: "changeme123"
      # PORT: "5000"
      # FLASK_DEBUG: "false"
      # SSH_KEY_PATH: "/root/.ssh/id_rsa"
      # ANSIBLE_PLAYBOOK: "ansible-playbook"
    volumes:
      - netforge_data:/app/service/data
      - netforge_repo:/app/service/configgen
    restart: unless-stopped

volumes:
  netforge_data:
  netforge_repo:
```

**Volumes:**

- `netforge_data` — persists all user workspaces, hosts and generated configs across container restarts
- `netforge_repo` — persists the cloned NetForge repo across restarts so it doesn't need to be re-cloned on every start

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | `change-me-in-production` | Flask session secret — use a long random string |
| `CONFIGGEN_REPO_URL` | Yes | — | SSH URL of your NetForge repo |
| `ADMIN_PASSWORD` | No | `changeme123` | Initial admin password, only applied on first run |
| `CONFIGGEN_REPO_PATH` | No | `/app/service/configgen` | Where the config repo is cloned inside the container |
| `ANSIBLE_PLAYBOOK` | No | `ansible-playbook` | Path to the ansible-playbook binary |
| `PORT` | No | `5000` | Port the Flask app listens on |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode — development only |
| `FLASK_PROXY_FIX` | No | `false` | Set to `true` when running behind a reverse proxy |

---

## Reverse Proxy

The container listens on port `5000` and is designed to sit behind a reverse
proxy for TLS termination. Set `FLASK_PROXY_FIX=true` in the environment when
doing so. Do not expose port `5000` directly to the internet.

---

## Updating the Config Repo

When templates or skeletons are updated in the NetForge repo:

1. Log in as an admin
2. Go to **Admin** → click **Pull latest**

This pulls the latest commit. Existing user host_vars files are not affected —
only newly scaffolded switches will use updated skeletons.

---

## User Workflow

1. **Hosts** — add a switch with its hostname and stacking type
2. **Editor** — select the switch and fill in the variable tabs
3. Use the include/exclude toggles to control which files get saved
4. **Save** — writes the variables to your workspace
5. **Generate** — optionally select specific sections, then generate
6. Download the resulting `.ios` config file(s) individually or as a zip
