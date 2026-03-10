# 🛠️ Server Scripts & Automation Blueprint

This repository contains the "Brain" of the homelab: automation scripts, system configurations, and recovery tools.

**Location on Server:** `/home/gravi-ctrl/scripts`.

---

## 🚨 Disaster Recovery Protocol (Day 0)

If the server is wiped, follow this order to restore functionality.

The weekly `docker-stacks-DATE.tar.zst` backup contains everything needed to restore:
*   `/opt/stacks/` — Docker compose files, configs, and `.env` secrets
*   `~/scripts` — Automation scripts with `.env` files (secrets not stored in Git)
*   `~/.ssh` — GitHub deploy keys
*   `/etc/ssh` — Host keys

### Phase 1: Bootstrap System

1.  **Extract the backup and fix SSH permissions:**
    ```bash
    sudo apt install zstd
    sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C /
    sudo chown -R $(id -u):$(id -g) ~/.ssh
    chmod 700 ~/.ssh && chmod 600 ~/.ssh/id_* && chmod 644 ~/.ssh/id_*.pub
    ```

    > **No backup?** You'll need to manually set up SSH keys for GitHub, then clone the repos:
    > ```bash
    > git clone git@github.com:gravi-ctrl/server-scripts.git ~/scripts
    > find ~/scripts -type f -name "*.sh" -exec chmod +x {} +
    > ```
    > You'll need to add the secrets manually in the `.env` files.

2.  **Re-link Git and pull the latest code**:
    ```bash
    cd ~/scripts
    git init
    git remote add origin git@github.com:gravi-ctrl/server-scripts.git
    git fetch origin
    git reset --hard origin/main
    git branch --set-upstream-to=origin/main
    ```

3.  **Run the Installer:**
    This installs Docker, dependencies, configures Python, Shell environment, and **automatically restores** system configs & dotfiles.
    ```bash
    ~/scripts/run_once/setup.sh
    ```

---

### Phase 2: Restore System Configs

*Reference files are located in:* `run_once/system_configs/`.

#### ✅ Automatically Handled by `setup.sh`:

*   `/etc/hosts` - Restored automatically
*   User & Root crontabs - Restored automatically
*   Dotfiles (`.zshrc`, `.p10k.zsh`, `.nanorc`, `.hushlogin`, `.config/*`) - Restored automatically

#### ⚠️ Manual Steps Still Required:

*   **Fix Permissions (After mounting drives):**
   ```bash
   sudo chown -R $(id -u):$(id -g) /data/assets
   sudo chown -R 33:33 /data/assets/nextcloud_data
   sudo setfacl -R -m u:33:rwx /data/assets
   sudo setfacl -R -d -m u:33:rwx /data/assets
   ```

---

### Phase 3: Restore Docker Stacks

The backup already extracted `/opt/stacks/` with all compose files, configs, and `.env` secrets in Phase 1. Pull the latest and launch:

1.  **Launch Dockge:**
    ```bash
    cd /opt/stacks/dockge
    docker compose up -d
    ```

2.  **Deploy remaining stacks via Dockge Web UI.**

> **No backup?** Clone the repo and set up secrets manually:
> ```bash
> sudo mkdir -p /opt/stacks
> sudo chown -R $(id -u):$(id -g) /opt/stacks
> git clone git@github.com:gravi-ctrl/server-docker-backup.git /opt/stacks
> ```
> Then copy `.env.example` files to `.env` and fill in your secrets:
> ```bash
> for d in /opt/stacks/*/; do [ -f "${d}.env.example" ] && cp -n "${d}.env.example" "${d}.env"; done
> ```
> You can edit them manually or through the Dockge Web UI after launching it.
> The same applies to any `.env` files in `~/scripts` — copy from `.env.example` and fill in values.


3.  **Re-link Git and pull the latest code**:
    ```bash
    cd /opt/stacks
    git init
    git remote add origin git@github.com:gravi-ctrl/server-docker-backup.git
    git fetch origin
    git reset --hard origin/main
    git branch --set-upstream-to=origin/main
    ```
**Useful extraction tips:**

*   Extract a specific directory from the backup:
    ```bash
    sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C / 'opt/stacks/nextcloud/html/extra-apps'
    ```

*   Extract only `.env` files (secrets) from the backup:
    ```bash
    sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C / --wildcards 'opt/stacks/*/.env' 'home/gravi-ctrl/scripts/*/.env'
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
| 1 | Extract backup & Run setup.sh | ✅ Full | Handles ~85% of restoration |
| 2 | System configs | ⚠️ Partial | Hosts, crons, dotfiles automated |
| 3 | Docker stacks | ⚠️ Minimal | Already extracted; just `git pull` and launch |
| 4 | Finalize | ⚠️ Manual | Path verification & reboot |
