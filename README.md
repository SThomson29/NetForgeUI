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
- **Firmware** — upload AOS-CX images from the browser, check what each switch runs, then install an image to a chosen partition and boot to it, with optional permission for non-failsafe subcomponent updates
- **Multi-user** — per-user data isolation with an admin panel for user management
- **Shared projects** — share a project with users or SSO groups at view, edit or deploy level, with protection against two people overwriting each other
- **Single sign-on** — optional OpenID Connect sign-in (Entra ID, Okta, Keycloak, ...), limited to a group, with admins by group
- **Project checks** — cross-switch mistakes (duplicate addresses, mismatched VSX pairs, undefined VLANs and VRFs, stale configs) flagged as warnings
- **Search** — find where an IP, subnet, VLAN, VNI or name is used across every project you can see, from the box in the sidebar
- **Project status at a glance** — the projects list shows configs out of date, changes not yet pushed and the last push for each project

---

## How Ansible runs

Worth understanding before deploying, because it shapes the requirements.

There are two execution paths.

**Generation** does **not** run `ansible-playbook` inside its own container. When you click Generate, it uses the Docker SDK to start a short-lived `cytopia/ansible` container, mounts the repo and data volumes into it by name, runs the playbook, and removes the container afterwards.

This means:

- The Docker socket must be mounted into the NetForgeUI container (`/var/run/docker.sock`)
- The `netforgeui_repo` and `netforgeui_data` volumes must exist under those exact names, since the ephemeral container mounts them by name — if a volume of that name does not exist, Docker silently creates an empty one and generation fails with a missing-playbook error
- The first generation on a new host pulls `cytopia/ansible`, which takes a moment

Both NetForgeUI and `cytopia/ansible` are published for `linux/amd64` and `linux/arm64`, so this works on Apple Silicon as well as x86.

**Deploy and firmware** run `ansible-playbook` directly inside the NetForgeUI
container, which is why `ansible-core`, `pyaoscx` and the `arubanetworks.aoscx`
collection are installed in the image. The sandbox exists to contain
user-authored Jinja during generation; deploy and firmware render nothing and
need network access to the switches, so the same isolation would only get in
the way.

Firmware runs as a background job rather than a synchronous request — an
upload, boot and reconnect can take 15–20 minutes — so the page polls for
progress and survives a refresh.

---

## Requirements

- Docker and Docker Compose
- Access to the Docker socket on the host
- Network access to clone the NetForge repo
- Network reachability to the switches, for deploy and firmware

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
      - ./firmware:/app/service/firmware
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
- `./firmware` — AOS-CX `.swi` images for the Firmware tab; read-write so admins can upload them

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
| `GENERATE_TIMEOUT_SECS` | No | `1800` | A generation run longer than this is killed and reported as failed |
| `GENERATE_MEM_LIMIT` | No | `2g` | Memory cap for the generation container (Docker size syntax) |
| `GENERATE_PIDS_LIMIT` | No | `512` | Process cap for the generation container |
| `SSH_KEY_PATH` | No | `/root/.ssh/id_rsa` | Deploy key used for git over SSH |
| `FIRMWARE_DIR` | No | `<app>/firmware` | Where `.swi` images are kept |
| `FIRMWARE_MAX_UPLOAD_MB` | No | `2048` | Largest image an admin can upload |
| `PORT` | No | `5000` | Port the Flask app listens on inside the container |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode — never use in production |
| `TRUSTED_PROXIES` | No | `0` | Number of reverse proxies in front (usually `1`), so the app trusts their forwarded scheme, host and client address |
| `OIDC_ISSUER` | No | — | Turns on single sign-on — see [Single sign-on](#single-sign-on) |

`REPO_VOLUME_NAME` and `DATA_VOLUME_NAME` only need setting if your volumes are
named something other than the defaults — for example when Compose has applied a
project prefix.

---

## Reverse Proxy

NetForgeUI is designed to sit behind a reverse proxy for TLS termination. Set `TRUSTED_PROXIES=1` when doing so. Do not expose the published port directly.

Firmware images are uploaded through the proxy and are typically 300–700 MB, well
over most proxies' default upload limit (Nginx: 1 MB). Raise it for NetForgeUI, for
example `client_max_body_size 2048m;` in its Nginx or SWAG site config.

---

## Single sign-on

NetForgeUI can sign people in through one OpenID Connect provider — Entra ID,
Okta, Keycloak, Authentik and others that can send group membership in a
claim (Google's own sign-in can't, so it isn't suitable). It is off unless
`OIDC_ISSUER` is set.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OIDC_ISSUER` | Yes | — | The provider's issuer URL; discovery is read from `<issuer>/.well-known/openid-configuration` |
| `OIDC_CLIENT_ID` | Yes | — | Client (application) ID registered with the provider |
| `OIDC_CLIENT_SECRET` | Yes | — | Client secret |
| `OIDC_ALLOWED_GROUP` | Yes | — | Only members of this group can sign in |
| `OIDC_ADMIN_GROUP` | No | — | Members are admins, re-checked at every sign-in |
| `OIDC_GROUPS_CLAIM` | No | `groups` | Claim that lists the user's groups |
| `OIDC_SCOPES` | No | `openid email profile` | Add a groups scope here if your provider needs one |
| `OIDC_REDIRECT_URI` | No | derived | Override the callback URL if the derived one is wrong |
| `OIDC_BUTTON_LABEL` | No | `Sign in with SSO` | Text on the login page button |
| `OIDC_SESSION_HOURS` | No | `12` | How long an SSO sign-in lasts before going back through the provider; `0` for no limit |
| `OIDC_LOGOUT` | No | `local` | `local` signs out of NetForgeUI only; `provider` also signs out of the identity provider |

The app refuses to start if `OIDC_ISSUER` is set without the other required
values, rather than run with sign-in open to anyone the provider knows.

**Registering the app with your provider**

- Application type: web, confidential client, authorization code flow (PKCE is used).
- Redirect URI: `https://<your NetForgeUI address>/login/sso/callback`. Behind a
  reverse proxy, set `TRUSTED_PROXIES=1` so the URL is built with the
  public `https` address, or set `OIDC_REDIRECT_URI` explicitly.
  **Admin → Single sign-on** shows the exact URL to register.
- Groups must be in the ID token or the userinfo response:
  - **Entra ID:** add a *groups* claim to the ID token (Token configuration).
    Entra sends group **Object IDs**, so `OIDC_ALLOWED_GROUP` and
    `OIDC_ADMIN_GROUP` are those GUIDs. Use "Groups assigned to the
    application" so users in many groups aren't cut off; if they are, sign-in
    explains why.
  - **Keycloak:** add a *Group Membership* mapper to the client; group names
    may be given with or without the leading `/`.
  - **Okta:** add a *groups* claim to the ID token with a filter matching your groups.

**How accounts work**

- A first SSO sign-in creates an account named from the email:
  `sam.thomson@corp.com` becomes `sam-thomson` (`sam-thomson-2` if taken).
  An email isn't required: without one, the provider's username
  (`preferred_username`) is used instead.
  Accounts are matched on the provider's subject ID afterwards, so a changed
  email or name doesn't create a new account.
- Removing someone from `OIDC_ADMIN_GROUP` removes their admin role at their
  next sign-in. An admin role granted in the Admin page is never removed by SSO.
- With SSO on, **password sign-in is for local admins only** — the way in if
  the provider is down or misconfigured. Keep at least one local admin.
- Someone with an existing local account keeps their projects by being linked:
  in **Admin → Users**, enter their SSO email against their account. Their next
  SSO sign-in lands in that account. If they already signed in and got a new,
  empty account, enter that account's name (or their email) instead: linking
  moves their SSO identity across and removes the empty one. For people the
  provider knows by username only, that second way is the one to use.
- Signing out ends the NetForgeUI session only, not the provider's, unless
  `OIDC_LOGOUT=provider`. Then someone who signed in by SSO is also signed out
  at the provider, which signs them out of every other app using it in that
  browser, and is sent back to the NetForgeUI login page. Register
  `https://<your NetForgeUI address>/login` with the provider as a post-logout
  redirect URI. A local admin's password sign-in only ever signs out locally.
- **Admin → Single sign-on** shows the settings in use (the client secret only
  as set or not set), tests the provider's discovery document, and lists the
  last 100 SSO sign-ins. A refusal shows the groups the provider sent, which is
  usually enough to see a wrong group name or a missing groups claim. Settings
  are changed in the environment, not here, so the secret stays out of `users.db`.
- Taking someone out of `OIDC_ALLOWED_GROUP` stops new sign-ins; a session
  already open lasts at most `OIDC_SESSION_HOURS` (12 by default), when they
  are sent back through the provider and its groups are checked again. The
  same applies to `OIDC_ADMIN_GROUP`. Deleting the account in Admin ends a
  session at once, but also removes their workspace.

---

## Shared projects

Every project has an owner, and its address includes them:
`/projects/<owner>/<project>/...`. The owner can share it from the project's
**Sharing** page with a user or, on an SSO instance, an identity provider group:

| Level | Can |
|---|---|
| View | Read everything — editor, Resources, Checks, History — and download configs |
| Edit | Also save switches, change Resources and generate |
| Deploy | Also run dry runs, pushes, firmware upgrades and reachability checks |

Only the owner can share, delete or transfer a project; admins can open any
project read-only and can transfer it. Shared projects appear under **Shared
with you** on the Projects page.

- **Two people on one switch:** a save is refused if someone else saved that
  switch since you opened it, naming who and when, rather than overwriting their
  changes. Reload to pick up theirs.
- **Jobs:** only one generation runs per project at a time, and a switch being
  pushed to or upgraded can't be pushed to or upgraded again until that finishes.
  Dry runs never wait. A running job's output is visible to whoever started it;
  everyone with access sees the result in History, with who ran it.
- **Groups** are matched against what the identity provider sent at the person's
  last SSO sign-in, so joining or leaving a group takes effect when they next sign in.
- **Transfer** moves the project, its shares and its history to another user;
  the previous owner keeps edit access. A user who owns shared projects can't be
  deleted until those are transferred.
- **Shares don't travel:** an exported project imported elsewhere belongs to whoever
  imports it.

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

## Upgrading firmware

A bench commissioning step, intended before a switch carries traffic.

An admin uploads AOS-CX `.swi` images under **Admin → Firmware images**, which
shows each image's version and SHA-256 to compare with the checksum HPE
publishes. Images copied into `./firmware` on the host are listed too. Then go to
the project → **Firmware**:

1. Pick an image, then **Check versions**: it reads each switch's running version
   over REST and unticks switches already on that version or of a different
   platform (an image's two-letter prefix, e.g. `FL`, must match the switch's),
   and sets the target partition to the one the switches aren't running from
2. Check the target partition (default `secondary` — upload to the partition the switch is *not* running)
3. Optionally tick **Allow non-failsafe updates** and set a window in minutes
4. Select the switches — the list and management IPs come from the Deploy tab mapping
5. Enter switch credentials, which are used for the check and the run only and never stored

Output streams live and the page can be left or refreshed; per-host results
appear when it finishes.

**Non-failsafe updates.** Some releases include firmware for programmable
subcomponents that will not apply without explicit permission. Enabling it
means the switch may reboot several times — **power must not be interrupted**
until it has fully returned. The command has no REST equivalent, so it is
issued over SSH, and it was renamed in 10.15.1010; the playbook detects the
running release and uses the right one.

Firmware needs both REST (`https-server`, for upload and boot) and SSH (for the
non-failsafe command) reachable on the management IP.

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
