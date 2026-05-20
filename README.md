![License](https://img.shields.io/badge/license-MIT-green?label=License)
![Platform](https://img.shields.io/badge/platform-Linux-blue?label=Platform)



# 🛠️ Homelab Blueprint

> **Mirror Status:** Mirrored across [Codeberg](https://codeberg.org/gravi-ctrl/homelab-blueprint) (Primary) and [GitHub](https://github.com/gravi-ctrl/homelab-blueprint).

Automation scripts, system configurations, and recovery tooling for a self-hosted home server. Every scheduled task reports back via Telegram, every failure is logged, and the entire server can be rebuilt from a single encrypted archive.

**Location on Server:** `~/scripts`

---

## 📊 Live Indices
Auto-generated daily — always current:

- **[📜 Script Inventory](./SCRIPTS_INVENTORY.md)** — every script, its purpose, and run frequency
- **[📅 Automation Schedule](./CRON_SCHEDULE.md)** — full cron schedule in human-readable form

---

## 🧠 How it fits together

```
Scheduled tasks → cron-guard.py → Telegram alerts
                                           ↓
                              success / failure / always

SSL certs     → cert-manager.sh → auto-uploads to NPM
DNS           → Pi-hole + Unbound (recursive, no upstream logging)
Containers    → Docker + NPM reverse proxy (no exposed ports except NPM)
Remote access → Tailscale (+ Funnel for n8n webhooks)
Backups       → age-encrypted weekly archive + Borg for /data/assets
```

---

## 🚨 Disaster Recovery

The weekly `docker-stacks-DATE.tar.zst.age` backup contains everything needed:

| Path | What |
|------|------|
| `/opt/stacks/` | Docker compose files, configs, `.env` secrets → [server-docker-backup](/gravi-ctrl/server-docker-backup) |
| `~/scripts` | This repo |
| `~/ctrl_s_master` | Credential archival engine → [ctrl-s-master](/gravi-ctrl/ctrl-s-master) |
| `~/.ssh` | Deploy keys |
| `/etc/ssh` | Host keys |

---

### Phase 1 — Bootstrap

**1.** Upload backup archive to `~/docker-stacks-*.tar.zst.age`

**2.** Set up decryption key:
```bash
sudo nano /root/.backup-key.txt
sudo chmod 600 /root/.backup-key.txt
```

**3.** Run bootstrap:
```bash
curl -fsSL codeberg.org/gravi-ctrl/homelab-blueprint/raw/bootstrap.sh -o $HOME/bootstrap.sh
cat bootstrap.sh
bash bootstrap.sh
```

Decrypts the backup, restores filesystem, fixes SSH, re-links git repos, self-destructs.

> **No backup?**
> ```bash
> # Place SSH keys from password manager into ~/.ssh/
> chmod 700 ~/.ssh && chmod 600 ~/.ssh/id_* && chmod 644 ~/.ssh/id_*.pub
>
> git clone git@codeberg.org:gravi-ctrl/homelab-blueprint.git ~/scripts
> find ~/scripts -type f -name "*.sh" -exec chmod +x {} +
>
> # Fill in secrets
> find ~/scripts -type f -name ".env.example" -execdir cp --update=none .env.example .env \;
> cp --update=none ~/scripts/dockcheck/default.config ~/scripts/dockcheck/dockcheck.config
> ```

**4.** Run the installer:
```bash
~/scripts/run_once/setup.sh
```

Covers: Docker, firewall, directory structure, permissions, Unbound, dotfiles, crontabs. Installs a background watcher that auto-configures containers as they come up.

**5.** Reopen SSH session for shell changes to take effect.

---

### Phase 2 — Docker Stacks

`/opt/stacks/` is already restored from Phase 1. Launch Dockge and deploy from its UI, or all at once:

```bash
cd /opt/stacks/dockge && docker compose up -d

# All stacks at once
find /opt/stacks -maxdepth 2 -name "compose.yml" -execdir docker compose up -d \;
```

> **No backup?**
> ```bash
> sudo mkdir -p /opt/stacks && sudo chown -R $(id -u):$(id -g) /opt/stacks
> git clone git@codeberg.org:gravi-ctrl/server-docker-backup.git /opt/stacks
>
> # New secrets only - as configs at this point are... well, gone
> for d in /opt/stacks/*/; do [ -f "${d}.env.example" ] && cp --update=none "${d}.env.example" "${d}.env"; done
> ```

**Useful extraction commands:**
```bash
# Specific path
sudo age -d -i /root/.backup-key.txt docker-stacks-*.tar.zst.age | sudo tar --zstd -xf - -C / 'opt/stacks/nextcloud/html'

# .env files only
sudo age -d -i /root/.backup-key.txt docker-stacks-*.tar.zst.age | sudo tar --zstd -xf - -C / --wildcards 'opt/stacks/*/.env' 'home/gravi-ctrl/scripts/*/.env'
```

---

### Phase 3 — Finalize

The background watcher handles most post-restore tasks automatically. What remains:

- **Borg key** → once external HDD is mounted:
  ```bash
  borg key import /mnt/external_hdd/borg-repo ~/borg-key-backup.txt && borgmatic check
  ```
- **Tailscale** → if connection fails, regenerate auth key at [Tailscale Admin](https://login.tailscale.com/admin/settings/keys) → Reusable + Tags → update `TS_AUTHKEY` in `/opt/stacks/tailscale/.env`
- **Reboot**

---

## 📋 Quick Reference

| Phase | Task | Automation |
|-------|------|-----------|
| 1 | Decrypt backup, bootstrap system | ✅ Full |
| 2 | Deploy Docker stacks | ⚠️ Launch Dockge, rest is one command |
| 3 | Post-restore config | ⚠️ Mostly via background watcher |

---

## 🔄 Dual-push mirror setup

```bash
git remote set-url --add --push origin git@codeberg.org:gravi-ctrl/homelab-blueprint.git
git remote set-url --add --push origin git@github.com:gravi-ctrl/homelab-blueprint.git
git remote -v
```