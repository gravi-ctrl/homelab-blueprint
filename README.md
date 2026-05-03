# 🛠️ Homelab Blueprint

> **Mirror Status:** This repository is mirrored across [Codeberg](https://codeberg.org/gravi-ctrl/homelab-blueprint) (Primary) and [GitHub](https://github.com/gravi-ctrl/homelab-blueprint).

This repository contains the "Brain" of the homelab: automation scripts, system configurations, and recovery tools.

**Location on Server:** `/home/gravi-ctrl/scripts`.

---

## 🚨 Disaster Recovery Protocol (Day 0)

If the server is wiped, follow this order to restore functionality.

The weekly `docker-stacks-DATE.tar.zst.age` backup contains everything needed to restore:
*   `/opt/stacks/` — Docker compose files, configs, and `.env` secrets
*   `~/scripts` — Automation scripts with `.env` files
*   `~/ctrl_s_master` - A personal project that can be found [here](https://github.com/gravi-ctrl/ctrl-s-master)
*   `~/.ssh` — Codeberg and server deploy keys
*   `/etc/ssh` — Host keys

### Phase 1: Bootstrap System

1.  **Upload the backup archive** to the server's home directory (`~/docker-stacks-*.tar.zst.age`).

2.  **Set up the decryption key:**
    ```bash
    # Paste your (age) private key from your password manager
    sudo nano /root/.backup-key.txt
    sudo chmod 600 /root/.backup-key.txt
    ```

3.  **Run the bootstrap script:**
    ```bash
    curl -fsSL codeberg.org/gravi-ctrl/server-bootstrap/raw/bootstrap.sh -o $HOME/bootstrap.sh
    cd $HOME && cat bootstrap.sh   # verify contents before running
    bash bootstrap.sh
    ```

    > **No backup?** Restore manually:
    >
    > 1. **Get the SSH keys from your password manager** and place them in `~/.ssh/`
    >    (you need both the server host keys and the Codeberg deploy key).
    >
    > 2. **Fix permissions:**
    >    ```bash
    >    chmod 700 ~/.ssh && chmod 600 ~/.ssh/id_* && chmod 644 ~/.ssh/id_*.pub
    >    ```
    >
    > 3. **Clone the repo:**
    >    ```bash
    >    git clone git@codeberg.org:gravi-ctrl/server-scripts.git ~/scripts
    >    find ~/scripts -type f -name "*.sh" -exec chmod +x {} +
    >    ```
    >
    > 4. **Create `.env` files from examples** and fill in the secrets (from your password manager):
    >    ```bash
    >    cp --update=none ~/scripts/.env.example ~/scripts/.env
    >    cp --update=none ~/scripts/bot-telegram/.env.example ~/scripts/bot-telegram/.env
    >    cp --update=none ~/scripts/cert-manager/.env.example ~/scripts/cert-manager/.env
    >    cp --update=none ~/scripts/dockcheck/default.config ~/scripts/dockcheck/dockcheck.config
    >    ```

4.  **Re-link Git and pull the latest code** *(skip if you cloned above)*:
    ```bash
    cd ~/scripts
    git init
    git remote add origin git@codeberg.org:gravi-ctrl/homelab-blueprint.git
    git fetch origin
    git checkout -f -B main origin/main
    ```

5.  **Run the Installer:**

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

6.  **Once the installer is done, just re-open the SSH session for changes to take effect**

    *Reference files are located in:* `run_once/system_configs/`.

---

### Phase 2: Restore Docker Stacks

The backup already extracted `/opt/stacks/` with all compose files, configs, and `.env` secrets in [Phase 1](https://codeberg.org/gravi-ctrl/server-scripts#phase-1-bootstrap-system).

> **No backup?** You'll be starting fresh — application data (databases,
> uploads, container configs) is unrecoverable.
> Clone the [server-docker-backup](https://codeberg.org/gravi-ctrl/server-docker-backup) repo:
> ```bash
> sudo mkdir -p /opt/stacks
> sudo chown -R $(id -u):$(id -g) /opt/stacks
> git clone git@codeberg.org:gravi-ctrl/server-docker-backup.git /opt/stacks
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
    git remote add origin git@codeberg.org:gravi-ctrl/server-docker-backup.git
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
    sudo age -d -i /root/.backup-key.txt docker-stacks-*.tar.zst.age | sudo tar --zstd -xf - -C / 'opt/stacks/nextcloud/html'
    ```

*   Extract only `.env` files (secrets) from the backup:
    ```bash
    sudo age -d -i /root/.backup-key.txt docker-stacks-*.tar.zst.age | sudo tar --zstd -xf - -C / --wildcards 'opt/stacks/*/.env' 'home/gravi-ctrl/scripts/*/.env'
    ```

---

### Phase 3: Finalize

*   **Verify Paths:** Most, if not all, of the scripts are working through crontabs. Just make sure the paths of the scripts are matching the ones in crontabs.
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

---

## 🔄 Mirroring Workflow

This repository is primary-hosted on **Codeberg** and mirrored to **GitHub**. To maintain synchronicity with a single `git push`, the local `origin` is configured with multiple push URLs.

### Setup Dual-Push (Optional)
If you are contributing or mirroring this setup:
```bash
# Set the primary push URL (Codeberg)
git remote set-url --add --push origin git@codeberg.org:gravi-ctrl/homelab-blueprint.git

# Add the mirror push URL (GitHub)
git remote set-url --add --push origin git@github.com:gravi-ctrl/homelab-blueprint.git

# Verify configuration
git remote -v
