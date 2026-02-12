# 🛠️ Server Scripts & Automation Blueprint

This repository contains the "Brain" of the homelab: automation scripts, system configurations, and recovery tools.

**Location on Server:** `/home/gravi-ctrl/scripts`.

---

## 🚨 Disaster Recovery Protocol (Day 0)

If the server is wiped, follow this order to restore functionality.

*   For the `git clone..` to work, you need to restore the github keys first, which are included in the `docker-stacks-DATE.tar.zst` backup.
*   After restoring the keys, make sure to do the following: 
    *   `chmod 700 ~/.ssh`.
    *   `chmod 600 ~/.ssh/id_*`.
    *   `chmod 644 ~/.ssh/id_*.pub`.

### Phase 1: Bootstrap System

1.  **Clone this Repo:**
    ```bash
    git clone git@github.com:gravi-ctrl/server-scripts.git ~/scripts
    chmod +x ~/scripts/*.sh
    ```

2.  **Run the Installer:**
    This installs Docker, dependencies, configures Python, Shell environment, and **automatically restores** system configs & dotfiles.
    ```bash
    ~/scripts/setup.sh
    ```

---

### Phase 2: Restore System Configs

*Reference files are located in:* `run_once/system_configs/`.

#### ✅ Automatically Handled by `setup.sh`:

*   `/etc/hosts` - Restored automatically
*   User & Root crontabs - Restored automatically
*   Dotfiles (`.zshrc`, `.p10k.zsh`, `.nanorc`, `.hushlogin`, `.config/*`) - Restored automatically

#### ⚠️ Manual Steps Still Required:

**1. Fstab (Mount Drives) - CRITICAL:**
   *   After `setup.sh` completes, review your backup:
       ```bash
       cat ~/scripts/run_once/system_configs/fstab.txt
       ```
   *   Find your new drive UUIDs:
       ```bash
       blkid
       ```
   *   Edit `/etc/fstab`:
       ```bash
       sudo nano /etc/fstab
       ```
   *   Update UUIDs for your new hard drives
   *   Test before rebooting:
       ```bash
       sudo mount -a
       ```

**2. Sudo Permissions:**
   *   Run `sudo visudo`
   *   Add the following line:
       ```
       gravi-ctrl ALL=(root) NOPASSWD: /usr/bin/crontab -l
       ```

**3. Firewall Rules:**
   ```bash
   ~/scripts/run_once/setup-firewall.sh
   ```

**4. Fix Permissions (After mounting drives):**
   ```bash
   sudo chown -R $(id -u):$(id -g) /srv/data/assets
   sudo chown -R 33:33 /srv/data/assets/nextcloud_data
   sudo setfacl -R -m u:33:rwx /srv/data/assets
   sudo setfacl -R -d -m u:33:rwx /srv/data/assets
   ```

---

### Phase 3: Restore Docker Stacks

**Choose your approach:**

*   **Option A** — Full Restore from Backup (fastest, if you have the `.tar.zst`)
*   **Option B** — Clone Repo + Restore Configs (manual setup)

#### Option A: Full Restore from Backup (Fastest)

If you have the weekly `docker-stacks-DATE.tar.zst` backup, this restores everything in one command: compose files, configs, and secrets.

*   **Prerequisite:** Ensure zstd is installed
    ```bash
    sudo apt install zstd
    ```

*   **Restore the entire stack:**
    ```bash
    sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C /
    ```
    This extracts everything to the proper locations including `/opt/stacks/`, SSH keys, and host keys.

*   **Launch Dockge:**
    ```bash
    cd /opt/stacks/dockge
    docker compose up -d
    ```

*   **Deploy remaining stacks via Dockge Web UI**

---

#### Option B: Clone Repo + Restore Configs

If you don't have the backup file or prefer manual setup:

1.  **Clone the Docker Repo:**
    ```bash
    sudo mkdir -p /opt/stacks
    sudo chown -R $(id -u):$(id -g) /opt/stacks
    git clone git@github.com:gravi-ctrl/server-docker-backup.git /opt/stacks
    ```

2.  **Restore Secrets & Configs:**
    *   *Prerequisite:* Ensure zstd is installed (`sudo apt install zstd`).
    
    *   **Option B1 (From partial backup):**
        If you have the `docker-stacks-DATE.tar.zst` but only want `.env` files:
        ```bash
        sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C / --wildcards 'opt/stacks/*/.env'
        ```

    *   **Option B2 (Manual entry):**
        Get the secrets from your PWM and fill in manually:
        ```bash
        for d in /opt/stacks/*/; do [ -f "${d}.env.example" ] && cp -n "${d}.env.example" "${d}.env"; done
        ```
        Then fill in each `.env` file with your secrets using a text editor or Dockge Web UI.

3.  **Launch:**
    ```bash
    cd /opt/stacks/dockge
    docker compose up -d
    # Then deploy remaining stacks via Dockge Web UI
    # Dockge can help manage and fill .env files with ease
    ```
    
---

### Phase 4: Finalize

*   **Verify Paths:** Most if not all of the scripts are working through crontabs. Just make sure the paths of the scripts are matching the ones in crontabs.
*   **Reboot:**
    ```bash
    sudo reboot
    ```

---

## 📋 Quick Reference

| Phase | Task | Automation | Notes |
|-------|------|-----------|-------|
| 1 | Clone repo & Run setup.sh | ✅ Full | Handles ~85% of restoration |
| 2 | System configs | ⚠️ Partial | Hosts, crons, dotfiles automated; fstab manual |
| 3 | Docker stacks | ⚠️ Manual | Requires secrets & .env files |
| 4 | Finalize | ⚠️ Manual | Path verification & reboot |

---

## 🔄 Daily Backups

Backups run automatically via cron at **5am daily** and sync to Git:

*   **System Configs:** `run_once/system_configs/` (fstab, hosts, crontabs, packages, dotfiles)
*   **Scripts:** `~/scripts/` synced to Git
*   **Docker Stacks:** `/opt/stacks/` synced to Git
*   **Full Backup:** `docker-stacks-DATE.tar.zst` includes secrets & SSH keys
