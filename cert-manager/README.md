# cert-manager

A bash script that automates **SSL management** (via `mkcert`), **Local DNS provisioning** (via Pi-hole v6), and **Reverse Proxy provisioning** for a local homeserver environment. It handles CA distribution, multi-SAN certificate generation, and automatic deployment of both certificates and proxy hosts to Nginx Proxy Manager (NPM) via its API.

## Overview

This script eliminates the manual "click-ops" of setting up new services. It manages a `services.list` and communicates with the NPM and Pi-hole APIs to:

1.  **Manage Identity:** Generate one master certificate covering all your subdomains (e.g., `jellyfin.homeserver`, `nextcloud.homeserver`).
2.  **Manage Local DNS:** Automatically add or remove DNS records in your local Pi-hole instance pointing to your server's IP.
3.  **Manage Routing:** Automatically create or delete Proxy Host entries in NPM with optimized defaults (Force SSL, HSTS, WebSockets).
4.  **Keep in Sync:** When you add or remove a service, the certificate is regenerated, re-uploaded, the Pi-hole DNS is updated, and the NPM routing is updated instantly.

## Prerequisites

- Debian/Ubuntu-based server
- Nginx Proxy Manager (accessible via API)
- Pi-hole (v6+) with API write permissions enabled
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
NPM_PASS="your-password-here"
NPM_CERT_NAME="homeserver"

# Pi-hole API (v6+)
PIHOLE_URL="http://192.168.1.109:8081"
PIHOLE_PASS="your-pihole-app-password"
SERVER_IP="192.168.1.109"
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

**1. Add a service with automatic Pi-hole DNS and NPM routing:**
This adds the domain to the SSL certificate, **creates a local DNS record in Pi-hole pointing to your server's IP**, and creates the Proxy Host entry in NPM.
```bash
# Syntax: ./cert-manager.sh add <service_name> <internal_ip> <port> [scheme]
./cert-manager.sh add jellyfin 192.168.1.50 8096
./cert-manager.sh add paperless 192.168.1.109 8000
```

**2. Add a service (SSL and DNS only):**
If you only want the domain added to the certificate and registered in Pi-hole DNS, but prefer to configure the NPM proxy host manually.
```bash
./cert-manager.sh add custom-app
```

**3. Remove a service:**
This **deletes the DNS record from Pi-hole**, **deletes the Proxy Host from NPM**, removes the domain from the certificate list, and regenerates the smaller cert.
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

## API Configuration Setup

### 1. NPM API User Setup

For the script to manage both SSL and Routing, the NPM API user needs these specific permissions:

1.  **Users → Add User** in NPM.
2.  Role: **Administrator**.
3.  **Do NOT enable TOTP** for this specific user.
4.  **Edit Permissions**:
    *   **Item Visibility**: `All Items`
    *   **Certificates**: `Manage`
    *   **Proxy Hosts**: `Manage`
    *   *Everything else can be set to Hidden.*

### 2. Pi-hole v6 API Permissions (Crucial)

Because Pi-hole v6 API restricts configuration changes by default for security, you must permit the script to edit local DNS records:

*   **Via Web UI:** Go to **Settings > API / Web interface**, look for **Permit destructive actions via API (webserver.api.app_sudo)**, enable it, and save.
*   **Via Command Line:** (Run inside your Pi-hole container or host):
    ```bash
    pihole-FTL --config webserver.api.app_sudo true
    ```

*Note: Alternatively, you can use your primary Pi-hole login password as `PIHOLE_PASS` instead of an App Password to bypass this restriction.*

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
 4. Provision DNS ─────────► Creates/Updates Local DNS in Pi-hole:
       │                     jellyfin.homeserver ➔ 192.168.1.109 (SERVER_IP)
       │
 5. Provision Route ───────► Creates/Updates Proxy Host in NPM:
                             jellyfin.homeserver ➔ http://192.168.1.50:8096
                             (Auto-enables Force SSL, HSTS, and Websockets)
       │
 6. Sync Files ────────────► Copies certs to SHARE_DIR for client distribution.
```

## File Structure

```text
~/scripts/cert-manager/
├── cert-manager.sh       # The automation script
├── .env                  # Private configuration (Credentials)
├── services.list         # Plain text list of managed subdomains
└── certs/
    ├── homeserver.pem     # The current multi-domain certificate
    └── homeserver-key.pem # The private key
```
