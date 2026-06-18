# docker-dash 🐳

A lightweight, automated dashboard generator for your self-hosted Docker stacks. It crawls your GitHub repository for compose files, queries Nginx Proxy Manager (NPM) for active domain mappings, and produces a single, beautiful self-contained HTML dashboard.

## Features

- **Automatic Stack Detection:** Dynamically crawls and parses all Compose files in your repo.
- **Reverse Proxy Mapping:** Matches containers to live NPM domain URLs, including forward host and port.
- **Service Descriptions:** Add `dash.description` and `dash.notes` labels to any service for human-readable context directly in the dashboard.
- **Environment Template Parsing:** Documents expected variables from `.env.example` files, auto-masking secrets.
- **Security Auditing:** Flags privileged containers, `cap_add` capabilities, missing `container_name`, `network_mode: host`, and services with no restart policy.
- **Interactive UI:** Searchable, filterable, responsive dashboard with dark mode support.

---

## Service Descriptions

Add labels to any service in your compose file to show descriptions in the dashboard:

```yaml
services:
  jellyfin:
    image: jellyfin/jellyfin:latest
    labels:
      - "dash.description=Media server — movies, shows, music"
      - "dash.notes=Hardware transcoding configured via /dev/dri"
```

Both list (`- "key=value"`) and dict (`key: value`) label formats are supported.

---

## Local Setup

1. **Install requirements:**
```bash
/opt/venv/bin/pip install pyyaml requests python-dotenv
```

2. **Configure environment variables:**
   Create a `.env` file in the root directory:
```ini
   GITHUB_REPO="https://github.com/your-username/your-repo"
   GITHUB_BRANCH="main"
   EXCLUDE_DIRS="_archive"
   OUTPUT_FILE="index.html"

   # Optional: Nginx Proxy Manager credentials
   NPM_URL="http://192.168.1.x:81"
   NPM_EMAIL="admin@example.com"
   NPM_PASSWORD="your-secure-password"
```

3. **Generate your dashboard:**
```bash
   python3 docker_dash.py
```

---

## Options

| Flag | Default | Description |
|---|---|---|
| `--env PATH` | `.env` | Path to env file |
| `--out PATH` | `OUTPUT_FILE` | Override output file path |
| `--no-npm` | — | Skip NPM API (useful if NPM is offline) |
| `--verbose` | — | Print extra debug info |

---

## Dashboard Filters

| Filter | What it shows |
|---|---|
| Open ports | Services with mapped host ports |
| Has URL | Services with an NPM domain or `APP_URL` hint |
| Internal net | Services on internal-only Docker networks |
| Rolling tag | Services using non-pinned image tags (e.g. `latest`) |
| ⚠ Security | Services with any security concern |
| Host network | Services using `network_mode: host` |
