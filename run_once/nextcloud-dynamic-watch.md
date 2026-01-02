# 🛠️ Nextcloud Watcher Installation Guide

This guide sets up the system service to run your `nextcloud-dynamic-watch.sh` script in the background.

### Step 1: Prepare the Script
1.  **Create the file:**
    ```bash
    nano /home/gravi-ctrl/scripts/nextcloud-dynamic-watch.sh
    ```
    *(Paste the script code here if you haven't already).*

2.  **Make it executable:**
    ```bash
    chmod +x /home/gravi-ctrl/scripts/nextcloud-dynamic-watch.sh
    ```

### Step 2: Tune Linux Watchers (Crucial)
Nextcloud data folders are huge. Default Linux limits are too low, which causes the script to silently fail on large libraries.

1.  **Edit sysctl:**
    ```bash
    sudo nano /etc/sysctl.conf
    ```
2.  **Add this line to the bottom:**
    ```ini
    fs.inotify.max_user_watches=524288
    ```
3.  **Apply changes:**
    ```bash
    sudo sysctl -p
    ```

### Step 3: Create the System Service
1.  **Edit the service file:**
    ```bash
    sudo nano /etc/systemd/system/nc-watcher.service
    ```

2.  **Paste this configuration:**
    ```ini
    [Unit]
    Description=Nextcloud Dynamic Filesystem Watcher
    After=network.target snap.nextcloud.apache.service

    [Service]
    User=root
    ExecStart=/home/gravi-ctrl/scripts/nextcloud-dynamic-watch.sh
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    ```

3.  **Enable and Start:**
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable --now nc-watcher.service
    ```

### Step 4: Verification
1.  **Check the logs:**
    ```bash
    sudo journalctl -u nc-watcher.service -f
    ```
2.  **Test it:** Create a test file in your assets folder (`/srv/data/assets`). You should see the log trigger within 10 seconds, and the file should appear in Nextcloud immediately.
