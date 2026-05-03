#!/bin/bash
# @DESCRIPTION: Restores CPU max frequency to 1.6GHz and restarts TLP after an OS upgrade (Device specific)
# @FREQUENCY: Run Once
# Restores CPU max frequency and restarts TLP after an OS upgrade

# Set CPU max frequency to 1.6 GHz on all cores
for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
    echo 1600000 | sudo tee $cpu/cpufreq/scaling_max_freq
done

# Restart TLP to ensure power management settings are active
sudo systemctl restart tlp
sudo systemctl enable cpu-cooler

# Print current CPU frequency and TLP status for verification
echo "CPU frequencies:"
cpupower frequency-info | grep "current policy"
echo ""
echo "TLP status:"
sudo tlp-stat -s
