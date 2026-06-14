#!/usr/bin/env python3
# @DESCRIPTION: Creates a human-readable .MD file of every script and its function, env dependencies, and mismatch warnings
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
import os
import re
import collections
import getpass
from pathlib import Path

# Get the Root Dir
ROOT_DIR = os.path.dirname(os.path.realpath(__file__))
OUTPUT_FILE = os.path.join(ROOT_DIR, "SCRIPTS_INVENTORY.md")
ENV_EXAMPLE_FILE = os.path.join(ROOT_DIR, ".env.example")

BACKUP_USER = getpass.getuser()
EXTENSIONS = {".sh", ".py"}

# Names that are valid @USED_BY consumers but are NOT script files.
NON_SCRIPT_CONSUMERS = {
    "crontab",
}

def is_script_file(file_path):
    if file_path.suffix in EXTENSIONS:
        return True

    if file_path.suffix == "":
        try:
            with open(file_path, 'rb') as f:
                return f.read(2) == b'#!'
        except:
            return False

    return False

def get_metadata(file_path):
    """Reads @DESCRIPTION, @FREQUENCY, @CRON, and @USES_ENV from a script's header."""
    desc = None
    freq = None
    cron_ctx = None
    uses_env = []

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue

                upper = stripped.upper()
                if "@DESCRIPTION:" in upper:
                    desc = stripped.split(":", 1)[1].strip()
                elif "@FREQUENCY:" in upper:
                    freq = stripped.split(":", 1)[1].strip()
                elif "@CRON:" in upper:
                    cron_ctx = stripped.split(":", 1)[1].strip()
                elif "@USES_ENV:" in upper:
                    raw = stripped.split(":", 1)[1].strip()
                    uses_env = [v.strip() for v in re.split(r'[,\s]+', raw) if v.strip()]
    except:
        pass

    return desc, freq, uses_env, cron_ctx

def parse_env_example(env_path):
    env_used_by = {}   
    all_vars = set()
    pending_used_by = None

    if not os.path.exists(env_path):
        return env_used_by, all_vars

    try:
        with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except:
        return env_used_by, all_vars

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
            raw_scripts = used_by_match.group(1).strip().split('#')[0].strip()
            scripts = [s.strip() for s in re.split(r'[,\s]+', raw_scripts) if s.strip()]

            var_inline = re.match(r'^(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=', stripped.split('#')[0])
            if var_inline:
                var_name = var_inline.group(1)
                all_vars.add(var_name)
                env_used_by[var_name] = scripts
            else:
                pending_used_by = scripts
            continue

        var_decl = re.match(r'^(?:export\s+)?([A-Z_][A-Z0-9_]*)(?:\s*=.*)?$', stripped.split('#')[0].strip())
        if var_decl and ('=' in stripped.split('#')[0] or stripped.startswith('export ')):
            var_name = var_decl.group(1)
            all_vars.add(var_name)
            if pending_used_by is not None:
                env_used_by[var_name] = pending_used_by
            continue

        if not stripped.startswith('#'):
            pending_used_by = None

    return env_used_by, all_vars

def generate_inventory():
    env_used_by, all_env_vars = parse_env_example(ENV_EXAMPLE_FILE)
    has_env = bool(all_env_vars)

    env_declares_for = collections.defaultdict(set)
    for var, scripts in env_used_by.items():
        for script in scripts:
            env_declares_for[script].add(var)

    categorized_inventory = collections.defaultdict(list)
    undocumented = []
    all_scripts_uses_env = {}

    for root, _, files in os.walk(ROOT_DIR):
        for file in files:
            file_path = Path(root) / file
            if not is_script_file(file_path):
                continue

            desc, freq, uses_env, cron_ctx = get_metadata(file_path)
            rel_path = file_path.relative_to(ROOT_DIR)
            rel_str = str(rel_path)
            script_name = file  

            if uses_env:
                all_scripts_uses_env[rel_str] = set(uses_env)

            if desc is None or freq is None:
                undocumented.append(rel_str)
                continue

            category = rel_path.parent.name if rel_path.parent.name else "Core Scripts"
            categorized_inventory[category].append({
                "script": file,
                "script_name": script_name,
                "desc": desc,
                "freq": freq,
                "cron_ctx": cron_ctx,
                "full_path": rel_str,
                "uses_env": set(uses_env),
            })

    mismatches = []

    if has_env:
        all_script_names = set()
        script_name_to_uses_env = {}

        for category_items in categorized_inventory.values():
            for item in category_items:
                all_script_names.add(item["script_name"])
                if item["uses_env"]:
                    script_name_to_uses_env[item["script_name"]] = item["uses_env"]

        for script_name, uses_env_vars in script_name_to_uses_env.items():
            if script_name in NON_SCRIPT_CONSUMERS:
                continue

            declared_vars = env_declares_for.get(script_name, set())

            only_in_script = uses_env_vars - declared_vars
            only_in_env    = declared_vars - uses_env_vars

            if only_in_script or only_in_env:
                mismatches.append({
                    "script": script_name,
                    "only_in_script": sorted(only_in_script),
                    "only_in_env": sorted(only_in_env),
                })

        for script_name, declared_vars in env_declares_for.items():
            if script_name in NON_SCRIPT_CONSUMERS:
                continue
            if script_name not in script_name_to_uses_env and declared_vars:
                mismatches.append({
                    "script": script_name,
                    "only_in_script": [],
                    "only_in_env": sorted(declared_vars),
                    "note": "No `@USES_ENV` tag found in script"
                })

    env_var_reference = collections.defaultdict(set)

    for var, scripts in env_used_by.items():
        for s in scripts:
            env_var_reference[var].add(s)

    for category_items in categorized_inventory.values():
        for item in category_items:
            for var in item["uses_env"]:
                env_var_reference[var].add(item["script_name"])

    md = [
        "# 📂 Script Inventory",
        "> 🤖 Auto-generated by `script_indexer.py`\n"
    ]

    has_uses_env_data = any(
        item["uses_env"]
        for items in categorized_inventory.values()
        for item in items
    )
    show_env_col = has_env and has_uses_env_data

    for category in sorted(categorized_inventory.keys()):
        md.append(f"### 📁 {category.title()}")

        # Added the "Crontab" column
        if show_env_col:
            md.append("| Script File | Purpose | Frequency | Crontab | Env Dependencies |")
            md.append("| :--- | :--- | :--- | :--- | :--- |")
        else:
            md.append("| Script File | Purpose | Frequency | Crontab |")
            md.append("| :--- | :--- | :--- | :--- |")

        for item in sorted(categorized_inventory[category], key=lambda x: x['script']):
            
            # Format the Cron Badge cleanly (supports single or comma-separated values)
            cron_badge = "—"
            if item["cron_ctx"]:
                parts = [p.strip() for p in item["cron_ctx"].split(",") if p.strip()]
                badges = []
                for part in parts:
                    p_lower = part.lower()
                    if p_lower == "root":
                        badges.append("⚡ Root")
                    elif p_lower == "user":
                        badges.append("👤 User")
                    else:
                        badges.append(part)
                cron_badge = ", ".join(badges)

            if show_env_col:
                if item["uses_env"]:
                    env_badges = " ".join(f"`{v}`" for v in sorted(item["uses_env"]))
                else:
                    env_badges = "—"
                md.append(f"| `{item['full_path']}` | {item['desc']} | {item['freq']} | {cron_badge} | {env_badges} |")
            else:
                md.append(f"| `{item['full_path']}` | {item['desc']} | {item['freq']} | {cron_badge} |")

        md.append("\n")

    if env_var_reference:
        md.append("---\n")
        md.append("## 🔑 Environment Variable Reference")
        md.append("> *Cross-referenced from `.env.example` (`@USED_BY`) and scripts (`@USES_ENV`).*\n")
        md.append("| Variable | Used By |")
        md.append("| :--- | :--- |")

        for var in sorted(env_var_reference.keys()):
            scripts_list = " ".join(f"`{s}`" for s in sorted(env_var_reference[var]))
            md.append(f"| `{var}` | {scripts_list} |")

        md.append("\n")

    if mismatches:
        md.append("---\n")
        md.append("## 🔴 Env Annotation Mismatches")
        md.append("> *Variables declared in one place but missing from the other.*")
        md.append("> - **Only in script** (`@USES_ENV`) → missing from `.env.example` `@USED_BY`")
        md.append("> - **Only in `.env`** (`@USED_BY`) → missing from script's `@USES_ENV`\n")

        for m in sorted(mismatches, key=lambda x: x['script']):
            note = m.get('note', '')
            header = f"#### `{m['script']}`" + (f" — _{note}_" if note else "")
            md.append(header)

            if m['only_in_script']:
                vars_fmt = " ".join(f"`{v}`" for v in m['only_in_script'])
                md.append(f"- 🟡 **Only in `@USES_ENV`** (add to `.env.example`): {vars_fmt}")

            if m['only_in_env']:
                vars_fmt = " ".join(f"`{v}`" for v in m['only_in_env'])
                md.append(f"- 🔵 **Only in `@USED_BY`** (add to script's `@USES_ENV`): {vars_fmt}")

            md.append("")

    if undocumented:
        md.append("---\n")
        md.append("## ⚠️ Undocumented Scripts")
        md.append("> *These scripts are missing `@DESCRIPTION:` or `@FREQUENCY:` tags.*")
        for u in sorted(undocumented):
            md.append(f"- `{u}`")

    with open(OUTPUT_FILE, 'w') as f:
        f.write("\n".join(md))

    total = sum(len(v) for v in categorized_inventory.values())
    print(f"✅ Inventory generated: {OUTPUT_FILE}")
    print(f"   {total} documented scripts across {len(categorized_inventory)} categories")
    if env_var_reference:
        print(f"   {len(env_var_reference)} env variables cross-referenced")
    if mismatches:
        print(f"   ⚠️  {len(mismatches)} env annotation mismatch(es) detected")
    if undocumented:
        print(f"   ⚠️  {len(undocumented)} undocumented script(s)")

if __name__ == "__main__":
    generate_inventory()