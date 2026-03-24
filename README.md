# Network-Tools

Web-based frontend for the [Network-ConfigGen](https://github.com/Network-Team-Repository/Network-ConfigGen)
Ansible config generation project. Provides per-user workspaces, a browser-based
host variable editor, and a config generation interface that runs against a
shared read-only clone of the config repo.

---

## Architecture

```
Host machine
├── network-tools container
│   ├── /app/service/configgen/     ← git clone of Network-ConfigGen repo (volume)
│   └── /app/service/data/users/    ← per-user workspaces (volume)
│       ├── <username>/hosts.ini
│       ├── <username>/host_vars/
│       └── <username>/generated_configs/
└── /root/.ssh/id_rsa               ← deploy key mounted into container (read-only)
```

All users share the same templates, roles, skeletons and playbooks from the
cloned config repo. User data is completely isolated — each person only sees
their own hosts and generated configs. Only admin users can pull updates to the
shared config repo.

---

## Prerequisites

- Docker on the host (managed via Terraform)
- A GitHub account with access to both this repo and the Network-ConfigGen repo
- A read-only SSH deploy key configured for the Network-ConfigGen repo
- A GitHub classic PAT with `read:packages` scope for pulling the container image

---

## Deploy Key Setup

The service uses a read-only SSH deploy key to clone and pull the
Network-ConfigGen repo. The key is mounted into the container at runtime and
never baked into the image.

### Generate the keypair

On your host machine:

```bash
ssh-keygen -t ed25519 -C "network-tools-deploy" \
  -f /home/administrator/Infra/NetworkTools/deploy_key -N ""
chmod 600 /home/administrator/Infra/NetworkTools/deploy_key
```

This creates two files:
- `deploy_key` — private key, mounted into the container
- `deploy_key.pub` — public key, added to GitHub

### Add the public key to GitHub

1. Go to your **Network-ConfigGen** repo on GitHub
2. Settings → Deploy keys → Add deploy key
3. Title: `network-tools-service`
4. Key: paste the contents of `deploy_key.pub`
5. Leave **Allow write access** unchecked — read-only is sufficient
6. Click Add key

---

## Container Image

The image is hosted on `ghcr.io` under the organisation. Pulling it requires
a GitHub classic PAT with `read:packages` scope — fine-grained tokens are not
currently supported for ghcr.io authentication.

---

## Terraform Deployment

The recommended deployment method is Terraform using the
[kreuzwerker/docker](https://registry.terraform.io/providers/kreuzwerker/docker)
provider. Add the following to your Terraform configuration:

**`provider.tf`** — add a registry auth block:
```hcl
provider "docker" {
  registry_auth {
    address  = "ghcr.io"
    username = "YOUR_GITHUB_USERNAME"
    password = var.ghcr_token
  }
}
```

**`variables.tf`** — add these variables:
```hcl
variable "ghcr_token" {
  description = "GitHub classic PAT with read:packages scope"
  type        = string
  sensitive   = true
}

variable "configgen_secret_key" {
  description = "Flask secret key — generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
  type        = string
  sensitive   = true
}

variable "configgen_admin_password" {
  description = "Initial admin password — change immediately after first login"
  type        = string
  sensitive   = true
}
```

**`network-tools.tf`**:
```hcl
data "docker_registry_image" "network_tools" {
  name = "ghcr.io/Network-Team-Repository/network-tools:1.0.0" # renovate
}

resource "docker_image" "network_tools" {
  name          = data.docker_registry_image.network_tools.name
  pull_triggers = [data.docker_registry_image.network_tools.sha256_digest]
}

resource "docker_volume" "network_tools_data" {
  name = "network-tools_data"
}

resource "docker_volume" "network_tools_repo" {
  name = "network-tools_repo"
}

resource "docker_container" "network_tools" {
  name    = "network-tools"
  image   = docker_image.network_tools.image_id
  restart = "always"

  network_mode = "host"

  volumes {
    volume_name    = docker_volume.network_tools_data.name
    container_path = "/app/service/data"
  }

  volumes {
    volume_name    = docker_volume.network_tools_repo.name
    container_path = "/app/service/configgen"
  }

  volumes {
    host_path      = "/home/administrator/Infra/NetworkTools/deploy_key"
    container_path = "/root/.ssh/id_rsa"
    read_only      = true
  }

  env = [
    "SECRET_KEY=${var.configgen_secret_key}",
    "CONFIGGEN_REPO_URL=git@github.com:Network-Team-Repository/Network-Tools.git",
    "ADMIN_PASSWORD=${var.configgen_admin_password}",
    "FLASK_PROXY_FIX=true",
  ]

  log_driver = "json-file"
  log_opts = {
    max-size = "10m"
    max-file = "3"
  }
}
```

---

## Reverse Proxy

The service is designed to sit behind a reverse proxy (SWAG/nginx) which
handles TLS termination. A SWAG subdomain config is provided in
`network-tools.subdomain.conf` — place it in your SWAG
`nginx/proxy-confs/` directory.

The container must be on the same Docker network as your SWAG container.
Set `FLASK_PROXY_FIX=true` in the container env so Flask correctly handles
forwarded headers from the proxy.

Do not expose port `5000` directly to the internet.

---

## First Run

1. Deploy the container via Terraform
2. Browse to `http://yourhost:5000` (or via your reverse proxy)
3. Log in with username `admin` and the password set in `ADMIN_PASSWORD`
   (default: `changeme123`)
4. Go to **Admin** → click **Pull** to clone the Network-ConfigGen repo
5. Go to **Account** and change the admin password immediately
6. Create user accounts for your team via the Admin panel

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | `change-me-in-production` | Flask session secret — use a long random string |
| `CONFIGGEN_REPO_URL` | Yes | — | SSH URL of the Network-ConfigGen repo |
| `ADMIN_PASSWORD` | No | `changeme123` | Initial admin password, only applied on first run |
| `SSH_KEY_PATH` | No | `/root/.ssh/id_rsa` | Path to the deploy key inside the container |
| `CONFIGGEN_REPO_PATH` | No | `/app/service/configgen` | Where the config repo is cloned inside the container |
| `ANSIBLE_PLAYBOOK` | No | `ansible-playbook` | Path to the ansible-playbook binary |
| `PORT` | No | `5000` | Port the Flask app listens on |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode — development only |
| `FLASK_PROXY_FIX` | No | `false` | Set to `true` when running behind a reverse proxy |

---

## Releasing a New Version

```bash
git tag v1.0.1
git push origin v1.0.1
```

GitHub Actions builds the image and pushes three tags:
- `ghcr.io/Network-Team-Repository/network-tools:1.0.1` — exact version (pin to this in Terraform)
- `ghcr.io/Network-Team-Repository/network-tools:1.0` — minor floating tag
- `ghcr.io/Network-Team-Repository/network-tools:1` — major floating tag

Update the image tag in `network-tools.tf` and apply via Terraform.

---

## Updating the Config Repo Templates

When templates, skeletons or roles are updated in the Network-ConfigGen repo:

1. Log in as an admin
2. Go to **Admin**
3. Click **Pull latest** — the service pulls the latest commit from main
4. All users' next generate run will use the updated templates immediately

Pulling does not affect existing `host_vars` files in user workspaces. Only
newly scaffolded switches added after the pull will use updated skeletons.

---

## User Workflow

1. Go to **Hosts** → add a switch (provide hostname and stacking type)
2. Go to **Editor** → select the switch and fill in the variable tabs
3. Use the include/exclude toggles on each tab to control which files get saved
4. Click **Save** — variables are written to the user's workspace
5. Go to **Generate** → optionally select specific sections via the checkboxes
6. Click **Generate** — runs the Ansible playbook against the user's workspace
7. Download the resulting `.ios` file(s) individually or as a zip
