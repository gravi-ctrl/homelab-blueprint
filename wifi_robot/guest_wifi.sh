#!/bin/bash
# @DESCRIPTION: Triggers the `guest_wifi.py` script
# @FREQUENCY: On Demand (Telegram - through n8n)
# Define the absolute path to your project directory.
PROJECT_DIR="/home/gravi-ctrl/scripts/wifi_robot"

# The rest of your script.
if [ -z "$1" ]; then
    echo "Error: Please specify 'on' or 'off'."
    echo "Usage: guestwifi on"
    exit 1
fi

echo "--- Starting the Wi-Fi Robot... ---"

# Tell docker-compose exactly where to find its files using the --project-directory flag.
# This is the most reliable method.
sudo docker compose --project-directory "$PROJECT_DIR" run --rm robot $1
