# cert-manager

A bash script that automates **SSL management** (via `mkcert`), **Local DNS provisioning** (via Pi-hole v6), and **Reverse Proxy provisioning** for a local homeserver environment. It handles CA distribution, multi-SAN certificate generation, and automatic deployment of both certificates and proxy hosts to Nginx Proxy Manager (NPM) via its API.

## Overview

This script eliminates the manual "click-ops" of setting up new services. It manages a `services.list` and communicates with the NPM API and Pi-hole (via `docker exec`) to:

1.  **Manage Identity:** Generate one master certificate covering all your subdomains (e.g., `jellyfin.homeserver`, `nextcloud.homeserver`).
2.  **Manage Local DNS:** Automatically add or remove DNS records in your local Pi-hole instance pointing to your server's IP.
3.  **Manage Routing:** Automatically create or update Proxy Host entries in NPM with optimized defaults (Force SSL, HSTS, WebSockets).
4.  **Keep in Sync:** When you add, update, or remove a service, the certificate is regenerated, re-uploaded, the Pi-hole DNS is updated, and the NPM routing is updated instantly.

## Prerequisites

- Debian/Ubuntu-based server
- Nginx Proxy Manager (accessible via API)
- Pi-hole (v6+) running in Docker, and a user allowed to run `docker exec` (docker group) — see [Pi-hole DNS](#2-pi-hole-dns-no-api-password)
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
NPM_URL="http://192.168.1.x:81"
NPM_EMAIL="api@homeserver.local"
NPM_PASS="your-password-here"
NPM_CERT_NAME="homeserver"

# Pi-hole (v6+)
PIHOLE_CONTAINER="pihole"   # optional, this is the default
SERVER_IP="192.168.1.x"
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
This adds the domain to the SSL certificate, **creates a local DNS record in Pi-hole**, and creates the Proxy Host entry in NPM.
```bash
# Syntax: ./cert-manager.sh add <service_name> <internal_ip_or_container_name> <port> [s]
# [s] = use https. Omit entirely for http.
./cert-manager.sh add jellyfin jellyfin 8096
./cert-manager.sh add paperless 192.168.1.x 8000
```

**2. Update an existing service:**
If a service's IP, port, or protocol changes, just run the `add` command again. The script will dynamically update NPM and your local `services.list` instead of creating duplicates.
```bash
./cert-manager.sh add jellyfin 192.168.1.50 8096 s  # The 's' sets the forward scheme to https
```

**3. Add a service (SSL and DNS only):**
If you only want the domain added to the certificate and registered in Pi-hole DNS, but prefer to configure the NPM proxy host manually.
```bash
./cert-manager.sh add custom-app
```

**4. Remove a service:**
This **deletes the DNS record from Pi-hole**, **deletes the Proxy Host from NPM**, removes the domain from the certificate list, and regenerates the smaller cert.
```bash
./cert-manager.sh remove jellyfin
```

### Bulk Operations

You can act on the entire `services.list` file at once. This is highly useful for restoring your state to a new NPM/Pi-hole instance or tearing everything down.

*   **Replay Configuration**: `./cert-manager.sh add services.list`
    Iterates through your `services.list`, ensuring every DNS record and NPM proxy host exists and is up to date. Only regenerates the certificate once at the very end if changes were made.
*   **Purge Everything**: `./cert-manager.sh remove services.list`
    Backs up your `services.list` with a timestamp, completely removes all records from Pi-hole and NPM, and blanks out the local file.

### Maintenance & Audit

```bash
./cert-manager.sh list     # Show all services with their current target (ip:port/scheme)
./cert-manager.sh status   # Show cert expiry, SANs, and check the cert covers every entry in services.list
./cert-manager.sh regen    # Force a certificate regeneration and upload (alias: renew)
```

---

## NPM & Pi-hole Setup

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

### 2. Pi-hole DNS (no API password)

The script manages Local DNS records through Pi-hole's own CLI instead of its HTTP API:

```bash
docker exec pihole pihole-FTL --config dns.hosts            # read the list
docker exec pihole pihole-FTL --config dns.hosts '["IP name", ...]'   # REPLACE the list
```

*   **No credentials.** There is no Pi-hole password, app password, session or `app_sudo` setting involved, and Pi-hole's web port doesn't need to be published. It also doesn't depend on NPM or Pi-hole's web server being up.
*   **Access requirement.** The user running the script must be able to run `docker exec` (docker group). That is effectively root on the host, so it's only a good fit if that user already has that access.
*   **Records show up in the UI** under **Local DNS > DNS Records**, and DNS changes take effect immediately.
*   **The write replaces the whole list**, so every change is read-modify-write. The script protects your other records (manually added ones included):
    *   it aborts without writing if the current list can't be read or doesn't parse as a list of `IP name` entries;
    *   it refuses a write that would remove more than one record;
    *   it saves the previous list to `.pihole-hosts.bak` before each write and verifies the result by reading it back.
*   If your Pi-hole container isn't called `pihole`, set `PIHOLE_CONTAINER` in `.env`.
*   Don't pin `dns.hosts` through an `FTLCONF_dns_hosts` environment variable in Pi-hole's compose. Pi-hole v6 treats env-defined settings as read-only.

---

## How It Works

```text
 User Command: ./cert-manager.sh add jellyfin 192.168.1.x 8096
       │
       ▼
 1. Update services.list ──► Upserts "jellyfin jellyfin 8096" into the
       │                     local manifest. No-ops if that exact entry
       │                     already exists; replaces in place if it changed.
       │
 2. Generate SSL ──────────► mkcert creates a single SAN cert for:
       │                     homeserver, jellyfin.homeserver, etc.
       │
 3. Sync with NPM ─────────► Uploads the new .pem and .key via NPM API.
       │
 4. Provision DNS ─────────► Creates/Updates Local DNS in Pi-hole:
       │                     jellyfin.homeserver ➔ 192.168.1.x (SERVER_IP)
       │
 5. Provision Route ───────► Creates/Updates Proxy Host in NPM:
                             jellyfin.homeserver ➔ http://192.168.1.x:8096
                             (Auto-enables Force SSL, HSTS, and Websockets)
       │
 6. Sync Files ────────────► Copies certs to SHARE_DIR for client distribution.
```

## File Structure

```text
~/scripts/cert-manager/
├── cert-manager.sh       # The automation script
├── .env                  # Private configuration (Credentials)
├── services.list         # One full entry per service: "svc ip port [s]"
└── certs/
    ├── homeserver.pem     # The current multi-domain certificate
    └── homeserver-key.pem # The private key
```
