#!/usr/bin/env python3
# @DESCRIPTION: Creates a human-readable .MD file of the crontabs
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
import os
import re
from cron_descriptor import get_description, Options

# Load BACKUP_USER from .env
with open(os.path.join(os.path.dirname(__file__), ".env")) as f:
    BACKUP_USER = next(l.split('=')[1].strip().strip("'\"") for l in f if l.startswith('BACKUP_USER='))

# --- CONFIG ---
SOURCE_DIR = f"/home/{BACKUP_USER}/scripts/run_once/system_configs"
OUTPUT_FILE = f"{SOURCE_DIR}/CRON_SCHEDULE.md"

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

    week_map = {
        '1': 'Monday', '2': 'Tuesday', '3': 'Wednesday', 
        '4': 'Thursday', '5': 'Friday', '6': 'Saturday', '7': 'Sunday',
        'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday',
        'thu': 'Thursday', 'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday'
    }

    ordinal_map = {
        "1-7": "1st", "8-14": "2nd", "15-21": "3rd", 
        "22-28": "4th", "29-31": "5th"
    }

    for line in lines:
        line = line.strip()

        if not line:
            last_comment = ""
            continue

        if line.startswith("#"):
            last_comment = line.lstrip("#").strip()
            continue

        if line[0].isdigit() or line[0] == '*' or line.startswith("@"):
            raw_schedule = ""
            command = ""
            human_desc = ""

            try:
                if line.startswith("@"):
                    special_map = {
                        '@reboot':   'On every boot/restart',
                        '@yearly':   'Once a year (Jan 1, 00:00)',
                        '@annually': 'Once a year (Jan 1, 00:00)',
                        '@monthly':  'Once a month (1st, 00:00)',
                        '@weekly':   'Once a week (Sunday, 00:00)',
                        '@daily':    'Once a day (00:00)',
                        '@midnight': 'Once a day (00:00)',
                        '@hourly':   'Once an hour (:00)',
                    }
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        raw_schedule = parts[0]
                        command = parts[1]
                        human_desc = special_map.get(raw_schedule.lower(), f"Unknown: `{raw_schedule}`")
                    else:
                        command = line
                        human_desc = "Unknown"
                else:
                    parts = line.split(maxsplit=5)
                    if len(parts) >= 6:
                        minute, hour, dom, month, dow = parts[:5]
                        raw_schedule = " ".join(parts[:5])
                        command = parts[5]

                        try:
                            human_desc = get_description(raw_schedule, opts)
                        except:
                            human_desc = f"`{raw_schedule}`"

                        # Check for Day/Date Logic (Nth Weekday or Specific Day Text)
                        date_match = re.match(r'^\[\s*"\$\(date \+\\%[ua]\)"\s*=\s*"?(\w+)"?\s*\]\s*&&\s*(.*)', command, re.IGNORECASE)

                        if date_match:
                            day_val = date_match.group(1).lower()
                            day_name = week_map.get(day_val, "Day")

                            dom_parts = dom.split(',')
                            found_ordinals = [ordinal_map[p] for p in dom_parts if p in ordinal_map]

                            if found_ordinals:
                                ord_str = " and ".join(found_ordinals)
                                time_str = f"{hour.zfill(2)}:{minute.zfill(2)}"
                                human_desc = f"At {time_str}, on the **{ord_str} {day_name}** of the month"
                            else:
                                human_desc += f" <br>**(⚠️ Condition: Only on {day_name}s)**"

                        elif command.startswith("if [") or command.startswith("[ ") or command.startswith("test "):
                            human_desc += " <br>**(⚠️ Conditional: Bash Logic Check)**"

                        elif " | grep " in command or " || " in command:
                            human_desc += " <br>**(⚠️ Conditional: Pipeline Check)**"

                    else:
                        command = line
                        human_desc = "⚠️ Invalid Format"

                if command:
                    task_name = f"**{last_comment}**" if last_comment else ""
                    safe_command = command.replace("|", "\\|")
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

final_md.extend(parse_crontab("user_crontab.txt", f"👤 User Cron ({BACKUP_USER})"))
final_md.extend(parse_crontab("root_crontab.txt", "⚡ Root Cron"))

with open(OUTPUT_FILE, 'w') as f:
    f.write("\n".join(final_md))

print(f"Schedule generated at: {OUTPUT_FILE}")
