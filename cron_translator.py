#!/usr/bin/env python3
# @DESCRIPTION: Creates a human-readable .MD file of the crontabs
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)

import os
import re
import getpass
import collections
from cron_descriptor import get_description, Options

# --- CONFIG ---
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SOURCE_DIR = os.path.join(SCRIPT_DIR, "run_once", "system_configs")
ENV_EXAMPLE_FILE = os.path.join(SCRIPT_DIR, ".env.example")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "CRON_SCHEDULE.md")
BACKUP_USER = getpass.getuser()

opts = Options()
opts.use_24hour_time_format = True

# Env vars too ubiquitous to be worth listing in the reference table.
# Path shortcuts used by virtually every job — add any others here.
ENV_REF_SKIPLIST = {"S", "C", "A"}

# Frequency tier classification (checked in order — first match wins)
# Each entry: (label, matcher_fn)
# matcher_fn receives the raw cron expression (5-part string or @special)
FREQUENCY_TIERS = [
    ("⚡ Every few minutes",  lambda e: bool(re.match(r'\*/\d+\s', e)) and int(re.match(r'\*/(\d+)', e).group(1)) < 60),
    ("🕐 Hourly",             lambda e: bool(re.match(r'\d+\s\*\s', e))),
    ("🌙 Daily",              lambda e: bool(re.match(r'[\d,]+\s[\d,]+\s\*\s\*\s\*', e)) or e.startswith('@daily') or e.startswith('@midnight')),
    ("📅 Weekly",             lambda e: (not re.match(r'.+\*$', e) and bool(re.match(r'[\d,]+\s[\d,]+\s\*\s\*\s[\d,a-z]+', e, re.I))) or e.startswith('@weekly')),
    ("🗓️ Monthly",            lambda e: re.match(r'[\d,]+\s[\d,]+\s[\d,]+\s\*\s\*', e) or e.startswith('@monthly')),
    ("📆 Yearly",             lambda e: e.startswith('@yearly') or e.startswith('@annually')),
    ("🔁 On Reboot",          lambda e: e.startswith('@reboot')),
]


def classify_frequency(raw_schedule):
    """Return the tier label for a raw cron expression."""
    expr = raw_schedule.strip()
    for label, matcher in FREQUENCY_TIERS:
        try:
            if matcher(expr):
                return label
        except Exception:
            pass
    return "🔀 Other"


# ---------------------------------------------------------------------------
# .env.example parser — extract vars tagged @USED_BY: crontab
# ---------------------------------------------------------------------------

def parse_env_for_crontab(env_path):
    """
    Returns a set of variable names that are declared with @USED_BY containing
    'crontab' in .env.example. Also returns the full var->scripts mapping so we
    can show which other scripts share the var.
    """
    crontab_vars = set()      # vars where crontab is listed as a consumer
    var_all_consumers = {}    # var -> list of all @USED_BY scripts (for the ref table)
    pending_used_by = None

    if not os.path.exists(env_path):
        return crontab_vars, var_all_consumers

    try:
        with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception:
        return crontab_vars, var_all_consumers

    for line in lines:
        stripped = line.strip()

        if not stripped:
            pending_used_by = None
            continue

        if re.match(r'^#\s*[=\-]{3,}', stripped):
            pending_used_by = None
            continue

        used_by_match = re.search(r'@USED_BY:\s*(.+)', stripped, re.IGNORECASE)
        if used_by_match:
            raw = used_by_match.group(1).strip().split('#')[0].strip()
            scripts = [s.strip() for s in re.split(r'[,\s]+', raw) if s.strip()]

            var_inline = re.match(r'^(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=', stripped.split('#')[0])
            if var_inline:
                var_name = var_inline.group(1)
                var_all_consumers[var_name] = scripts
                if 'crontab' in [s.lower() for s in scripts]:
                    crontab_vars.add(var_name)
            else:
                pending_used_by = scripts
            continue

        var_decl = re.match(r'^(?:export\s+)?([A-Z_][A-Z0-9_]*)(?:\s*=.*)?$', stripped.split('#')[0].strip())
        if var_decl and ('=' in stripped.split('#')[0] or stripped.startswith('export ')):
            var_name = var_decl.group(1)
            if pending_used_by is not None:
                var_all_consumers[var_name] = pending_used_by
                if 'crontab' in [s.lower() for s in pending_used_by]:
                    crontab_vars.add(var_name)
            continue

        if not stripped.startswith('#'):
            pending_used_by = None

    return crontab_vars, var_all_consumers


# ---------------------------------------------------------------------------
# Crontab parser
# ---------------------------------------------------------------------------

def parse_crontab(filename, title, crontab_label):
    """
    Parse a crontab file and return:
      - content:      list of markdown lines for the main table
      - jobs:         list of dicts with structured job data (for at-a-glance + env ref)
    """
    content = []
    jobs = []
    file_path = os.path.join(SOURCE_DIR, filename)

    if not os.path.exists(file_path):
        return [], []

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
                        except Exception:
                            human_desc = f"`{raw_schedule}`"

                        date_match = re.match(
                            r'^\[\s*"\$\(date \+\\%[ua]\)"\s*=\s*"?(\w+)"?\s*\]\s*&&\s*(.*)',
                            command, re.IGNORECASE
                        )

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
                    task_label = last_comment if last_comment else command[:40]
                    task_name = f"**{last_comment}**" if last_comment else ""
                    safe_command = command.replace("|", "\\|")
                    if len(safe_command) > 120:
                        safe_command = safe_command[:117] + "..."

                    content.append(f"| {task_name} | {human_desc} | `{safe_command}` |")
                    has_entries = True

                    # Extract referenced env vars from command ($VAR or ${VAR})
                    referenced_vars = set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', command))

                    jobs.append({
                        "label":        task_label,
                        "raw_schedule": raw_schedule,
                        "human_desc":   human_desc,
                        "command":      command,
                        "crontab":      crontab_label,
                        "tier":         classify_frequency(raw_schedule),
                        "env_vars":     referenced_vars,
                    })

                    last_comment = ""

            except Exception:
                content.append(f"| ⚠️ Error | Could not parse | `{line}` |")

    if not has_entries:
        content.append("*No active jobs found.*")

    content.append("\n")
    return content, jobs


# ---------------------------------------------------------------------------
# At-a-Glance section
# ---------------------------------------------------------------------------

def build_at_a_glance(all_jobs):
    """Groups jobs by frequency tier into a summary table."""
    tier_jobs = collections.defaultdict(list)
    for job in all_jobs:
        tier_jobs[job["tier"]].append(job)

    if not tier_jobs:
        return []

    md = [
        "## ⏱️ Schedule At a Glance",
        "| Frequency Tier | Crontab | Jobs |",
        "| :--- | :--- | :--- |",
    ]

    # Preserve tier order from FREQUENCY_TIERS definition
    tier_order = [label for label, _ in FREQUENCY_TIERS] + ["🔀 Other"]
    for tier in tier_order:
        if tier not in tier_jobs:
            continue
        jobs_in_tier = tier_jobs[tier]

        # Group by crontab within the tier
        by_crontab = collections.defaultdict(list)
        for job in jobs_in_tier:
            by_crontab[job["crontab"]].append(job["label"])

        first = True
        for crontab_label, labels in by_crontab.items():
            jobs_str = "<br>".join(f"- *{l}*" for l in labels)
            tier_col = tier if first else ""
            md.append(f"| {tier_col} | {crontab_label} | {jobs_str} |")
            first = False

    md.append("\n")
    return md


# ---------------------------------------------------------------------------
# Env Var Reference section
# ---------------------------------------------------------------------------

def build_env_reference(all_jobs, crontab_vars, var_all_consumers):
    """
    Shows which env vars (tagged @USED_BY: crontab in .env.example) are
    referenced by which jobs, and which other scripts also share the var.
    Only includes vars that are actually referenced by at least one job.
    """
    if not crontab_vars:
        return []

    # Build: var -> { crontab_label -> [job labels] }
    var_job_map = collections.defaultdict(lambda: collections.defaultdict(list))
    vars_seen = set()

    for job in all_jobs:
        for var in job["env_vars"]:
            if var in crontab_vars and var not in ENV_REF_SKIPLIST:
                var_job_map[var][job["crontab"]].append(job["label"])
                vars_seen.add(var)

    if not vars_seen:
        return []

    md = [
        "---\n",
        "## 🔑 Env Var Reference",
        "> *Only vars tagged `@USED_BY: crontab` in `.env.example` that appear in at least one job.*\n",
        "| Variable | Used In | Jobs |",
        "| :--- | :--- | :--- |",
    ]

    for var in sorted(vars_seen):
        consumers = var_job_map[var]
        also_used_by = [s for s in var_all_consumers.get(var, []) if s.lower() != 'crontab']

        first = True
        for crontab_label, job_labels in consumers.items():
            var_col = f"`{var}`" if first else ""

            jobs_str = "<br>".join(f"- *{l}*" for l in job_labels)

            if first and also_used_by:
                also_str = " ".join(f"`{s}`" for s in also_used_by)
                jobs_str += f"<br>⚠️ _(also used by: {also_str})_"

            md.append(f"| {var_col} | {crontab_label} | {jobs_str} |")
            first = False

    md.append("\n")
    return md


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

crontab_vars, var_all_consumers = parse_env_for_crontab(ENV_EXAMPLE_FILE)

user_content, user_jobs = parse_crontab("user_crontab.txt", f"👤 User Cron ({BACKUP_USER})", f"👤 User ({BACKUP_USER})")
root_content, root_jobs = parse_crontab("root_crontab.txt", "⚡ Root Cron", "⚡ Root")
all_jobs = user_jobs + root_jobs

at_a_glance = build_at_a_glance(all_jobs)
env_reference = build_env_reference(all_jobs, crontab_vars, var_all_consumers)

final_md = [
    "# 📅 System Automation Schedule",
    "> 🤖 Auto-generated by `cron_translator.py`",
    ""
]

if at_a_glance:
    final_md.extend(at_a_glance)

final_md.extend(["---\n"])
final_md.extend(user_content)
final_md.extend(root_content)

if env_reference:
    final_md.extend(env_reference)

with open(OUTPUT_FILE, 'w') as f:
    f.write("\n".join(final_md))

print(f"Schedule generated at: {OUTPUT_FILE}")
