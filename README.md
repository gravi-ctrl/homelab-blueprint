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

1.  **Extract the backup and fix SSH permissions by running:**

    > *Make sure the `docker-stacks-DATE.tar.zst` file is in `/home/$USER` first*

    ```bash
    curl -sL is.gd/bootme | bash
    ```
    **Or by manually entering:**
    ```bash
    sudo apt update && sudo apt install zstd -y
    cd $HOME && sudo tar --use-compress-program=zstd -xf docker-stacks-*.tar.zst -C /

    sudo apt purge cloud-init -y
    sudo rm -rf /etc/cloud
    sudo rm -f /etc/ssh/sshd_config.d/50-cloud-init.conf
    sudo systemctl restart ssh
    ```

    > **No backup?** You'll need to restore the SSH keys for both the Server and GitHub (Secrets are in the PWM), then clone the repo:
    > ```bash
    > chmod 700 ~/.ssh && chmod 600 ~/.ssh/id_* && chmod 644 ~/.ssh/id_*.pub
    > git clone git@github.com:gravi-ctrl/server-scripts.git ~/scripts
    > find ~/scripts -type f -name "*.sh" -exec chmod +x {} +
    > ```
    > Then you'll need to copy the `.env.example` files to `.env` and add the secrets manually, which can be obtained from the PWM.

3.  **Re-link Git and pull the latest code** *(skip if you cloned above)*:
    ```bash
    cd ~/scripts
    git init
    git remote add origin git@github.com:gravi-ctrl/server-scripts.git
    git fetch origin
    git checkout -f -B main origin/main
    ```

4.  **Run the Installer:**

    This installs Docker, dependencies, configures Python, Shell environment, and **automatically handles:**
    *   Dotfiles (`.zshrc`, `.p10k.zsh`, `.nanorc`, `.hushlogin`, `.config/*`)
    *   `/etc/hosts` restoration
    *   User & Root crontabs restoration
    *   Cloud-init removal & SSH restart
    *   Firewall rules (`setup-firewall.sh`)
    *   `/data/assets` directory creation & permissions

    ```bash
    ~/scripts/run_once/setup.sh
    ```
5.  **Once the installer is done, just re-open the SSH session for changes to take effect**

    *Reference files are located in:* `run_once/system_configs/`.

---

### Phase 2: Restore Docker Stacks

The backup already extracted `/opt/stacks/` with all compose files, configs, and `.env` secrets in [Phase 1](https://github.com/gravi-ctrl/server-scripts/tree/main#phase-1-bootstrap-system).

> **No backup?** You'll be starting fresh — application data (databases,
> uploads, container configs) is unrecoverable.
> Clone the [server-docker-backup](https://github.com/gravi-ctrl/server-docker-backup) repo:
> ```bash
> sudo mkdir -p /opt/stacks
> sudo chown -R $(id -u):$(id -g) /opt/stacks
> git clone git@github.com:gravi-ctrl/server-docker-backup.git /opt/stacks
> ```
> Then generate new secrets for the stacks:
> ```bash
> for d in /opt/stacks/*/; do [ -f "${d}.env.example" ] && cp --update=none "${d}.env.example" "${d}.env"; done
> ```
> These are fresh containers — set new passwords, don't reuse old ones.
> You can edit them manually or through the Dockge Web UI after launching it.

1.  **Re-link Git and pull the latest code** *(skip if you cloned above)*:
    ```bash
    cd /opt/stacks
    git init
    git remote add origin git@github.com:gravi-ctrl/server-docker-backup.git
    git fetch origin
    git checkout -f -B main origin/main
    ```

2.  **Launch Dockge:**
    ```bash
    cd /opt/stacks/dockge
    docker compose up -d
    ```

3.  **Deploy remaining stacks via Dockge Web UI.**

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

### Phase 3: Finalize

*   **Verify Paths:** Most if not all of the scripts are working through crontabs. Just make sure the paths of the scripts are matching the ones in crontabs.
*   **Reboot:**
    ```bash
    sudo reboot
    ```

---

## 📋 Quick Reference

| Phase | Task | Automation | Notes |
|-------|------|-----------|-------|
| 1 | Extract backup, re-link Git & run setup.sh | ✅ Full | Handles ~95% of restoration |
| 2 | Docker stacks | ⚠️ Minimal | Re-link Git, launch Dockge, deploy stacks |
| 3 | Finalize | ⚠️ Manual | Path verification & reboot |
