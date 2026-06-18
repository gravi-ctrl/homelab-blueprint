#!/usr/bin/env python3

# @DESCRIPTION: Generates a self-contained, interactive HTML dashboard of all homelab scripts, cron schedules, and environment variables.
# @FREQUENCY: On Demand (triggered by `backup-scripts-git.sh`)

import os
import re
import json
import argparse
import getpass
from pathlib import Path
from datetime import datetime, timezone

# ── Dependency check ──────────────────────────────────────────────
try:
    from cron_descriptor import get_description, Options
    cron_opts = Options()
    cron_opts.use_24hour_time_format = True
except ImportError:
    print("Missing dependency: cron_descriptor\nInstall: pip install cron-descriptor")
    exit(1)

# ── Configuration ─────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CRON_SOURCE_DIR = os.path.join(SCRIPT_DIR, "run_once", "system_configs")
ENV_EXAMPLE_FILE = os.path.join(SCRIPT_DIR, ".env.example")

BACKUP_USER = getpass.getuser()
SCRIPT_EXTS = {".sh", ".py"}

# Names that are valid @USED_BY consumers but are NOT script files.
NON_SCRIPT_CONSUMERS = {"crontab"}

# Frequency matching
FREQUENCY_TIERS = [
    ("⚡ Every few minutes", lambda e: bool(re.match(r'\*/\d+\s', e)) and int(re.match(r'\*/(\d+)', e).group(1)) < 60),
    ("🕐 Hourly",             lambda e: bool(re.match(r'\d+\s\*\s', e))),
    ("🌙 Daily",              lambda e: bool(re.match(r'[\d,]+\s[\d,]+\s\*\s\*\s\*', e)) or e.startswith('@daily') or e.startswith('@midnight')),
    ("📅 Weekly",             lambda e: (not re.match(r'.+\*$', e) and bool(re.match(r'[\d,]+\s[\d,]+\s\*\s\*\s[\d,a-z]+', e, re.I))) or e.startswith('@weekly')),
    ("🗓️ Monthly",            lambda e: re.match(r'[\d,]+\s[\d,]+\s[\d,]+\s\*\s\*', e) or e.startswith('@monthly')),
    ("📆 Yearly",             lambda e: e.startswith('@yearly') or e.startswith('@annually')),
    ("🔁 On Reboot",          lambda e: e.startswith('@reboot')),
]

def classify_frequency(raw_schedule):
    expr = raw_schedule.strip()
    for label, matcher in FREQUENCY_TIERS:
        try:
            if matcher(expr): return label
        except: pass
    return "🔀 Other"

def tier_sort_key(tier_label):
    for i, (label, _) in enumerate(FREQUENCY_TIERS):
        if label == tier_label: return i
    return 99

def get_semantic_category(desc, name):
    """
    Highly robust, multi-pass keyword matcher optimized against the real scripts.
    Guarantees every script lands in its most logical visual bucket.
    """
    text = (str(desc or "") + " " + str(name or "")).lower()
    fn = str(name or "").lower()
    
    # 1. Filename overrides for Core System configurations
    if any(k in fn for k in ['setup', 'bootstrap', 'configure']):
        return '⚙️ Core System & Automation'
        
    # 2. Maintenance & Cleaning tasks
    maint_keys = ['cleanup', 'purge', 'rm', 'prune', 'clean', 'delete', 'rotate', 'unused', 'trash', 'remove']
    if any(k in text for k in maint_keys):
        return '🧹 Maintenance'
        
    # 3. Backups, Repositories & Syncs
    backup_keys = ['backup', 'archive', 'sync', 'restore', 'recover', 'git', 'repo', 'push', 'pull', 'clone', 'rsync', 'ctrl_s_master', 'tar', 'zst', 'age-encrypted']
    # Filter 'health-snapshot' to prevent it from going into Backups & Sync
    if "health-snapshot" not in text and any(k in text for k in backup_keys + ['snapshot']):
        return '💾 Backups & Sync'
        
    # 4. Apps & Docker Containers
    docker_keys = ['docker', 'container', 'stack', 'compose', 'watch', 'nextcloud', 'app', 'volume', 'npm', 'n8n', 'tailscale']
    if any(k in text for k in docker_keys):
        return '🐳 Apps & Containers'
        
    # 5. Network, Vitals Monitoring, Alerts & Security
    net_keys = ['dns', 'network', 'ip', 'health', 'curl', 'kuma', 'alert', 'telegram', 'notify', 'guard', 'ping', 'port', 'host', 'ssh', 'bot', 'status', 'firewall', 'ufw', 'vpn', 'ssl', 'cert', 'ca', 'pihole', 'battery', 'monitor']
    if any(k in text for k in net_keys):
        return '🌐 Network & Monitoring'
        
    # 6. Default Fallback
    return '⚙️ Core System & Automation'

# ── Parsers ───────────────────────────────────────────────────────
def parse_env_example(env_path):
    env_used_by = {}
    if not os.path.exists(env_path): return env_used_by
    pending = None
    with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            stripped = line.strip()
            if not stripped or re.match(r'^#\s*[=\-]{3,}', stripped):
                pending = None; continue
            ub_match = re.search(r'@USED_BY:\s*(.+)', stripped, re.IGNORECASE)
            if ub_match:
                scripts = [s.strip() for s in re.split(r'[,\s]+', ub_match.group(1).split('#')[0].strip()) if s.strip()]
                v_inline = re.match(r'^(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=', stripped.split('#')[0])
                if v_inline:
                    env_used_by[v_inline.group(1)] = scripts
                else:
                    pending = scripts
                continue
            v_decl = re.match(r'^(?:export\s+)?([A-Z_][A-Z0-9_]*)(?:\s*=.*)?$', stripped.split('#')[0].strip())
            if v_decl and ('=' in stripped.split('#')[0] or stripped.startswith('export ')):
                if pending is not None: env_used_by[v_decl.group(1)] = pending
                continue
            if not stripped.startswith('#'): pending = None
    return env_used_by

def parse_scripts(env_used_by):
    scripts_data = []
    env_declares_for = {}
    for var, scripts in env_used_by.items():
        for script in scripts:
            env_declares_for.setdefault(script, set()).add(var)

    for root, _, files in os.walk(SCRIPT_DIR):
        if any(ignore in root for ignore in ['.git', '_archive', 'node_modules', 'venv']):
            continue
        for file in files:
            p = Path(root) / file
            if p.suffix not in SCRIPT_EXTS and not (p.suffix == "" and open(p, 'rb').read(2) == b'#!'):
                continue
            
            desc, freq, uses_env, cron_ctx = None, None, [], None
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    for _ in range(50): 
                        line_raw = f.readline()
                        if not line_raw: break 
                        line = line_raw.strip()
                        if not line: continue 
                        
                        up = line.upper()
                        if "@DESCRIPTION:" in up: desc = line.split(":", 1)[1].strip()
                        elif "@FREQUENCY:" in up: freq = line.split(":", 1)[1].strip()
                        elif "@CRON:" in up: cron_ctx = line.split(":", 1)[1].strip()
                        elif "@USES_ENV:" in up:
                            raw = line.split(":", 1)[1].strip()
                            uses_env = [v.strip() for v in re.split(r'[,\s]+', raw) if v.strip()]
            except: pass

            rel_str = str(p.relative_to(SCRIPT_DIR)).replace('\\', '/')
            
            # Map directory structure cleanly
            rel_dir = p.parent.relative_to(SCRIPT_DIR)
            dir_str = str(rel_dir).replace('\\', '/')
            if dir_str == '.':
                dir_str = "Core Scripts"

            cat = p.parent.name if p.parent.name else "Core Scripts"
            if cat == Path(SCRIPT_DIR).name: cat = "Core Scripts"
            
            warnings = []
            if not desc or not freq: warnings.append("Undocumented (missing @DESCRIPTION or @FREQUENCY)")
            
            declared_vars = env_declares_for.get(file, set())
            script_vars = set(uses_env)
            
            only_in_script = script_vars - declared_vars
            only_in_env = declared_vars - script_vars
            
            if only_in_script: warnings.append(f"Missing from .env.example: {', '.join(only_in_script)}")
            if only_in_env: warnings.append(f"Missing from @USES_ENV: {', '.join(only_in_env)}")

            scripts_data.append({
                "name": file,
                "path": rel_str,
                "dir_path": dir_str,
                "category": cat.replace('_', ' ').title(),
                "semantic_cat": get_semantic_category(desc, file),
                "desc": desc,
                "freq": freq,
                "cron_ctx": cron_ctx or "",
                "env": uses_env,
                "warnings": warnings
            })
    return scripts_data

def build_envs_data(env_used_by, scripts_data):
    env_dict = {}
    for var, scripts in env_used_by.items():
        if var not in env_dict: env_dict[var] = {"env_used": set(), "script_used": set()}
        env_dict[var]["env_used"].update(scripts)
        
    for s in scripts_data:
        for var in s["env"]:
            if var not in env_dict: env_dict[var] = {"env_used": set(), "script_used": set()}
            env_dict[var]["script_used"].add(s["name"])
            
    out = []
    for var, data in sorted(env_dict.items()):
        eu = sorted(list(data["env_used"]))
        su = sorted(list(data["script_used"]))
        
        eu_clean = {s for s in eu if s not in NON_SCRIPT_CONSUMERS}
        su_clean = {s for s in su if s not in NON_SCRIPT_CONSUMERS}
        mismatch = bool(eu_clean ^ su_clean)
        
        out.append({
            "name": var,
            "env_used": eu,
            "script_used": su,
            "mismatch": mismatch
        })
    return out

def expand_cron_slots(raw_schedule):
    """Given a 5-field cron expr (or @-shortcut), return list of [dow,hour] pairs
    representing when in the week this job fires. dow: 0=Sun..6=Sat, hour: 0-23.
    Used to build the weekly heatmap. Best-effort for complex/step expressions."""
    expr = (raw_schedule or "").strip()
    if not expr:
        return []
    shortcuts = {
        "@yearly":  [[0, 0]], "@annually": [[0, 0]],
        "@monthly": [[0, 0]],
        "@weekly":  [[0, 0]],
        "@daily":   [[d, 0] for d in range(7)],
        "@midnight":[[d, 0] for d in range(7)],
        "@hourly":  [[d, h] for d in range(7) for h in range(24)],
        "@reboot":  [],
    }
    low = expr.lower().split()[0]
    if low in shortcuts:
        return shortcuts[low]

    parts = expr.split()
    if len(parts) != 5:
        return []
    minute, hour, dom, month, dow = parts

    def expand_field(field, lo, hi):
        if field == "*":
            return list(range(lo, hi + 1))
        out = set()
        for chunk in field.split(","):
            try:
                if "/" in chunk:
                    base, step = chunk.split("/")
                    step = int(step)
                    if base == "*":
                        a, b = lo, hi
                    elif "-" in base:
                        a, b = map(int, base.split("-"))
                    else:
                        a, b = int(base), hi
                    out.update(v for v in range(a, b + 1) if (v - lo) % step == 0)
                elif "-" in chunk:
                    a, b = map(int, chunk.split("-"))
                    out.update(range(a, b + 1))
                else:
                    out.add(int(chunk))
            except (ValueError, IndexError):
                continue
        return sorted(out)

    dow_names = {"sun":0,"mon":1,"tue":2,"wed":3,"thu":4,"fri":5,"sat":6}
    def expand_dow(field):
        f = field.lower()
        for name, num in dow_names.items():
            f = f.replace(name, str(num))
        vals = expand_field(f, 0, 7)
        return sorted({v % 7 for v in vals})

    try:
        hours = expand_field(hour, 0, 23)
        dows = expand_dow(dow) if dow != "*" else list(range(7))
        # If day-of-month is restricted but day-of-week is wildcard, we don't know
        # which weekday it'll land on, so spread evenly across all days.
        if dom != "*" and dow == "*":
            dows = list(range(7))
        return [[d, h] for d in dows for h in hours]
    except Exception:
        return []


def parse_crontabs():
    crons_data = []
    
    # Custom translators for date conditions
    week_map = {
        '1': 'Monday', '2': 'Tuesday', '3': 'Wednesday', '4': 'Thursday', 
        '5': 'Friday', '6': 'Saturday', '7': 'Sunday',
        'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday',
        'thu': 'Thursday', 'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday'
    }
    ordinal_map = {
        "1-7": "1st", "8-14": "2nd", "15-21": "3rd", "22-28": "4th", "29-31": "5th"
    }

    def process_file(filename, owner_label, is_root):
        path = os.path.join(CRON_SOURCE_DIR, filename)
        if not os.path.exists(path): return
        last_comment = ""
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line: last_comment = ""; continue
                if line.startswith("#"): last_comment = line.lstrip("#").strip(); continue
                
                if line[0].isdigit() or line[0] == '*' or line.startswith("@"):
                    try:
                        raw_sched, cmd, desc = "", "", ""
                        if line.startswith("@"):
                            parts = line.split(maxsplit=1)
                            if len(parts) == 2:
                                raw_sched, cmd = parts[0], parts[1]
                                sm = {'@reboot':'On boot', '@yearly':'Once a year', '@monthly':'Once a month', '@weekly':'Once a week', '@daily':'Once a day', '@hourly':'Once an hour'}
                                desc = sm.get(raw_sched.lower(), raw_sched)
                        else:
                            parts = line.split(maxsplit=5)
                            if len(parts) >= 6:
                                minute, hour, dom, month, dow = parts[:5]
                                raw_sched = " ".join(parts[:5])
                                cmd = parts[5]
                                try: desc = get_description(raw_sched, cron_opts)
                                except: desc = raw_sched

                                # 1. Custom date-conditional parsing (e.g., 2nd & 4th Fridays)
                                date_match = re.match(
                                    r'^\[\s*"\$\(date \+\\%[ua]\)"\s*=\s*"?(\w+)"?\s*\]\s*&&\s*(.*)',
                                    cmd, re.IGNORECASE
                                )

                                if date_match:
                                    day_val = date_match.group(1).lower()
                                    day_name = week_map.get(day_val, "Day")
                                    dom_parts = dom.split(',')
                                    found_ordinals = [ordinal_map[p] for p in dom_parts if p in ordinal_map]
                                    if found_ordinals:
                                        ord_str = " and ".join(found_ordinals)
                                        time_str = f"{hour.zfill(2)}:{minute.zfill(2)}"
                                        desc = f"At {time_str}, on the **{ord_str} {day_name}** of the month"
                                    else:
                                        desc += f" <br>**(⚠️ Condition: Only on {day_name}s)**"

                                # 2. General Bash Conditionals
                                elif cmd.startswith("if [") or cmd.startswith("[ ") or cmd.startswith("test "):
                                    desc += " <br>**(⚠️ Conditional: Bash Logic Check)**"
                                    
                                # 3. Pipelines & Logical OR chains
                                elif " | grep " in cmd or " || " in cmd:
                                    desc += " <br>**(⚠️ Conditional: Pipeline Check)**"
                        
                        env_vars = list(set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', cmd)))
                        crons_data.append({
                            "label": last_comment if last_comment else cmd[:40],
                            "owner": owner_label,
                            "is_root": is_root,
                            "raw_schedule": raw_sched,
                            "human_desc": desc,
                            "command": cmd,
                            "tier": classify_frequency(raw_sched),
                            "tier_order": tier_sort_key(classify_frequency(raw_sched)),
                            "env": env_vars,
                            "heat_slots": expand_cron_slots(raw_sched),
                        })
                    except: pass
                    last_comment = ""
                    
    process_file("user_crontab.txt", f"👤 User Cron ({BACKUP_USER})", False)
    process_file("root_crontab.txt", "⚡ Root Cron", True)
    return sorted(crons_data, key=lambda x: (1 if x["is_root"] else 0, x["tier_order"]))

# ── HTML Template ─────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Homelab Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:       #fbfbfa;
  --bg-sub:   #f3f3f1;
  --surface:  #ffffff;
  --surface-2:#f6f6f4;
  --border:   rgba(20,20,18,.09);
  --border-2: rgba(20,20,18,.16);

  --ink:      #16161a;
  --ink-2:    #5a5a58;
  --ink-3:    #94938e;

  --accent:     #3c5a73;
  --accent-soft:#eef2f5;
  --accent-bd:  #c2d2dd;

  --ok:       #3d6b4c;
  --ok-soft:  #e9f1ea;
  --warn:     #8a5a1f;
  --warn-soft:#f7eee0;
  --danger:   #9c3b3b;
  --danger-soft:#f8eaea;
  --info:     #3c5a73;
  --info-soft:#eef2f5;

  --radius-sm: 6px;
  --radius:    10px;
  --radius-lg: 14px;

  --mono: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;

  /* heatmap intensity scale (light) */
  --heat-0: var(--bg-sub);
  --heat-1: #d8e3ea;
  --heat-2: #a9c2d2;
  --heat-3: #6e93a9;
  --heat-4: #3c5a73;
}

@media (prefers-color-scheme: dark){
  :root{
    --bg:        #0e0f11;
    --bg-sub:    #131416;
    --surface:   #16171a;
    --surface-2: #1b1c1f;
    --border:    rgba(255,255,255,.08);
    --border-2:  rgba(255,255,255,.14);

    --ink:   #ececea;
    --ink-2: #a3a29c;
    --ink-3: #6b6a65;

    --accent:     #8fb3cc;
    --accent-soft:#1b2933;
    --accent-bd:  #2e4655;

    --ok:        #8fc49e;
    --ok-soft:   #16261b;
    --warn:      #e0b773;
    --warn-soft: #2c2210;
    --danger:    #e08a8a;
    --danger-soft:#2c1717;
    --info:      #8fb3cc;
    --info-soft: #1b2933;

    --heat-0: var(--surface-2);
    --heat-1: #1b3140;
    --heat-2: #1f5270;
    --heat-3: #2c7aa3;
    --heat-4: #5fb4e0;
  }
}

html{background:var(--bg)}
body{font-family:var(--sans);font-size:14px;color:var(--ink);background:var(--bg);line-height:1.55;-webkit-font-smoothing:antialiased}
.page{max-width:1040px;margin:0 auto;padding:3rem 1.25rem 6rem}

/* ── Masthead ─────────────────────────────────────────── */
.masthead{margin-bottom:1.75rem;padding-bottom:1.1rem;border-bottom:2px solid var(--accent)}
.masthead-top{display:flex;align-items:baseline;gap:.6rem;margin-bottom:.4rem}
.brand-mark{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:22px;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
.meta-line{font-size:12.5px;color:var(--ink-3)}

/* ── Stat strip ───────────────────────────────────────── */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:1px;
  background:var(--border);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden;margin-bottom:1.5rem}
.stat{background:var(--surface);padding:.95rem 1.1rem}
.stat-n{font-size:24px;font-weight:600;letter-spacing:-.02em;color:var(--ink);font-family:var(--mono)}
.stat-l{font-size:11px;color:var(--ink-3);margin-top:3px;text-transform:uppercase;letter-spacing:.05em}
.stat.is-warn .stat-n{color:var(--warn)}

/* ── Tabs ─────────────────────────────────────────────── */
.nav-tabs{display:flex;gap:4px;margin-bottom:1.25rem;border-bottom:1px solid var(--border);overflow-x:auto}
.tab{background:none;border:none;color:var(--ink-2);font-size:14px;font-weight:500;cursor:pointer;
  padding:9px 4px;white-space:nowrap;position:relative;font-family:var(--sans);margin-right:22px;
  border-bottom:2px solid transparent;transition:color .15s,border-color .15s}
.tab:hover{color:var(--ink)}
.tab.active{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}

/* ── Filters ──────────────────────────────────────────── */
.filter-bar{display:flex;gap:8px;margin-bottom:1rem;flex-wrap:wrap;align-items:center}
.filter-bar.sub-filters{margin-top:-4px;margin-bottom:1.4rem}
.filter-bar input{flex:1;min-width:200px;padding:9px 13px;font-size:13px;font-family:var(--sans);
  border:1px solid var(--border-2);border-radius:var(--radius-sm);background:var(--surface);
  color:var(--ink);outline:none;transition:border-color .15s}
.filter-bar input::placeholder{color:var(--ink-3)}
.filter-bar input:focus{border-color:var(--accent)}
.fbtn{padding:7px 14px;font-size:12.5px;border:1px solid var(--border-2);border-radius:100px;
  background:var(--surface);color:var(--ink-2);cursor:pointer;white-space:nowrap;font-family:var(--sans);
  transition:background .15s,color .15s,border-color .15s}
.fbtn:hover{border-color:var(--ink-3)}
.fbtn.on{background:var(--accent-soft);color:var(--accent);border-color:var(--accent-bd);font-weight:600}
.fbtn.warn-on.on{background:var(--danger-soft);color:var(--danger);border-color:var(--danger)}
.sub-filter-group{display:none;gap:6px;flex-wrap:wrap;align-items:center}
.sub-filter-label{color:var(--ink-3);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-right:2px;font-weight:600}

/* ── Groups ───────────────────────────────────────────── */
.group-container{margin-bottom:18px;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);overflow:hidden}
.group-head{padding:.75rem 1rem;display:flex;justify-content:space-between;align-items:center;
  background:var(--surface-2);cursor:pointer;user-select:none;font-weight:600;font-size:14px;
  border-bottom:1px solid transparent}
.group-head:hover{background:var(--bg-sub)}
.group-head.is-open{border-bottom-color:var(--border)}
.group-body{padding:.9rem;background:var(--surface);display:none}
.group-body.is-open{display:grid;gap:9px}
.group-count{font-size:11.5px;font-weight:600;color:var(--ink-2);background:var(--bg-sub);
  padding:2px 9px;border-radius:100px;border:1px solid var(--border)}
.tier-header{font-size:11px;font-weight:600;margin:14px 0 7px;color:var(--ink-3);
  text-transform:uppercase;letter-spacing:.06em}
.tier-header:first-child{margin-top:0}

/* ── Overview narrative cards ─────────────────────────── */
.health-banner{display:flex;align-items:center;gap:10px;padding:13px 16px;border-radius:var(--radius);margin-bottom:22px;font-size:14px;font-weight:500}
.health-banner.ok{background:var(--ok-soft);color:var(--ok)}
.health-banner.warn{background:var(--warn-soft);color:var(--warn)}
.overview-title{font-size:18px;font-weight:600;margin-bottom:6px;color:var(--ink);letter-spacing:-.01em}
.overview-sub{font-size:13.5px;color:var(--ink-2);margin-bottom:20px}
.narrative-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);margin-bottom:10px;overflow:hidden}
.narrative-card summary{padding:13px 16px;font-size:14px;font-weight:600;cursor:pointer;
  display:flex;align-items:center;justify-content:space-between;user-select:none;background:var(--surface-2);list-style:none}
.narrative-card summary::-webkit-details-marker{display:none}
.narrative-card summary::after{content:'▾';font-size:11px;color:var(--ink-3);transition:transform .2s}
.narrative-card[open] summary::after{transform:rotate(180deg)}
.narrative-card[open] summary{border-bottom:1px solid var(--border)}
.narrative-body{padding:14px 16px}
.task-row{position:relative;padding-left:16px;margin-bottom:13px;font-size:13.5px;color:var(--ink);line-height:1.65}
.task-row:last-child{margin-bottom:0}
.task-row::before{content:"";position:absolute;left:0;top:.55em;width:5px;height:5px;border-radius:50%;background:var(--ink-3)}
.mono-script{font-family:var(--mono);font-size:12px;background:var(--bg-sub);padding:2px 6px;
  border-radius:4px;color:var(--ink);font-weight:600;border:1px solid var(--border)}
.time-badge{font-weight:600;color:var(--accent);background:var(--accent-soft);padding:2px 7px;
  border-radius:4px;font-size:10.5px;display:inline-block;margin-left:4px;letter-spacing:.02em;white-space:nowrap;vertical-align:middle}
.action-separator{color:var(--ink-3);font-weight:400;margin:0 5px}

/* ── Cards ────────────────────────────────────────────── */
.card{background:var(--bg-sub);border:1px solid var(--border);border-radius:var(--radius);
  padding:.85rem .95rem;display:flex;flex-direction:column;gap:7px}
.card.interactive{padding:0;cursor:pointer}
.card.interactive .card-head{padding:.7rem .95rem;display:flex;justify-content:space-between;align-items:center}
.card.interactive .card-head:hover{background:var(--surface-2)}
.card-body{padding:.7rem .95rem;border-top:1px solid var(--border);display:none;background:var(--surface)}
.card-body.open{display:block}
.card-title{font-weight:600;font-size:13.5px;display:flex;justify-content:space-between;
  align-items:flex-start;gap:10px;word-break:break-all;color:var(--ink)}
.card-sub{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.card-desc{font-size:12.5px;color:var(--ink-2);background:var(--surface);padding:7px 9px;
  border-radius:var(--radius-sm);border:1px solid var(--border)}
.badges{display:flex;gap:5px;flex-wrap:wrap;margin-top:auto;padding-top:3px}
.bdg{font-size:11px;padding:3px 9px;border-radius:100px;display:inline-flex;align-items:center;
  line-height:1.5;white-space:nowrap;font-weight:500;border:1px solid transparent}
.b-blue  {background:var(--info-soft);color:var(--info)}
.b-green {background:var(--ok-soft);color:var(--ok)}
.b-amber {background:transparent;color:var(--ink-2);border-color:var(--border-2);font-family:var(--mono);font-size:10.5px}
.b-red   {background:var(--danger-soft);color:var(--danger);font-weight:600}
.b-gray  {background:transparent;color:var(--ink-2);border-color:var(--border-2)}
.mono-cmd{font-family:var(--mono);font-size:11.5px;background:var(--bg-sub);padding:6px 8px;
  border-radius:var(--radius-sm);color:var(--ink);overflow-x:auto;white-space:pre-wrap;word-break:break-all;border:1px solid var(--border)}
.warning-list{margin:0;padding-left:14px;color:var(--danger);font-size:11.5px;margin-top:2px}
.detail{display:flex;gap:.7rem;font-size:12.5px;align-items:flex-start;margin-bottom:6px}
.dlabel{color:var(--ink-3);min-width:150px;flex-shrink:0;font-size:10px;text-transform:uppercase;
  letter-spacing:.06em;line-height:1.7;font-weight:600}
.dval{color:var(--ink);display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.view{display:none}
.view.active{display:block}
.no-match{color:var(--ink-2);font-size:13px;padding:3rem 0;text-align:center;display:none}

/* ── Guide card ───────────────────────────────────────── */
.guide-head{background:var(--accent-soft) !important;color:var(--accent) !important}
.guide-status{background:var(--accent-bd) !important;color:var(--accent) !important;
  font-size:9.5px !important;font-weight:700 !important;text-transform:uppercase;letter-spacing:.06em}

/* ── Heatmap ──────────────────────────────────────────── */
.heatmap-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);
  padding:1.1rem 1.2rem 1rem;margin-bottom:18px}
.heatmap-title{font-size:13px;font-weight:600;color:var(--ink);margin-bottom:2px}
.heatmap-sub{font-size:12px;color:var(--ink-3);margin-bottom:14px}
.heatmap-grid{display:grid;grid-template-columns:34px repeat(24,1fr);gap:3px;align-items:center}
.heatmap-grid .hm-corner{font-size:9px}
.heatmap-grid .hm-hour-label{font-size:9px;color:var(--ink-3);text-align:center;font-family:var(--mono)}
.heatmap-grid .hm-day-label{font-size:10.5px;color:var(--ink-2);text-align:right;padding-right:6px;font-weight:500}
.hm-cell{aspect-ratio:1;border-radius:3px;background:var(--heat-0);cursor:default;position:relative;border:1px solid var(--border)}
.hm-cell[data-level="1"]{background:var(--heat-1)}
.hm-cell[data-level="2"]{background:var(--heat-2)}
.hm-cell[data-level="3"]{background:var(--heat-3)}
.hm-cell[data-level="4"]{background:var(--heat-4)}
.hm-cell:hover::after{
  content:attr(data-tip);position:absolute;bottom:130%;left:50%;transform:translateX(-50%);
  background:var(--ink);color:var(--bg);font-size:11px;padding:4px 8px;border-radius:5px;
  white-space:nowrap;z-index:10;font-family:var(--sans);pointer-events:none;
}
.heatmap-legend{display:flex;align-items:center;gap:6px;margin-top:12px;font-size:10.5px;color:var(--ink-3)}
.heatmap-legend .hm-cell{width:11px;height:11px;aspect-ratio:unset;display:inline-block}
</style>
</head>
<body>
<div class="page">
  <div class="masthead">
    <div class="masthead-top">
      <span class="brand-mark">Homelab</span>
      <h1>Automation Dashboard</h1>
    </div>
    <div class="meta-line">Generated __DATE__</div>
  </div>

  <div class="stats" id="stats"></div>

  <div class="nav-tabs">
    <button class="tab active" onclick="switchTab('overview', this)">Overview</button>
    <button class="tab" onclick="switchTab('scripts', this)">Scripts</button>
    <button class="tab" onclick="switchTab('crons', this)">Cron schedule</button>
    <button class="tab" onclick="switchTab('envs', this)">Variables</button>
  </div>

  <div class="filter-bar" id="global-filter-bar">
    <input type="text" id="search" placeholder="Search names, paths, variables…" oninput="applyFilter()">
    <button class="fbtn warn-on" id="f-warn" onclick="tog('warn')">Warnings</button>
    <button class="fbtn" id="f-user" onclick="tog('user')">User</button>
    <button class="fbtn" id="f-root" onclick="tog('root')">Root</button>
  </div>

  <div class="filter-bar sub-filters" id="sub-filter-bar">
    <div id="sub-scripts" class="sub-filter-group" style="display:flex;flex-direction:column;gap:8px;width:100%">
      <div id="scripts-parent-dirs" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"></div>
      <div id="scripts-child-dirs" style="display:none;gap:6px;flex-wrap:wrap;align-items:center;padding-left:14px;border-left:2px solid var(--border)"></div>
    </div>
    <div id="sub-crons" class="sub-filter-group">
      <span class="sub-filter-label">Runtime</span>
      <button class="fbtn" id="f-docker" onclick="tog('docker')">Container tasks</button>
      <button class="fbtn" id="f-quick" onclick="tog('quick')">Frequent jobs</button>
    </div>
    <div id="sub-envs" class="sub-filter-group">
      <span class="sub-filter-label">Audit</span>
      <button class="fbtn" id="f-secrets" onclick="tog('secrets')">Secrets / tokens</button>
      <button class="fbtn" id="f-orphans" onclick="tog('orphans')">Unused (.env only)</button>
    </div>
  </div>

  <div id="view-overview" class="view active"></div>
  <div id="view-scripts" class="view"></div>
  <div id="view-crons" class="view"></div>
  <div id="view-envs" class="view"></div>
  <div class="no-match" id="no-match">No results match your filter.</div>
</div>

<script>
const D = __DATA__;
let activeTab = 'scripts';
const F = {
  warn: false, user: false, root: false,
  docker: false, quick: false,
  secrets: false, orphans: false
};

let activeParentDir = null;
let activeChildDir = null;

function e(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function bdg(cls, txt){ return `<span class="bdg ${cls}">${e(txt)}</span>`; }

function toggleGroup(id) {
  const head = document.getElementById(id+'-head');
  const body = document.getElementById(id+'-body');
  head.classList.toggle('is-open');
  body.classList.toggle('is-open');
  if (id === 'guide') {
    const status = document.getElementById('guide-status');
    if (status) status.textContent = head.classList.contains('is-open') ? 'Collapse' : 'Expand';
  }
}

/* ── Weekly heatmap: day-of-week × hour-of-day cron density ───────── */
function renderHeatmap(crons){
  const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const grid = Array.from({length:7}, () => Array(24).fill(0));
  const cellJobs = Array.from({length:7}, () => Array.from({length:24}, () => []));
  let totalSlots = 0;

  crons.forEach(c => {
    (c.heat_slots || []).forEach(([d,h]) => {
      if (d>=0 && d<7 && h>=0 && h<24){
        grid[d][h]++;
        cellJobs[d][h].push(c.label);
        totalSlots++;
      }
    });
  });

  if (totalSlots === 0) return '';

  const max = Math.max(1, ...grid.flat());
  function levelFor(n){
    if (n===0) return 0;
    const ratio = n/max;
    if (ratio > .75) return 4;
    if (ratio > .5)  return 3;
    if (ratio > .25) return 2;
    return 1;
  }

  let cellsHtml = '';
  // header row: corner + 24 hour labels (every 3rd hour to stay legible)
  cellsHtml += `<div class="hm-corner"></div>`;
  for (let h=0; h<24; h++){
    cellsHtml += `<div class="hm-hour-label">${h % 3 === 0 ? h : ''}</div>`;
  }
  for (let d=0; d<7; d++){
    cellsHtml += `<div class="hm-day-label">${DAYS[d]}</div>`;
    for (let h=0; h<24; h++){
      const n = grid[d][h];
      const level = levelFor(n);
      const jobs = cellJobs[d][h];
      const tip = n===0 ? '' : (n===1 ? jobs[0] : `${n} jobs: ${jobs.slice(0,4).join(', ')}${jobs.length>4?'…':''}`);
      cellsHtml += `<div class="hm-cell" data-level="${level}" data-tip="${e(tip)} ${n>0?('· '+String(h).padStart(2,'0')+':00'):''}"></div>`;
    }
  }

  return `<div class="heatmap-card">
    <div class="heatmap-title">When jobs fire</div>
    <div class="heatmap-sub">Density of scheduled cron triggers across the week — hour of day (UTC server time) × day of week</div>
    <div class="heatmap-grid">${cellsHtml}</div>
    <div class="heatmap-legend">
      <span>Fewer</span>
      <span class="hm-cell" data-level="0"></span>
      <span class="hm-cell" data-level="1"></span>
      <span class="hm-cell" data-level="2"></span>
      <span class="hm-cell" data-level="3"></span>
      <span class="hm-cell" data-level="4"></span>
      <span>More</span>
    </div>
  </div>`;
}

function renderOverview() {
  const scriptWarnCount = D.scripts.filter(s => s.warnings.length > 0).length;
  const envMismatchCount = D.envs.filter(ev => ev.mismatch).length;
  const totalIssues = scriptWarnCount + envMismatchCount;

  let bannerHtml;
  if (totalIssues === 0) {
    bannerHtml = `<div class="health-banner ok">Everything looks healthy — no issues found</div>`;
  } else {
    bannerHtml = `<div class="health-banner warn">${totalIssues} thing${totalIssues!==1?'s':''} could use a look — check the Scripts or Variables tab for details</div>`;
  }

  const grouped = {};
  D.scripts.forEach(s => {
    if (!s.desc || !s.freq) return;
    if (!grouped[s.semantic_cat]) grouped[s.semantic_cat] = [];
    grouped[s.semantic_cat].push(s);
  });

  let narrativeHtml = `
    <div class="overview-title">What runs on this server?</div>
    <div class="overview-sub">A categorized summary of background tasks, grouped by purpose.</div>
  `;

  Object.keys(grouped).sort().forEach(category => {
    const scripts = grouped[category];
    narrativeHtml += `
      <details class="narrative-card" open>
        <summary>
          <span>${e(category)}</span>
          <span class="group-count">${scripts.length}</span>
        </summary>
        <div class="narrative-body">
    `;
    scripts.forEach(s => {
      const warnIcon = s.warnings.length ? `<span style="color:var(--danger);cursor:help;font-weight:600" title="${e(s.warnings.join(', '))}">⚠</span> ` : '';
      narrativeHtml += `
          <div class="task-row">
            ${warnIcon}<code class="mono-script">${e(s.name)}</code>
            <span class="action-separator">—</span>
            <span>${e(s.desc)}</span>
            <span class="time-badge">${e(s.freq)}</span>
          </div>
      `;
    });
    narrativeHtml += `</div></details>`;
  });

  const heatmapHtml = renderHeatmap(D.crons);

  document.getElementById('view-overview').innerHTML = `
    ${bannerHtml}
    ${heatmapHtml}
    ${narrativeHtml}
    <div style="text-align:center;margin-top:20px">
      <button class="fbtn" onclick="switchTab('scripts')" style="padding:9px 18px;font-size:13px">Explore full script inventory →</button>
    </div>
  `;
}

function renderStats() {
  const warns = D.scripts.filter(s => s.warnings.length > 0).length + D.envs.filter(e => e.mismatch).length;
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="stat-n">${D.scripts.length}</div><div class="stat-l">Scripts</div></div>
    <div class="stat"><div class="stat-n">${D.crons.length}</div><div class="stat-l">Cron jobs</div></div>
    <div class="stat"><div class="stat-n">${D.envs.length}</div><div class="stat-l">Variables</div></div>
    <div class="stat${warns>0?' is-warn':''}"><div class="stat-n">${warns}</div><div class="stat-l">Warnings</div></div>
  `;
}

function renderScripts() {
  const grouped = {};
  D.scripts.forEach((s, idx) => {
    s._idx = idx;
    if (!grouped[s.category]) grouped[s.category] = [];
    grouped[s.category].push(s);
  });

  const cats = Object.keys(grouped).sort((a,b) => {
    if (a === "Core Scripts") return -1;
    if (b === "Core Scripts") return 1;
    return a.localeCompare(b);
  });

  let html = `
  <div class="group-container" id="guide-container">
    <div class="group-head guide-head" id="guide-head" onclick="toggleGroup('guide')">
      <div>How to document new scripts</div>
      <span class="group-count guide-status" id="guide-status">Expand</span>
    </div>
    <div class="group-body" id="guide-body" style="grid-template-columns:1fr">
      <div style="color:var(--ink-2);margin-bottom:10px;font-size:13px">To automatically include a new <code>.sh</code> or <code>.py</code> script in this inventory, add this header block right below your shebang:</div>
      <div class="mono-cmd">#!/bin/bash
# @DESCRIPTION: One-line summary of what the script does.
# @FREQUENCY:   How often it runs (e.g., Daily 5am, On Demand, etc.)
# @CRON:        User, Root       # (Optional) Which crontab it runs in
# @USES_ENV:    VAR1, VAR2       # (Optional) Config variables it depends on</div>
    </div>
  </div>
  `;

  cats.forEach((cat, c_idx) => {
    const scripts = grouped[cat];
    const gid = `s-cat-${c_idx}`;
    const warnsInCat = scripts.filter(s => s.warnings.length > 0).length;
    const warnBadge = warnsInCat > 0 ? bdg('b-red', `${warnsInCat} warning${warnsInCat!==1?'s':''}`) : '';

    html += `<div class="group-container ui-group" data-group="${gid}">
      <div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')">
        <div>${e(cat)}</div>
        <div style="display:flex;gap:8px;align-items:center">${warnBadge}<span class="group-count">${scripts.length}</span></div>
      </div>
      <div class="group-body is-open" id="${gid}-body">`;

    scripts.forEach(s => {
      const badges = [];
      if (s.freq) badges.push(bdg('b-green', s.freq));
      if (s.cron_ctx) {
        if (s.cron_ctx.toLowerCase().includes('root')) badges.push(bdg('b-red', 'Root'));
        if (s.cron_ctx.toLowerCase().includes('user')) badges.push(bdg('b-blue', 'User'));
      }
      s.env.forEach(ev => badges.push(bdg('b-amber', ev)));

      const warnHtml = s.warnings.length
        ? `<div class="card-desc" style="border-color:var(--danger);background:var(--danger-soft)"><ul class="warning-list">${s.warnings.map(w=>`<li>${e(w)}</li>`).join('')}</ul></div>`
        : '';

      html += `<div class="card item-card" data-idx="${s._idx}">
        <div class="card-title"><span>${e(s.name)}</span></div>
        <div class="card-sub">${e(s.path)}</div>
        ${s.desc ? `<div class="card-desc">${e(s.desc)}</div>` : ''}
        ${warnHtml}
        <div class="badges">${badges.join('')}</div>
      </div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-scripts').innerHTML = html;
}

function renderCrons() {
  const grouped = { "User cron": [], "Root cron": [] };
  D.crons.forEach((c, idx) => {
    c._idx = idx;
    const key = c.is_root ? "Root cron" : "User cron";
    grouped[key].push(c);
  });

  let html = renderHeatmap(D.crons);

  Object.keys(grouped).forEach((owner, c_idx) => {
    if (grouped[owner].length === 0) return;
    const gid = `c-cat-${c_idx}`;
    html += `<div class="group-container ui-group" data-group="${gid}">
      <div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')">
        <div>${e(owner)}</div>
        <span class="group-count">${grouped[owner].length} jobs</span>
      </div>
      <div class="group-body is-open" id="${gid}-body">`;

    let currentTier = null;
    const list = grouped[owner];
    list.forEach((c, i) => {
      if (c.tier !== currentTier) {
        currentTier = c.tier;
        html += `<div class="tier-header ui-tier" data-tier="${c.tier}">${e(c.tier)}</div><div class="ui-tier-grid" style="display:grid;gap:9px;margin-bottom:12px">`;
      }
      const badges = c.env.map(ev => bdg('b-amber', ev));
      html += `<div class="card item-card" data-idx="${c._idx}">
        <div class="card-title"><span>${e(c.label)}</span><span class="card-sub" style="color:var(--ink-2)">${e(c.human_desc)}</span></div>
        <div class="card-sub">${e(c.raw_schedule)}</div>
        <div class="mono-cmd">${e(c.command)}</div>
        <div class="badges">${badges.join('')}</div>
      </div>`;
      const next = list[i+1];
      if (!next || next.tier !== currentTier) html += `</div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-crons').innerHTML = html;
}

function renderEnvs() {
  const grouped = { "Action required (mismatches)": [], "Healthy variables": [] };
  D.envs.forEach((env, idx) => {
    env._idx = idx;
    if (env.mismatch) grouped["Action required (mismatches)"].push(env);
    else grouped["Healthy variables"].push(env);
  });

  let html = '';
  Object.keys(grouped).forEach((cat, c_idx) => {
    if (grouped[cat].length === 0) return;
    const gid = `e-cat-${c_idx}`;
    html += `<div class="group-container ui-group" data-group="${gid}">
      <div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')">
        <div>${e(cat)}</div>
        <span class="group-count">${grouped[cat].length}</span>
      </div>
      <div class="group-body is-open" id="${gid}-body">`;

    grouped[cat].forEach(env => {
      const headerBadge = env.mismatch ? bdg('b-red', 'Mismatch') : '';
      const b1 = env.env_used.map(s => bdg('b-blue', s)).join(' ');
      const b2 = env.script_used.map(s => bdg('b-green', s)).join(' ');

      html += `<div class="card interactive item-card" data-idx="${env._idx}">
        <div class="card-head" onclick="document.getElementById('eb${env._idx}').classList.toggle('open')">
          <div style="font-weight:600;font-family:var(--mono);font-size:13px">${e(env.name)}</div>
          <div>${headerBadge}</div>
        </div>
        <div class="card-body" id="eb${env._idx}">
          <div class="detail"><span class="dlabel">In .env.example</span><span class="dval">${b1 || '<span style="color:var(--ink-3)">—</span>'}</span></div>
          <div class="detail"><span class="dlabel">In script @USES_ENV</span><span class="dval">${b2 || '<span style="color:var(--ink-3)">—</span>'}</span></div>
        </div>
      </div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-envs').innerHTML = html;
}

function renderDirectoryFilters() {
  const parentContainer = document.getElementById('scripts-parent-dirs');
  const childContainer = document.getElementById('scripts-child-dirs');
  if (!parentContainer) return;

  const allPaths = [...new Set(D.scripts.map(s => s.dir_path))];
  const topLevelsSet = new Set();
  allPaths.forEach(p => {
    if (p === "Core Scripts") topLevelsSet.add(p);
    else topLevelsSet.add(p.split('/')[0]);
  });
  const topLevels = [...topLevelsSet].sort();

  let parentHtml = '<span class="sub-filter-label">Folders</span>';
  parentHtml += `<button class="fbtn ${activeParentDir === null ? 'on' : ''}" onclick="selectParentDir(null)">All</button>`;
  topLevels.forEach(p => {
    const label = p === "Core Scripts" ? p : p.replace(/_/g, ' ').replace(/-/g, ' ');
    const activeClass = activeParentDir === p ? 'on' : '';
    parentHtml += `<button class="fbtn ${activeClass}" onclick="selectParentDir('${p}')">${e(label)}</button>`;
  });
  parentContainer.innerHTML = parentHtml;

  if (activeParentDir && activeParentDir !== "Core Scripts") {
    const children = allPaths.filter(p => p.startsWith(activeParentDir + '/')).sort();
    if (children.length > 0) {
      childContainer.style.display = 'flex';
      let childHtml = `<span class="sub-filter-label" style="font-size:10px">Subfolders</span>`;
      childHtml += `<button class="fbtn ${activeChildDir === null ? 'on' : ''}" onclick="selectChildDir(null)">All ${e(activeParentDir.replace(/_/g, ' ').replace(/-/g, ' '))}</button>`;
      children.forEach(c => {
        const subName = c.substring(activeParentDir.length + 1);
        const label = subName.replace(/_/g, ' ').replace(/-/g, ' ');
        const activeClass = activeChildDir === c ? 'on' : '';
        childHtml += `<button class="fbtn ${activeClass}" onclick="selectChildDir('${c}')">${e(label)}</button>`;
      });
      childContainer.innerHTML = childHtml;
    } else {
      childContainer.style.display = 'none';
    }
  } else {
    childContainer.style.display = 'none';
  }
}

function selectParentDir(dir) {
  activeParentDir = dir;
  activeChildDir = null;
  renderDirectoryFilters();
  applyFilter();
}

function selectChildDir(dir) {
  activeChildDir = dir;
  renderDirectoryFilters();
  applyFilter();
}

function switchTab(t, btn) {
  activeTab = t;
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));

  if (btn) {
    btn.classList.add('active');
  } else {
    const defaultTab = Array.from(document.querySelectorAll('.tab')).find(el => el.getAttribute('onclick').includes(t));
    if (defaultTab) defaultTab.classList.add('active');
  }
  document.getElementById(`view-${t}`).classList.add('active');

  const globalBar = document.getElementById('global-filter-bar');
  const subBar = document.getElementById('sub-filter-bar');
  if (t === 'overview') {
    globalBar.style.display = 'none';
    subBar.style.display = 'none';
  } else {
    globalBar.style.display = 'flex';
    subBar.style.display = 'flex';
  }

  document.querySelectorAll('.sub-filter-group').forEach(el => el.style.display = 'none');
  const activeSub = document.getElementById(`sub-${t}`);
  if (activeSub) activeSub.style.display = 'flex';

  if (t !== 'scripts') {
    activeParentDir = null;
    activeChildDir = null;
  } else {
    renderDirectoryFilters();
  }
  if (t !== 'crons') { F.docker = false; F.quick = false; document.getElementById('f-docker').classList.remove('on'); document.getElementById('f-quick').classList.remove('on'); }
  if (t !== 'envs') { F.secrets = false; F.orphans = false; document.getElementById('f-secrets').classList.remove('on'); document.getElementById('f-orphans').classList.remove('on'); }

  applyFilter();
}

function tog(k) {
  F[k] = !F[k];
  document.getElementById('f-'+k).classList.toggle('on', F[k]);
  applyFilter();
}

function applyFilter() {
  const q = document.getElementById('search').value.toLowerCase();
  let count = 0;

  if (activeTab === 'scripts') {
    document.querySelectorAll('#view-scripts .item-card').forEach(el => {
      const s = D.scripts[el.dataset.idx];
      const txt = JSON.stringify(s).toLowerCase();
      const isRoot = s.cron_ctx && s.cron_ctx.toLowerCase().includes('root');
      const isUser = s.cron_ctx && s.cron_ctx.toLowerCase().includes('user');

      let dirMatch = true;
      if (activeParentDir) {
        if (activeParentDir === "Core Scripts") {
          dirMatch = (s.dir_path === "Core Scripts");
        } else if (activeChildDir) {
          dirMatch = (s.dir_path === activeChildDir);
        } else {
          dirMatch = (s.dir_path === activeParentDir || s.dir_path.startsWith(activeParentDir + '/'));
        }
      }

      const match = (!q || txt.includes(q)) &&
                    (!F.warn || s.warnings.length > 0) &&
                    (!F.root || isRoot) &&
                    (!F.user || isUser) &&
                    dirMatch;
      el.style.display = match ? '' : 'none';
      if (match) count++;
    });
  }
  else if (activeTab === 'crons') {
    document.querySelectorAll('#view-crons .item-card').forEach(el => {
      const c = D.crons[el.dataset.idx];
      const txt = JSON.stringify(c).toLowerCase();
      const hasDocker = /\bdocker\s+(exec|run|compose)\b/i.test(c.command);
      const isQuick = c.tier_order <= 1;

      const match = (!q || txt.includes(q)) &&
                    (!F.warn) &&
                    (!F.root || c.is_root) &&
                    (!F.user || !c.is_root) &&
                    (!F.docker || hasDocker) &&
                    (!F.quick || isQuick);
      el.style.display = match ? '' : 'none';
      if (match) count++;
    });

    document.querySelectorAll('#view-crons .ui-tier-grid').forEach(grid => {
      const hasVisible = Array.from(grid.querySelectorAll('.item-card')).some(c => c.style.display !== 'none');
      grid.style.display = hasVisible ? 'grid' : 'none';
      grid.previousElementSibling.style.display = hasVisible ? 'block' : 'none';
    });
  }
  else if (activeTab === 'envs') {
    document.querySelectorAll('#view-envs .item-card').forEach(el => {
      const env = D.envs[el.dataset.idx];
      const txt = JSON.stringify(env).toLowerCase();
      const isSecret = /token|key|pass|secret|auth|id/i.test(env.name);
      const isOrphan = env.script_used.length === 0;

      const match = (!q || txt.includes(q)) &&
                    (!F.warn || env.mismatch) &&
                    (!F.root) && (!F.user) &&
                    (!F.secrets || isSecret) &&
                    (!F.orphans || isOrphan);
      el.style.display = match ? '' : 'none';
      if (match) count++;
    });
  }

  document.querySelectorAll(`#view-${activeTab} .ui-group`).forEach(group => {
    const hasVisible = Array.from(group.querySelectorAll('.item-card')).some(c => c.style.display !== 'none');
    group.style.display = hasVisible ? 'block' : 'none';
  });

  const guide = document.getElementById('guide-container');
  if (guide) {
    const hasFilters = q || F.warn || F.root || F.user || activeParentDir;
    guide.style.display = (activeTab === 'scripts' && !hasFilters) ? 'block' : 'none';
  }

  document.getElementById('no-match').style.display = (activeTab !== 'overview' && !count) ? 'block' : 'none';
}

renderStats();
renderOverview();
renderScripts();
renderCrons();
renderEnvs();
switchTab('overview');

</script>
</body>
</html>
"""

def main():
    print("🔍 Scanning Homelab Environment...")
    env_vars_parsed = parse_env_example(ENV_EXAMPLE_FILE)
    scripts = parse_scripts(env_vars_parsed)
    envs = build_envs_data(env_vars_parsed, scripts)
    crons = parse_crontabs()
    
    warn_scripts = sum(1 for s in scripts if s["warnings"])
    mismatch_envs = sum(1 for e in envs if e["mismatch"])
    
    print(f"  ✓ Found {len(scripts)} Scripts ({warn_scripts} flagged with warnings)")
    print(f"  ✓ Found {len(crons)} Cron Jobs")
    print(f"  ✓ Found {len(envs)} Tracked Variables ({mismatch_envs} env mismatches detected)")
    
    out_path = Path("index.html")
    
    data = {
        "scripts": scripts,
        "crons": crons,
        "envs": envs
    }
    
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_out = HTML.replace("__DATE__", now).replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out_path.write_text(html_out, encoding="utf-8")
    
    print(f"\n✅ Dashboard generated: {out_path.resolve()}")

if __name__ == "__main__":
    main()
