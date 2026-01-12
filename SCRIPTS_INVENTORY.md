# 📂 Script Inventory
> Auto-generated on Tue Jan 13 01:42:55 AM EET 2026

| Script File | Purpose | Frequency |
| :--- | :--- | :--- |
| `backup-scripts-git.sh` | Snapshots fstab/cron/packages/dotfiles and pushes this repo to GitHub | Daily 5am |
| `cleanup_script.py` | Cleans folders, keeping the 2 most recent files | Daily 10am |
| `cron_translator.py` | Creates a human-readable .MD file of the crontabs | Daily 5am (`backup-scripts-git.sh` runs it) |
| `git-auto-sync.sh` | Master logic to push/pull Git repos (Docs, Stacks) | Every 15m |
| `local-opt-backup.sh` | Backs up Docker volumes to tar.xz | Weekly (Mon 5:30am |
| `nextcloud-dynamic-watch.sh` | Watches `/srv/data/assets` + `/mnt`, scans Nextcloud, fixes Permissions | Service (Always) |
| `run_once/fix-cpu-thermals.sh` | Restores CPU max frequency to 1.6GHz and restarts TLP after an OS upgrade | On Demand |
| `run_once/setup-firewall.sh` | Bootstrap: Resets UFW and applies correct rules | Run Once |
| `run_once/setup.sh` | Bootstrap: Installs all apt/snap requirements and fixes permissions | Run Once |
| `script_indexer.py` | Creates a human-readable file of every script and its function | Daily 5am (`backup-scripts-git.sh` runs it) |
| `wifi_robot/guest_wifi.py` | Selenium bot to toggle Guest WiFi via TP-Link Router. | On Demand (Telegram) |
| `wifi_robot/guest_wifi.sh` | Triggers the `guest_wifi.py` script | On Demand (Telegram) |