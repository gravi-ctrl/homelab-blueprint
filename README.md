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
    This installs Docker, ACLs, BindFS, Nextcloud Snap, Python envs, and creates the directory skeleton.
    ```bash
    ~/scripts/setup.sh
    ```

### Phase 2: Restore System Configs
*Reference files are located in:* `run_once/system_configs/`.

1.  **Fstab (Mounts):**
    *   Open `/etc/fstab`.
    *   **CRITICAL:** Update UUIDs for your new hard drives (check with `blkid`).
    *   Copy the **BindFS** line for Nextcloud assets from `fstab.txt`.
2.  **Sudo Permissions:**
    *   Run `sudo visudo`.
    *   Add: `gravi-ctrl ALL=(root) NOPASSWD: /usr/bin/crontab -l`.
3.  **Cron Jobs:**
    *   Restore User: `cat "run_once/system_configs/user_crontab.txt" | crontab -`.
    *   Restore Root: `cat "run_once/system_configs/root_crontab.txt" | sudo crontab -`.
4.  **Firewall Rules:**
    *   Run the script: `run_once/setup-firewall.sh`.
5.  **Hosts File:**
    *   Run `sudo cp run_once/system_configs/hosts.txt /etc/hosts`.

### Phase 3: Restore Docker Stacks
1.  **Clone the Docker Repo:**
    ```bash
    sudo mkdir -p /opt/stacks
    sudo chown -R $(id -u):$(id -g) /opt/stacks
    git clone git@github.com:gravi-ctrl/server-docker-backup.git /opt/stacks
    ```
2.  **Restore Identity & Secrets:**
    *   *Prerequisite:* Ensure zstd is installed (`sudo apt install zstd`).
    *   *Source:* The weekly `docker-stacks-DATE.tar.zst` backup.
    *   **Option A (Full Restore):** Restores Stacks, SSH Keys and Host Keys:
        ```bash
        sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C /
        ```
    *   **Option B (Just Envs):**
        ```bash
        sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C / --wildcards 'opt/stacks/*/.env'
        ```
    *   **Option C (.env.example):**
        *   Get the secrets from your PWM.
        *   Copy every `.env.example` in every stack to `.env` using:
        ```bash
        for d in /opt/stacks/*/; do [ -f "${d}.env.example" ] && cp -n "${d}.env.example" "${d}.env"; done
        ```
        *   Fill in the `.env` files with your secrets either through terminal, or Dockge.
3.  **Launch:**
    ```bash
    cd /opt/stacks/dockge
    docker compose up -d
    # Then deploy remaining stacks via Dockge Web UI
    # If 'Option C' was chosen, Dockge can help fill the .env with ease
    ```

### Phase 4: Finalize
*   **Pathes** Most if not all of the scripts are working through crontabs. Just make sure the pathes of the scripts are matching the ones in crontabs.
*   **Reboot**.
