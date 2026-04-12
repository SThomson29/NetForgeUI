# Deploy Feature — Integration Guide

## Overview

Adds a **Deploy** tab to each project in NetForgeUI. Allows pushing generated
switch configurations to live AOS-CX switches with checkpoint rollback
protection.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  NetForgeUI (Flask)                                     │
│                                                         │
│  Deploy tab                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Mapping   │→ │ Dry Run  │→ │ Deploy + Confirm     │  │
│  │ Editor    │  │ (diffs)  │  │ (checkpoint/rollback) │  │
│  └──────────┘  └──────────┘  └──────────────────────┘  │
│       │              │              │                    │
│       ▼              ▼              ▼                    │
│  deploy_mapping.yml  ansible-playbook (check mode)      │
│                      ansible-playbook (deploy mode)     │
└─────────────────────┬───────────────────────────────────┘
                      │  SSH (network_cli)
                      ▼
              ┌───────────────┐
              │  AOS-CX       │
              │  Switches      │
              │  (port 22)    │
              └───────────────┘
```

## Deployment Flow

### Step 1 — Mapping
User edits a table of hostname → management IP. Can auto-populate from
project hosts. Saved as `deploy_mapping.yml` per project.

### Step 2 — Dry Run
1. User enters credentials (prompted, not stored)
2. Flask builds dynamic Ansible inventory from mapping + creds
3. Runs `deploy_dryrun.yml` with `--check --diff`
4. Playbook connects to each switch via SSH, diffs generated config
   against running config
5. Results shown per-host with syntax-highlighted diff

### Step 3 — Deploy
1. User reviews diffs, clicks Confirm & Deploy
2. Flask runs `deploy_push.yml` with `serial: 1` (one switch at a time)
3. Per switch:
   a. Creates checkpoint: `copy running-config checkpoint netforge-pre-<timestamp>`
   b. Schedules rollback job: `job netforge-rollback delay <N> "copy checkpoint ... running-config"`
   c. Pushes generated config via `aoscx_config`
   d. Waits 5s, then verifies connectivity
   e. If reachable: cancels rollback job + `copy running-config startup-config`
   f. If unreachable: rollback fires on-box after timer expires
4. Results shown per-host with status badges

## Prerequisites

### Switch-side
- SSH enabled (default on AOS-CX): `ssh server vrf mgmt`
- User account with config access
- Job scheduler available (for rollback timer)

### Container / server-side
Add to requirements.txt (or the ephemeral container image):
```
pyaoscx>=2.6.0
```

Install the Ansible collection:
```bash
ansible-galaxy collection install arubanetworks.aoscx
ansible-galaxy collection install ansible.netcommon
```

If using the Docker ephemeral execution model, add this to the
ephemeral container Dockerfile:
```dockerfile
RUN pip install --no-cache-dir pyaoscx>=2.6.0 && \
    ansible-galaxy collection install arubanetworks.aoscx ansible.netcommon
```

## Integration Steps

### 1. Add playbooks to NetForge repo

Copy into your NetForge (configgen) repo:
```
playbooks/
  deploy_dryrun.yml
  deploy_push.yml
```

### 2. Add template

Copy to `app/templates/`:
```
project_deploy.html
```

### 3. Add routes

Either merge the functions from `deploy_routes.py` into your existing
`projects.py` blueprint, or import them:

```python
# In app/projects.py or app/__init__.py

from app.deploy_routes import (
    project_deploy,
    deploy_save_mapping,
    deploy_auto_populate,
    deploy_dryrun,
    deploy_push,
)

projects_bp.add_url_rule(
    '/<project_name>/deploy',
    'project_deploy',
    project_deploy
)
projects_bp.add_url_rule(
    '/<project_name>/deploy/save-mapping',
    'deploy_save_mapping',
    deploy_save_mapping,
    methods=['POST']
)
projects_bp.add_url_rule(
    '/<project_name>/deploy/auto-populate',
    'deploy_auto_populate',
    deploy_auto_populate
)
projects_bp.add_url_rule(
    '/<project_name>/deploy/dryrun',
    'deploy_dryrun',
    deploy_dryrun,
    methods=['POST']
)
projects_bp.add_url_rule(
    '/<project_name>/deploy/push',
    'deploy_push',
    deploy_push,
    methods=['POST']
)
```

### 4. Add Deploy to the sidebar

In `base_project.html`, add the Deploy nav item after Resources
in the JavaScript sidebar injection:

```html
<a href="/projects/${encodeURIComponent(projectName)}/deploy"
   class="nav-item ${'{{ "active" if request.endpoint == "projects.project_deploy" else "" }}'}">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" y1="3" x2="12" y2="15"/>
  </svg>
  Deploy
</a>
```

### 5. Add PyYAML dependency (if not already present)

The deploy routes use `pyyaml` for the mapping file. This should already
be in your requirements.txt from the existing config generation work.

## Credential Handling

Credentials are:
- Prompted via a modal when the user triggers dry-run or deploy
- Held in a JavaScript variable (`sessionCreds`) for the page session
- Passed to the Flask backend in the POST body over HTTPS
- Injected into a temporary Ansible inventory file
- Inventory file is written to a `tempfile.TemporaryDirectory` which
  is automatically cleaned up after the playbook completes
- Never written to disk permanently, never stored in the database

The credentials are valid for the browser tab session only. Refreshing
the page clears them.

## Rollback Behaviour

The rollback uses the AOS-CX `job` scheduler as a dead-man's switch:

1. Before config push, a delayed job is scheduled on the switch:
   ```
   job netforge-rollback delay 300 "copy checkpoint netforge-pre-... running-config"
   ```

2. If the config push succeeds and the switch remains reachable:
   - The job is cancelled: `no job netforge-rollback`
   - Running config is saved: `copy running-config startup-config`

3. If the switch becomes unreachable after the push:
   - The Ansible play cannot reach it to cancel the job
   - After the timeout (default 5 mins), the switch executes the job
   - Running config is reverted to the pre-change checkpoint
   - The switch is back to its previous state

This means the switch protects itself — no external intervention needed.

## File Structure

```
deploy-feature/
├── playbooks/
│   ├── deploy_dryrun.yml        # Ansible: check mode + diff
│   └── deploy_push.yml          # Ansible: checkpoint → push → confirm
├── templates/
│   └── project_deploy.html      # Flask/Jinja2 template
├── routes/
│   └── deploy_routes.py         # Flask route handlers
├── deploy_mapping.example.yml   # Example mapping file
└── DEPLOY_README.md             # This file
```
