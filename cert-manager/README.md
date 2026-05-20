# cert-manager

A bash script that automates **SSL management** (via `mkcert`) and **Reverse Proxy provisioning** for a local homeserver environment. It handles CA distribution, multi-SAN certificate generation, and automatic deployment of both certificates and proxy hosts to Nginx Proxy Manager (NPM) via its API.

## Overview

This script eliminates the manual "click-ops" of setting up new services. It manages a `services.list` and communicates with the NPM API to:

1.  **Manage Identity:** Generate one master certificate covering all your subdomains (e.g., `jellyfin.homeserver`, `nextcloud.homeserver`).
2.  **Manage Routing:** Automatically create or delete Proxy Host entries in NPM with optimized defaults (Force SSL, HSTS, WebSockets).
3.  **Keep in Sync:** When you add or remove a service, the certificate is regenerated, re-uploaded, and the NPM routing is updated instantly.

## Prerequisites

- Debian/Ubuntu-based server
- Nginx Proxy Manager (accessible via API)
- A dedicated NPM user for API access (Administrator role, TOTP disabled)
- `jq`, `curl`, `openssl`, and `mkcert` (installed automatically via `init`)

### `.env`

Create a `.env` file in the same directory:

```bash
# Base domain for all services
DOMAIN="homeserver"

# Where to copy CA/certs for device distribution (e.g., a Syncthing folder)
SHARE_DIR="/data/assets/syncthing/Shared"

# Nginx Proxy Manager API
NPM_URL="http://192.168.1.109:81"
NPM_EMAIL="api@homeserver.local"
NPM_PASS="your-strong-password-here"
NPM_CERT_NAME="homeserver"
```

---

## Usage

### First-Time Setup

```bash
./cert-manager.sh init        # Install dependencies, create CA
./cert-manager.sh export-ca   # Copy rootCA to shared folder for device import
./cert-manager.sh regen       # Generate initial cert and upload to NPM
./cert-manager.sh setup-cron  # Install monthly auto-renewal cron job
```

**Note:** After `export-ca`, you must manually install the `rootCA.pem` (Windows/Firefox) or `rootCA.crt` (Android) on your devices once to trust your homeserver domains.

### Day-to-Day: Provisioning Services

**1. Add a service with automatic NPM routing:**
This adds the domain to the SSL certificate **and** creates the Proxy Host entry in NPM.
```bash
# Syntax: ./cert-manager.sh add <service_name> <internal_ip> <port> [scheme]
./cert-manager.sh add jellyfin 192.168.1.50 8096
./cert-manager.sh add pihole 192.168.1.10 80
```

**2. Add a service (SSL only):**
If you only want the domain on the certificate but prefer to configure the NPM proxy host manually.
```bash
./cert-manager.sh add custom-app
```

**3. Remove a service:**
This **deletes the Proxy Host from NPM**, removes the domain from the certificate list, and regenerates the smaller cert.
```bash
./cert-manager.sh remove jellyfin
```

### Maintenance & Audit

```bash
./cert-manager.sh list     # Show all subdomains currently in the list
./cert-manager.sh status   # Show cert expiry, SANs, and check if NPM matches the list
./cert-manager.sh regen    # Force a certificate regeneration and upload
```

---

## NPM API User Setup

For the script to manage both SSL and Routing, the API user needs these specific permissions:

1.  **Users → Add User** in NPM.
2.  Role: **Administrator**.
3.  **Do NOT enable TOTP** for this specific user.
4.  **Edit Permissions**:
    *   **Item Visibility**: `All Items`
    *   **Certificates**: `Manage`
    *   **Proxy Hosts**: `Manage`
    *   *Everything else can be set to Hidden.*

---

## How It Works

```text
 User Command: ./cert-manager.sh add jellyfin 192.168.1.50 8096
       │
       ▼
 1. Update services.list ──► Adds "jellyfin" to the local manifest.
       │
 2. Generate SSL ──────────► mkcert creates a single SAN cert for:
       │                     homeserver, jellyfin.homeserver, etc.
       │
 3. Sync with NPM ─────────► Uploads the new .pem and .key via NPM API.
       │
 4. Provision Route ───────► Creates/Updates Proxy Host in NPM:
                             jellyfin.homeserver ➔ http://192.168.1.50:8096
                             (Auto-enables Force SSL, HSTS, and Websockets)
       │
 5. Sync Files ────────────► Copies certs to SHARE_DIR for client distribution.
```

## File Structure

```text
~/scripts/cert-manager/
├── cert-manager.sh       # The automation script
├── .env                  # Private configuration (NPM credentials)
├── services.list         # Plain text list of managed subdomains
└── certs/
    ├── homeserver.pem     # The current multi-domain certificate
    └── homeserver-key.pem # The private key
```
