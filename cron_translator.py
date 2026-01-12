#!/usr/bin/env python3
import os
from cron_descriptor import get_description, Options

# --- CONFIG ---
SOURCE_DIR = "/home/gravi-ctrl/scripts/run_once/system_configs"
OUTPUT_FILE = "/home/gravi-ctrl/scripts/run_once/system_configs/CRON_SCHEDULE.md"

# Options to make it sound natural
opts = Options()
opts.use_24hour_time_format = False 

def parse_crontab(filename, title):
    content = []
    file_path = os.path.join(SOURCE_DIR, filename)
    
    if not os.path.exists(file_path):
        return []

    content.append(f"## {title}")
    content.append("| Frequency (Human Readable) | Command | Raw Schedule |")
    content.append("| :--- | :--- | :--- |")

    with open(file_path, 'r') as f:
        lines = f.readlines()

    has_entries = False
    for line in lines:
        line = line.strip()
        # Skip comments and empty lines
        if not line or line.startswith("#"):
            continue
        
        # Check if it looks like a cron line (starts with digit, *, or @)
        if line[0].isdigit() or line[0] == '*' or line.startswith("@"):
            parts = line.split(maxsplit=5)
            
            # Handle standard 5-part cron
            if len(parts) >= 6:
                raw_schedule = " ".join(parts[:5])
                command = parts[5]
                try:
                    # Translate to English
                    human_desc = get_description(raw_schedule, opts)
                except:
                    human_desc = "⚠️ Could not parse"
                
                content.append(f"| **{human_desc}** | `{command}` | `{raw_schedule}` |")
                has_entries = True
            
            # Handle @reboot, @daily, etc.
            elif line.startswith("@"):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    content.append(f"| **On System Event** | `{parts[1]}` | `{parts[0]}` |")
                    has_entries = True

    if not has_entries:
        content.append("*No active jobs found.*")
    
    content.append("\n")
    return content

# --- MAIN EXECUTION ---
final_md = ["# 📅 System Automation Schedule", f"Generated on: {os.popen('date').read().strip()}", ""]

final_md.extend(parse_crontab("user_crontab.txt", "👤 User Cron (gravi-ctrl)"))
final_md.extend(parse_crontab("root_crontab.txt", "⚡ Root Cron"))

# Write to file
with open(OUTPUT_FILE, 'w') as f:
    f.write("\n".join(final_md))

print(f"Schedule generated at: {OUTPUT_FILE}")
