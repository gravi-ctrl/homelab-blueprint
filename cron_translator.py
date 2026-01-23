#!/usr/bin/env python3
# @DESCRIPTION: Creates a human-readable .MD file of the crontabs
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
import os
import re
from cron_descriptor import get_description, Options

# --- CONFIG ---
SOURCE_DIR = "/home/gravi-ctrl/scripts/run_once/system_configs"
OUTPUT_FILE = "/home/gravi-ctrl/scripts/run_once/system_configs/CRON_SCHEDULE.md"

# Options for the standard translator
opts = Options()
opts.use_24hour_time_format = True

def parse_crontab(filename, title):
    content = []
    file_path = os.path.join(SOURCE_DIR, filename)

    if not os.path.exists(file_path):
        return []

    content.append(f"## {title}")
    content.append("| Task Name / Description | Frequency | Command |")
    content.append("| :--- | :--- | :--- |")

    with open(file_path, 'r') as f:
        lines = f.readlines()

    has_entries = False
    last_comment = ""

    # 1. Map numbers to Day Names (1=Mon, 7=Sun)
    week_map = {
        '1': 'Monday', '2': 'Tuesday', '3': 'Wednesday', 
        '4': 'Thursday', '5': 'Friday', '6': 'Saturday', '7': 'Sunday'
    }

    # 2. Map Date Ranges to "Ordinals" (The Logic Decoder)
    # 1-7 is always the 1st occurrence of a day, 8-14 is the 2nd, etc.
    ordinal_map = {
        "1-7": "1st",
        "8-14": "2nd",
        "15-21": "3rd",
        "22-28": "4th",
        "29-31": "5th"
    }

    for line in lines:
        line = line.strip()

        # Skip empty lines
        if not line:
            last_comment = ""
            continue

        # Capture Comments
        if line.startswith("#"):
            last_comment = line.lstrip("#").strip()
            continue

        # Process Cron Line
        if line[0].isdigit() or line[0] == '*' or line.startswith("@"):
            raw_schedule = ""
            command = ""
            human_desc = ""

            try:
                # --- SCENARIO A: @reboot / Special ---
                if line.startswith("@"):
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        raw_schedule = parts[0]
                        command = parts[1]
                        human_desc = "On System Event"
                    else:
                        command = line
                        human_desc = "Unknown"

                # --- SCENARIO B: Standard Cron ---
                else:
                    parts = line.split(maxsplit=5)
                    if len(parts) >= 6:
                        # Extract time parts for custom formatting
                        minute = parts[0]
                        hour = parts[1]
                        dom = parts[2] # Day of Month (e.g., 8-14,22-28)
                        
                        raw_schedule = " ".join(parts[:5])
                        command = parts[5]

                        # 1. Get Standard Translation first (as a baseline)
                        try:
                            human_desc = get_description(raw_schedule, opts)
                        except:
                            human_desc = f"`{raw_schedule}`"

                        # 2. CUSTOM LOGIC: Detect "Nth Weekday" Hack
                        # Pattern: [ "$(date +\%u)" = 5 ] && command
                        match = re.match(r'^\[\s*"\$\(date \+\\%u\)"\s*=\s*([1-7])\s*\]\s*&&\s*(.*)', command)
                        
                        if match:
                            day_num = match.group(1)   # e.g., "5"
                            # real_cmd = match.group(2) # The command after the check
                            day_name = week_map.get(day_num, "Day")

                            # Check if the Day-of-Month matches standard ordinal ranges
                            dom_parts = dom.split(',')
                            found_ordinals = []
                            
                            for part in dom_parts:
                                if part in ordinal_map:
                                    found_ordinals.append(ordinal_map[part])
                            
                            # If we found valid ranges (e.g. 8-14), rewrite the description completely
                            if found_ordinals:
                                ord_str = " and ".join(found_ordinals) # "2nd and 4th"
                                
                                # Make time pretty (0 2 -> 02:00)
                                nice_hour = hour.zfill(2) if hour.isdigit() else hour
                                nice_min = minute.zfill(2) if minute.isdigit() else minute
                                time_str = f"{nice_hour}:{nice_min}"

                                # OVERWRITE with human logic
                                human_desc = f"At {time_str}, on the **{ord_str} {day_name}** of the month"
                            
                            else:
                                # Fallback: Logic is there, but range is weird (e.g., run every day but check for Friday)
                                human_desc += f" <br>**(⚠️ Condition: Only on {day_name}s)**"

                    else:
                        command = line
                        human_desc = "⚠️ Invalid Format"

                # Output Row
                if command:
                    task_name = f"**{last_comment}**" if last_comment else ""
                    # Escape pipes for markdown table
                    safe_command = command.replace("|", "\\|")
                    
                    # Truncate long commands
                    if len(safe_command) > 120:
                        safe_command = safe_command[:117] + "..."

                    content.append(f"| {task_name} | {human_desc} | `{safe_command}` |")
                    has_entries = True
                    last_comment = ""

            except Exception as e:
                content.append(f"| ⚠️ Error | Could not parse | `{line}` |")

    if not has_entries:
        content.append("*No active jobs found.*")

    content.append("\n")
    return content

# --- MAIN EXECUTION ---
final_md = [
    "# 📅 System Automation Schedule",
    "> 🤖 Auto-generated by `cron_translator.py`",
    ""
]

final_md.extend(parse_crontab("user_crontab.txt", "👤 User Cron (gravi-ctrl)"))
final_md.extend(parse_crontab("root_crontab.txt", "⚡ Root Cron"))

with open(OUTPUT_FILE, 'w') as f:
    f.write("\n".join(final_md))

print(f"Schedule generated at: {OUTPUT_FILE}")
