# cert-manager

A bash script that automates mkcert certificate management for a homeserver environment. It handles CA creation, certificate generation for all services (as a single SAN certificate), and automatic upload to Nginx Proxy Manager via its API.

## Overview

Instead of manually running `mkcert` with a growing list of domains, editing certificates in NPM's UI, and remembering which services are covered, this script manages everything through a simple `services.list` file and a handful of commands.

One certificate is generated covering all services. When you add or remove a service, the certificate is regenerated and re-uploaded. Any NPM proxy hosts already using it pick up the change automatically.

## Prerequisites

- Debian/Ubuntu-based server
- Nginx Proxy Manager (accessible via API)
- A dedicated NPM user for API access (admin role, TOTP disabled, only "Certificates" permission set to Manage)

## Setup

```bash
mkdir -p ~/scripts/cert-manager && cd ~/scripts/cert-manager
```

Place `cert-manager.sh`, `.env`, and optionally `services.list` in this directory.

```bash
chmod +x cert-manager.sh
cp .env.example .env
nano .env  # fill in your values
```

### `.env`

```bash
# Base domain for all services
DOMAIN="homeserver"

# Where to copy CA/certs for device distribution
SHARE_DIR="/data/assets/syncthing/Shared"

# Nginx Proxy Manager API (dedicated API user, no TOTP)
NPM_URL="http://192.168.1.109:81"
NPM_EMAIL="api@homeserver.local"
NPM_PASS="your-strong-password-here"
NPM_CERT_NAME="homeserver"
```

### `services.list`

One service name per line. Created automatically on first run with sensible defaults, or create your own:

```
jellyfin
nextcloud
pihole
sonarr
radarr
```

## Usage

### First-Time Setup

```bash
./cert-manager.sh init        # install mkcert, create CA
./cert-manager.sh export-ca   # copy rootCA to shared folder for device import
./cert-manager.sh regen       # generate cert for all services, upload to NPM
./cert-manager.sh setup-cron  # optional: monthly auto-renewal
```

After `export-ca`, manually install `rootCA.pem` / `rootCA.crt` on your devices (one-time):

| Device | File | Method |
|---|---|---|
| Windows | `rootCA.pem` | MMC → Trusted Root Certification Authorities → Import |
| Android | `rootCA.crt` | Settings → Security → Install CA certificate |
| Firefox | `rootCA.pem` | Settings → Certificates → Authorities → Import |

### Day-to-Day

```bash
# Add one or more services (regenerates & uploads automatically)
./cert-manager.sh add grafana
./cert-manager.sh add immich komga homepage

# Remove a service
./cert-manager.sh remove romm

# Manually regenerate and upload
./cert-manager.sh regen

# Upload current cert to NPM without regenerating
./cert-manager.sh upload

# See what's configured
./cert-manager.sh list

# Check cert expiry, SANs, and whether services.list matches the cert
./cert-manager.sh status
```

## Commands

| Command | Description |
|---|---|
| `init` | Install mkcert and dependencies, create the CA |
| `export-ca` | Copy `rootCA.pem` and `rootCA.crt` to the shared folder |
| `add <svc> [...]` | Add service(s) to the list, regenerate cert, upload to NPM |
| `remove <svc>` | Remove a service, regenerate cert, upload to NPM |
| `regen` | Regenerate the certificate covering all services and upload |
| `upload` | Push the current certificate to NPM without regenerating |
| `list` | Display all configured services |
| `status` | Show certificate details, expiry, and coverage audit |
| `setup-cron` | Install a monthly cron job for automatic renewal |

## How It Works

```
services.list          One service name per line
       │
       ▼
   cert-manager.sh regen
       │
       ├─ Reads services.list
       ├─ Builds domain list: homeserver, svc1.homeserver, svc2.homeserver, ...
       ├─ Runs mkcert to generate a single SAN certificate
       ├─ Uploads to NPM via API (finds or creates the cert entry by name)
       └─ Copies cert files to shared folder
```

Since it always uploads to the **same certificate entry** in NPM, any proxy hosts already referencing it automatically use the updated cert. You only touch the NPM UI when creating a new proxy host (selecting the certificate from the dropdown once).

## NPM API User Setup

Create a dedicated user in NPM for the script:

1. **Users → Add User** in NPM
2. Email: `api@homeserver.local` (doesn't need to be real)
3. Password: generate with `openssl rand -base64 32`
4. Role: Administrator
5. **Do NOT enable TOTP**
6. Edit user permissions:
   - **Certificates**: Manage
   - Everything else: Hidden

## File Structure

```
~/scripts/cert-manager/
├── cert-manager.sh       # the script
├── .env                  # secrets & config (do not commit)
├── .env.example          # template
├── services.list         # one service name per line
├── README.md
└── certs/
    ├── homeserver.pem     # generated certificate
    └── homeserver-key.pem # generated private key
```
