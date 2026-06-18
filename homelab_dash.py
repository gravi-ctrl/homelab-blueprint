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

NON_SCRIPT_CONSUMERS = {"crontab"}

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
    text = (str(desc or "") + " " + str(name or "")).lower()
    fn = str(name or "").lower()
    if any(k in fn for k in ['setup', 'bootstrap', 'configure']): return '⚙️ Core System & Automation'
    if any(k in text for k in ['cleanup', 'purge', 'rm', 'prune', 'clean', 'delete', 'rotate', 'unused', 'trash', 'remove']): return '🧹 Maintenance'
    if "health-snapshot" not in text and any(k in text for k in ['backup', 'archive', 'sync', 'restore', 'recover', 'git', 'repo', 'push', 'pull', 'clone', 'rsync', 'ctrl_s_master', 'tar', 'zst', 'age-encrypted', 'snapshot']): return '💾 Backups & Sync'
    if any(k in text for k in ['docker', 'container', 'stack', 'compose', 'watch', 'nextcloud', 'app', 'volume', 'npm', 'n8n', 'tailscale']): return '🐳 Apps & Containers'
    if any(k in text for k in ['dns', 'network', 'ip', 'health', 'curl', 'kuma', 'alert', 'telegram', 'notify', 'guard', 'ping', 'port', 'host', 'ssh', 'bot', 'status', 'firewall', 'ufw', 'vpn', 'ssl', 'cert', 'ca', 'pihole', 'battery', 'monitor']): return '🌐 Network & Monitoring'
    return '⚙️ Core System & Automation'

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

    for root, dirs, files in os.walk(SCRIPT_DIR):
        dirs.sort()
        if any(ignore in root for ignore in ['.git', '_archive', 'node_modules', 'venv']):
            continue
        for file in sorted(files):
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
            rel_dir = p.parent.relative_to(SCRIPT_DIR)
            dir_str = str(rel_dir).replace('\\', '/')
            if dir_str == '.': dir_str = "Core Scripts"

            cat = p.parent.name if p.parent.name else "Core Scripts"
            if cat == Path(SCRIPT_DIR).name: cat = "Core Scripts"
            
            warnings = []
            if not desc or not freq: warnings.append("Undocumented (missing @DESCRIPTION or @FREQUENCY)")
            
            declared_vars = env_declares_for.get(file, set())
            script_vars = set(uses_env)
            only_in_script = script_vars - declared_vars
            only_in_env = declared_vars - script_vars
            
            if only_in_script: warnings.append(f"Missing from .env.example: {', '.join(sorted(only_in_script))}")
            if only_in_env: warnings.append(f"Missing from @USES_ENV: {', '.join(sorted(only_in_env))}")

            scripts_data.append({
                "name": file, "path": rel_str, "dir_path": dir_str,
                "category": cat.replace('_', ' ').title(), "semantic_cat": get_semantic_category(desc, file),
                "desc": desc, "freq": freq, "cron_ctx": cron_ctx or "",
                "env": uses_env, "warnings": warnings
            })
    return sorted(scripts_data, key=lambda x: (x["dir_path"], x["name"]))

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
        out.append({"name": var, "env_used": eu, "script_used": su, "mismatch": bool(eu_clean ^ su_clean)})
    return out

def expand_cron_slots(raw_schedule):
    expr = (raw_schedule or "").strip()
    if not expr: return []
    shortcuts = {
        "@yearly": [[0, 0]], "@annually": [[0, 0]], "@monthly": [[0, 0]], "@weekly": [[0, 0]],
        "@daily": [[d, 0] for d in range(7)], "@midnight": [[d, 0] for d in range(7)],
        "@hourly": [[d, h] for d in range(7) for h in range(24)], "@reboot": [],
    }
    low = expr.lower().split()[0]
    if low in shortcuts: return shortcuts[low]
    parts = expr.split()
    if len(parts) != 5: return []
    minute, hour, dom, month, dow = parts

    def expand_field(field, lo, hi):
        if field == "*": return list(range(lo, hi + 1))
        out = set()
        for chunk in field.split(","):
            try:
                if "/" in chunk:
                    base, step = chunk.split("/")
                    step = int(step)
                    if base == "*": a, b = lo, hi
                    elif "-" in base: a, b = map(int, base.split("-"))
                    else: a, b = int(base), hi
                    out.update(v for v in range(a, b + 1) if (v - lo) % step == 0)
                elif "-" in chunk:
                    a, b = map(int, chunk.split("-"))
                    out.update(range(a, b + 1))
                else: out.add(int(chunk))
            except: continue
        return sorted(out)

    dow_names = {"sun":0,"mon":1,"tue":2,"wed":3,"thu":4,"fri":5,"sat":6}
    def expand_dow(field):
        f = field.lower()
        for name, num in dow_names.items(): f = f.replace(name, str(num))
        vals = expand_field(f, 0, 7)
        return sorted({v % 7 for v in vals})

    try:
        hours = expand_field(hour, 0, 23)
        dows = expand_dow(dow) if dow != "*" else list(range(7))
        if dom != "*" and dow == "*": dows = list(range(7))
        return [[d, h] for d in dows for h in hours]
    except Exception: return []

def parse_crontabs():
    crons_data = []
    week_map = {'1': 'Monday', '2': 'Tuesday', '3': 'Wednesday', '4': 'Thursday', '5': 'Friday', '6': 'Saturday', '7': 'Sunday', 'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday', 'thu': 'Thursday', 'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday'}
    ordinal_map = {"1-7": "1st", "8-14": "2nd", "15-21": "3rd", "22-28": "4th", "29-31": "5th"}
    
    # Map all variants of Day of Week filters to JS-native Date.getDay() integers (0-6, 0=Sunday)
    dow_numeric_map = {
        '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 0, '0': 0,
        'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6, 'sun': 0
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
                        day_conditional = None
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

                                # Match any day-of-week custom conditional (e.g. date +%u, %w, %a, %A)
                                date_match = re.match(r'^\[\s*"\$\(date \+\\%[uwaA]\)"\s*=\s*"?(\w+)"?\s*\]\s*&&\s*(.*)', cmd, re.IGNORECASE)
                                if date_match:
                                    day_val = date_match.group(1).lower()
                                    day_conditional = dow_numeric_map.get(day_val)
                                    day_name = week_map.get(day_val, "Day")
                                    dom_parts = dom.split(',')
                                    found_ordinals = [ordinal_map[p] for p in dom_parts if p in ordinal_map]
                                    if found_ordinals: desc = f"At {hour.zfill(2)}:{minute.zfill(2)}, on the **{' and '.join(found_ordinals)} {day_name}** of the month"
                                    else: desc += f" <br>**(⚠️ Condition: Only on {day_name}s)**"
                                elif cmd.startswith("if [") or cmd.startswith("[ ") or cmd.startswith("test "): desc += " <br>**(⚠️ Conditional: Bash Logic Check)**"
                                elif " | grep " in cmd or " || " in cmd: desc += " <br>**(⚠️ Conditional: Pipeline Check)**"
                        
                        env_vars = sorted(list(set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', cmd))))
                        crons_data.append({
                            "label": last_comment if last_comment else cmd[:40],
                            "owner": owner_label, "is_root": is_root,
                            "raw_schedule": raw_sched, "human_desc": desc,
                            "command": cmd, "tier": classify_frequency(raw_sched),
                            "tier_order": tier_sort_key(classify_frequency(raw_sched)),
                            "env": env_vars, "heat_slots": expand_cron_slots(raw_sched),
                            "day_conditional": day_conditional, # Pass static parsed day index down safely
                        })
                    except: pass
                    last_comment = ""
    process_file("user_crontab.txt", f"👤 User Cron ({BACKUP_USER})", False)
    process_file("root_crontab.txt", "⚡ Root Cron", True)
    return sorted(crons_data, key=lambda x: (1 if x["is_root"] else 0, x["tier_order"], x["label"], x["command"]))

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
  --warn-bd:  #e0d8c8;
  --danger:   #9c3b3b;
  --danger-soft:#f8eaea;
  --danger-bd:#e0c8c8;
  --purple:   #875b9e;
  --purple-soft:#f4eef9;
  --purple-bd:  #e4d8f0;

  --info:     #3c5a73;
  --info-soft:#eef2f5;

  --radius-sm: 6px;
  --radius:    10px;
  --radius-lg: 14px;
  --mono: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
  --heat-0: var(--bg-sub); --heat-1: #d8e3ea; --heat-2: #a9c2d2; --heat-3: #6e93a9; --heat-4: #3c5a73;
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
    --warn-bd:   #594a33;
    --danger:    #e08a8a;
    --danger-soft:#2c1717;
    --danger-bd: #593333;
    --purple:    #b28bc9;
    --purple-soft:#2c1e36;
    --purple-bd:  #4a3359;

    --info:      #8fb3cc;
    --info-soft: #1b2933;
    --heat-0: var(--surface-2); --heat-1: #1b3140; --heat-2: #1f5270; --heat-3: #2c7aa3; --heat-4: #5fb4e0;
  }
}

html{background:var(--bg)}
body{font-family:var(--sans);font-size:14px;color:var(--ink);background:var(--bg);line-height:1.55;-webkit-font-smoothing:antialiased}
.page{max-width:1040px;margin:0 auto;padding:3rem 1.25rem 6rem}

.masthead{margin-bottom:1.75rem;padding-bottom:1.1rem;border-bottom:2px solid var(--accent)}
.masthead-top{display:flex;align-items:baseline;gap:.6rem;margin-bottom:.4rem}
.brand-mark{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:22px;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
.meta-line{font-size:12.5px;color:var(--ink-3)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:1px;background:var(--border);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:1.5rem}
.stat{background:var(--surface);padding:.95rem 1.1rem}
.stat-n{font-size:24px;font-weight:600;letter-spacing:-.02em;color:var(--ink);font-family:var(--mono)}
.stat-l{font-size:11px;color:var(--ink-3);margin-top:3px;text-transform:uppercase;letter-spacing:.05em}
.stat.is-warn .stat-n{color:var(--warn)}

.nav-tabs{display:flex;gap:4px;margin-bottom:1.25rem;border-bottom:1px solid var(--border);overflow-x:auto}
.tab{background:none;border:none;color:var(--ink-2);font-size:14px;font-weight:500;cursor:pointer;padding:9px 4px;white-space:nowrap;position:relative;font-family:var(--sans);margin-right:22px;border-bottom:2px solid transparent;transition:color .15s,border-color .15s}
.tab:hover{color:var(--ink)}
.tab.active{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}

.filter-bar{display:flex;gap:8px;margin-bottom:1rem;flex-wrap:wrap;align-items:center}
.filter-bar.sub-filters{margin-top:-4px;margin-bottom:1.4rem}
.filter-bar input{flex:1;min-width:200px;padding:9px 13px;font-size:13px;font-family:var(--sans);border:1px solid var(--border-2);border-radius:var(--radius-sm);background:var(--surface);color:var(--ink);outline:none;transition:border-color .15s}
.filter-bar input::placeholder{color:var(--ink-3)}
.filter-bar input:focus{border-color:var(--accent)}
.fbtn{padding:7px 14px;font-size:12.5px;border:1px solid var(--border-2);border-radius:100px;background:var(--surface);color:var(--ink-2);cursor:pointer;white-space:nowrap;font-family:var(--sans);transition:all .15s}
.fbtn:hover{border-color:var(--ink-3)}
.fbtn.on{background:var(--accent-soft);color:var(--accent);border-color:var(--accent-bd);font-weight:600}
.fbtn.warn-on.on{background:var(--danger-soft);color:var(--danger);border-color:var(--danger)}
.sub-filter-group{display:none;gap:6px;flex-wrap:wrap;align-items:center}
.sub-filter-label{color:var(--ink-3);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-right:2px;font-weight:600}

.group-container{margin-bottom:18px;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);overflow:hidden}
.group-head{padding:.75rem 1rem;display:flex;justify-content:space-between;align-items:center;background:var(--surface-2);cursor:pointer;user-select:none;font-weight:600;font-size:14px;border-bottom:1px solid transparent}
.group-head:hover{background:var(--bg-sub)}
.group-head.is-open{border-bottom-color:var(--border)}
.group-body{padding:.9rem;background:var(--surface);display:none}
.group-body.is-open{display:grid;gap:9px}
.group-count{font-size:11.5px;font-weight:600;color:var(--ink-2);background:var(--bg-sub);padding:2px 9px;border-radius:100px;border:1px solid var(--border)}
.tier-header{font-size:11px;font-weight:600;margin:14px 0 7px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.tier-header:first-child{margin-top:0}

.health-banner{display:flex;align-items:center;gap:10px;padding:13px 16px;border-radius:var(--radius);margin-bottom:22px;font-size:14px;font-weight:500}
.health-banner.ok{background:var(--ok-soft);color:var(--ok)}
.health-banner.warn{background:var(--warn-soft);color:var(--warn)}
.overview-title{font-size:18px;font-weight:600;margin-bottom:6px;color:var(--ink);letter-spacing:-.01em}
.overview-sub{font-size:13.5px;color:var(--ink-2);margin-bottom:20px}
.narrative-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);margin-bottom:10px;overflow:hidden}
.narrative-card summary{padding:13px 16px;font-size:14px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:space-between;user-select:none;background:var(--surface-2);list-style:none}
.narrative-card summary::-webkit-details-marker{display:none}
.narrative-card summary::after{content:'▾';font-size:11px;color:var(--ink-3);transition:transform .2s}
.narrative-card[open] summary::after{transform:rotate(180deg)}
.narrative-card[open] summary{border-bottom:1px solid var(--border)}
.narrative-body{padding:14px 16px}
.task-row{position:relative;padding-left:16px;margin-bottom:13px;font-size:13.5px;color:var(--ink);line-height:1.65}
.task-row:last-child{margin-bottom:0}
.task-row::before{content:"";position:absolute;left:0;top:.55em;width:5px;height:5px;border-radius:50%;background:var(--ink-3)}
.mono-script{font-family:var(--mono);font-size:12px;background:var(--bg-sub);padding:2px 6px;border-radius:4px;color:var(--ink);font-weight:600;border:1px solid var(--border)}
.time-badge{font-weight:600;color:var(--accent);background:var(--accent-soft);padding:2px 7px;border-radius:4px;font-size:10.5px;display:inline-block;margin-left:4px;letter-spacing:.02em;white-space:nowrap;vertical-align:middle}
.action-separator{color:var(--ink-3);font-weight:400;margin:0 5px}

.card{background:var(--bg-sub);border:1px solid var(--border);border-radius:var(--radius);padding:.85rem .95rem;display:flex;flex-direction:column;gap:7px}
.card.interactive{padding:0;cursor:pointer}
.card.interactive .card-head{padding:.7rem .95rem;display:flex;justify-content:space-between;align-items:center}
.card.interactive .card-head:hover{background:var(--surface-2)}
.card-body{padding:.7rem .95rem;border-top:1px solid var(--border);display:none;background:var(--surface)}
.card-body.open{display:block}
.card-title{font-weight:600;font-size:13.5px;display:flex;justify-content:space-between;align-items:flex-start;gap:10px;word-break:break-all;color:var(--ink)}
.card-sub{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.card-desc{font-size:12.5px;color:var(--ink-2);background:var(--surface);padding:7px 9px;border-radius:var(--radius-sm);border:1px solid var(--border)}

/* Dynamic Badges & Tiers */
.badges{display:flex;gap:5px;flex-wrap:wrap;margin-top:auto;padding-top:3px}
.bdg{font-size:10.5px;padding:3px 9px;border-radius:100px;display:inline-flex;align-items:center;line-height:1.4;white-space:nowrap;font-weight:500;border:1px solid transparent}
.b-amber { color: var(--warn); border-color: var(--warn-bd); background: rgba(224, 183, 115, 0.08); }
.b-gray  { color: var(--ink-2); border-color: var(--border-2); background: transparent; }
.b-blue  { color: var(--accent); border-color: var(--accent-bd); background: rgba(143, 179, 204, 0.08); }
.b-purple{ color: var(--purple); border-color: var(--purple-bd); background: rgba(178, 139, 201, 0.08); }
.b-red   { color: var(--danger); border-color: var(--danger-bd); background: rgba(224, 138, 138, 0.08); }
.b-user  { color: var(--info); border-color: transparent; background: var(--info-soft); }
.b-root  { color: var(--danger); border-color: transparent; background: var(--danger-soft); font-weight:600; }
.b-env   { color: var(--warn); border-color: transparent; background: var(--warn-soft); font-family: var(--mono); font-size: 10px; }

.mono-cmd{font-family:var(--mono);font-size:11.5px;background:var(--bg-sub);padding:6px 8px;border-radius:var(--radius-sm);color:var(--ink);overflow-x:auto;white-space:pre-wrap;word-break:break-all;border:1px solid var(--border)}
.warning-list{margin:0;padding-left:14px;color:var(--danger);font-size:11.5px;margin-top:2px}
.detail{display:flex;gap:.7rem;font-size:12.5px;align-items:flex-start;margin-bottom:6px}
.dlabel{color:var(--ink-3);min-width:150px;flex-shrink:0;font-size:10px;text-transform:uppercase;letter-spacing:.06em;line-height:1.7;font-weight:600}
.dval{color:var(--ink);display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.view{display:none}
.view.active{display:block}
.no-match{color:var(--ink-2);font-size:13px;padding:3rem 0;text-align:center;display:none}
.guide-head{background:var(--accent-soft) !important;color:var(--accent) !important}
.guide-status{background:var(--accent-bd) !important;color:var(--accent) !important;font-size:9.5px !important;font-weight:700 !important;text-transform:uppercase;letter-spacing:.06em}

/* ── Cron UI Specific ─────────────────────────────────────────── */
.cron-schedule-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;margin-bottom:18px}
.heatmap-header{display:flex;align-items:center;justify-content:space-between;padding:.75rem 1rem;background:var(--surface-2);border-bottom:1px solid var(--border);cursor:pointer;user-select:none}
.heatmap-header-left{display:flex;align-items:center;gap:10px}
.heatmap-title{font-size:14px;font-weight:700;color:var(--ink);letter-spacing:-.01em}
.heatmap-title-icon{font-size:15px}
.heatmap-toggle-arrow{font-size:11px;color:var(--ink-3);transition:transform .2s}
.heatmap-toggle-arrow.open{transform:rotate(180deg)}
.heatmap-body{padding:1.1rem 1rem 1rem}

.cron-filters-row{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px;}
.hm-freq-btn{padding:5px 12px;font-size:11.5px;border:1px solid var(--border-2);border-radius:100px;background:transparent;color:var(--ink-2);cursor:pointer;font-family:var(--sans);font-weight:500;transition:all .15s;display:inline-flex;align-items:center;opacity:0.6;white-space:nowrap}
.hm-freq-btn:hover{opacity:0.9;}
.hm-freq-btn.on{opacity:1;}
.hm-freq-btn.on[data-color="amber"] { color: var(--warn); border-color: var(--warn-soft); background: rgba(224, 183, 115, 0.1); }
.hm-freq-btn.on[data-color="gray"] { color: var(--ink); border-color: var(--ink-3); background: var(--surface-2); }
.hm-freq-btn.on[data-color="blue"] { color: var(--accent); border-color: var(--accent-bd); background: var(--accent-soft); }
.hm-freq-btn.on[data-color="purple"] { color: var(--purple); border-color: var(--purple-bd); background: var(--purple-soft); }
.hm-freq-btn.on[data-color="red"] { color: var(--danger); border-color: var(--danger-bd); background: var(--danger-soft); }

.cron-view-controls{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;}
.cron-view-tabs{display:flex;gap:5px;}
.cron-tab-btn{padding:5px 12px;font-size:12px;border:1px solid var(--border-2);border-radius:6px;background:transparent;color:var(--ink-2);cursor:pointer;font-weight:500;transition:all .15s;display:flex;align-items:center;gap:6px;}
.cron-tab-btn:hover{color:var(--ink);border-color:var(--border);}
.cron-tab-btn.active{background:var(--surface-2);color:var(--ink);font-weight:600;border-color:var(--border);}
.cron-content-pane{display:none;}
.cron-content-pane.active{display:block;}

/* Heatmap View */
.heatmap-grid{display:grid;grid-template-columns:30px repeat(24,1fr);gap:2px;align-items:center}
.heatmap-grid .hm-corner{font-size:9px}
.heatmap-grid .hm-hour-label{font-size:8.5px;color:var(--ink-3);text-align:center;font-family:var(--mono)}
.heatmap-grid .hm-day-label{font-size:10px;color:var(--ink-2);text-align:right;padding-right:5px;font-weight:500}
.hm-cell{aspect-ratio:1;border-radius:2px;background:var(--heat-0);cursor:default;position:relative;border:1px solid var(--border)}
.hm-cell[data-level="1"]{background:var(--heat-1)}
.hm-cell[data-level="2"]{background:var(--heat-2)}
.hm-cell[data-level="3"]{background:var(--heat-3)}
.hm-cell[data-level="4"]{background:var(--heat-4)}
.hm-cell:hover::after{content:attr(data-tip);position:absolute;bottom:130%;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);font-size:11px;padding:4px 8px;border-radius:5px;white-space:nowrap;z-index:10;font-family:var(--sans);pointer-events:none;}
.heatmap-legend{display:flex;align-items:center;gap:6px;margin-top:10px;font-size:10px;color:var(--ink-3)}
.heatmap-legend .hm-cell{width:10px;height:10px;aspect-ratio:unset;display:inline-block}

/* Monthly Calendar View */
.calendar-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.calendar-title{font-size:14.5px;font-weight:600;color:var(--ink)}
.calendar-nav{display:flex;gap:4px}
.calendar-nav button{background:var(--bg-sub);border:1px solid var(--border-2);border-radius:4px;padding:4px 10px;font-size:11.5px;cursor:pointer;color:var(--ink);transition:all .15s;font-weight:500;}
.calendar-nav button:hover{background:var(--surface-2);border-color:var(--ink-3)}
.calendar-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:1px;background:var(--border-2);border:1px solid var(--border-2);border-radius:var(--radius-sm);overflow:hidden}
.cal-header-day{background:var(--surface-2);text-align:center;padding:8px 0;font-size:10px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.cal-cell{background:var(--surface);min-height:90px;padding:6px;display:flex;flex-direction:column;gap:3px}
.cal-cell.other-month{background:var(--bg-sub);opacity:.5}
.cal-cell.today { background: rgba(143, 179, 204, 0.05); outline: 1px solid var(--accent); outline-offset: -1px; }
.cal-cell.today .cal-day-num { color: var(--accent); font-weight: 600; }
.cal-day-num{font-size:11px;font-weight:500;color:var(--ink-3);margin-bottom:2px;}
.cal-job{font-size:9.5px;padding:2px 5px;border-radius:3px;border:1px solid transparent;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.cal-more{font-size:9.5px;color:var(--ink-3);font-weight:600;padding-left:2px;margin-top:1px;}

/* Agenda View */
.agenda-section{margin-bottom:24px}
.agenda-header{font-size:10px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px}
.agenda-row{display:flex;align-items:center;gap:16px;padding:10px 4px;border-bottom:1px solid var(--border-2);}
.agenda-row:last-child{border-bottom:none;}
.agenda-time{font-family:var(--mono);font-size:12px;color:var(--ink-2);font-weight:600;min-width:40px;text-align:left;}
.agenda-title{font-size:13px;color:var(--ink);font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agenda-badge{flex-shrink:0}
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
const F = { warn: false, user: false, root: false, docker: false, quick: false, secrets: false, orphans: false };
let activeParentDir = null, activeChildDir = null;

function e(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function bdg(cls, txt){ return `<span class="bdg ${cls}">${e(txt)}</span>`; }

function getTierColor(tier) {
  if (!tier) return 'gray';
  if (tier.includes('few minutes')) return 'amber';
  if (tier.includes('Hourly')) return 'gray';
  if (tier.includes('Daily')) return 'blue';
  if (tier.includes('Weekly')) return 'purple';
  if (tier.includes('Monthly') || tier.includes('Yearly')) return 'red';
  return 'gray';
}

function getTierHex(colorId) {
  if (colorId === 'amber') return 'var(--warn)';
  if (colorId === 'blue') return 'var(--accent)';
  if (colorId === 'purple') return 'var(--purple)';
  if (colorId === 'red') return 'var(--danger)';
  return 'var(--ink-3)';
}

/* ── Client-Side JS Cron Engine (Deterministic builds) ────────────── */
function parseCronField(str, min, max) {
    if (str === '*' || str === '?') return null;
    let vals = new Set();
    str.split(',').forEach(part => {
        let step = 1, rangeStr = part;
        if (part.includes('/')) {
            const p = part.split('/'); rangeStr = p[0]; step = parseInt(p[1], 10);
        }
        let s = min, e = max;
        if (rangeStr !== '*' && rangeStr !== '?') {
            if (rangeStr.includes('-')) {
                const p = rangeStr.split('-'); s = parseInt(p[0], 10); e = parseInt(p[1], 10);
            } else { s = e = parseInt(rangeStr, 10); }
        }
        for (let i = s; i <= e; i += step) vals.add(i);
    });
    return vals;
}

function parseCron(expr) {
    expr = (expr||"").toLowerCase().trim();
    if (expr.startsWith('@')) {
        const map = {
            '@yearly': '0 0 1 1 *', '@annually': '0 0 1 1 *', '@monthly': '0 0 1 * *', 
            '@weekly': '0 0 * * 0', '@daily': '0 0 * * *', '@midnight': '0 0 * * *', '@hourly': '0 * * * *'
        };
        expr = map[expr] || expr;
        if (expr.startsWith('@')) return null;
    }
    const p = expr.split(/\s+/);
    if (p.length < 5) return null;
    try {
        let mstr = p[3].replace(/jan/g,'1').replace(/feb/g,'2').replace(/mar/g,'3').replace(/apr/g,'4').replace(/may/g,'5').replace(/jun/g,'6').replace(/jul/g,'7').replace(/aug/g,'8').replace(/sep/g,'9').replace(/oct/g,'10').replace(/nov/g,'11').replace(/dec/g,'12');
        let dstr = p[4].replace(/sun/g,'0').replace(/mon/g,'1').replace(/tue/g,'2').replace(/wed/g,'3').replace(/thu/g,'4').replace(/fri/g,'5').replace(/sat/g,'6');
        
        let dowVals = parseCronField(dstr, 0, 7);
        if (dowVals && dowVals.has(7)) { dowVals.delete(7); dowVals.add(0); }
        
        return {
            min: parseCronField(p[0], 0, 59), hr: parseCronField(p[1], 0, 23),
            dom: parseCronField(p[2], 1, 31), mon: parseCronField(mstr, 1, 12), dow: dowVals
        };
    } catch(err) { return null; }
}

function getDailyRunTimes(c, dateObj) {
    const parsed = c._parsed;
    if (!parsed) return [];
    
    // Command level AND constraint check (e.g. only runs if current weekday matches conditional day)
    if (c.day_conditional !== undefined && c.day_conditional !== null) {
        if (dateObj.getDay() !== c.day_conditional) return [];
    }
    
    const m = dateObj.getMonth() + 1, d = dateObj.getDate(), w = dateObj.getDay();
    if (parsed.mon && !parsed.mon.has(m)) return [];
    
    const domMatch = !parsed.dom || parsed.dom.has(d);
    const dowMatch = !parsed.dow || parsed.dow.has(w);
    let dayMatch = (parsed.dom && parsed.dow) ? (domMatch || dowMatch) : (domMatch && dowMatch);
    if (!dayMatch) return [];
    
    let times = [];
    let hrs = parsed.hr ? Array.from(parsed.hr) : Array.from({length:24},(_,i)=>i);
    let mins = parsed.min ? Array.from(parsed.min) : Array.from({length:60},(_,i)=>i);
    hrs.forEach(h => mins.forEach(m => times.push({h, m})));
    return times;
}

D.crons.forEach(c => c._parsed = parseCron(c.raw_schedule));

/* ── UI Logic ─────────────────────────────────────────────────────── */
function toggleGroup(id) {
  document.getElementById(id+'-head').classList.toggle('is-open');
  document.getElementById(id+'-body').classList.toggle('is-open');
  if(id==='guide') document.getElementById('guide-status').textContent = document.getElementById(id+'-head').classList.contains('is-open') ? 'Collapse' : 'Expand';
}

const HEATMAP_TIERS = ["⚡ Every few minutes", "🕐 Hourly", "🌙 Daily", "📅 Weekly", "🗓️ Monthly", "📆 Yearly", "🔁 On Reboot", "🔀 Other"];
let hmActiveTiers = new Set(HEATMAP_TIERS);
let hmCollapsed = { overview: false, crons: false };
let cronCurrentView = 'heatmap';
let currentCalDate = new Date();

function toggleCronSchedule(ctx) {
  hmCollapsed[ctx] = !hmCollapsed[ctx];
  document.getElementById(`cron-body-${ctx}`).style.display = hmCollapsed[ctx] ? 'none' : '';
  document.getElementById(`cron-arrow-${ctx}`).classList.toggle('open', !hmCollapsed[ctx]);
}

function toggleHmTier(tier) {
  if (hmActiveTiers.has(tier)) {
    if (hmActiveTiers.size === 1) return;
    hmActiveTiers.delete(tier);
  } else { hmActiveTiers.add(tier); }
  
  document.querySelectorAll('.hm-freq-btn').forEach(btn => {
    btn.classList.toggle('on', hmActiveTiers.has(btn.dataset.tier));
  });
  renderAllCronViews();
}

function setCronView(view) {
  cronCurrentView = view;
  ['overview', 'crons'].forEach(ctx => {
    document.querySelectorAll(`#cron-body-${ctx} .cron-content-pane`).forEach(p => p.classList.remove('active'));
    document.querySelectorAll(`#cron-body-${ctx} .cron-tab-btn`).forEach(b => b.classList.remove('active'));
    
    let target = document.getElementById(`cron-content-${view}-${ctx}`);
    if (target) target.classList.add('active');
    
    let btn = document.querySelector(`#cron-body-${ctx} .cron-tab-btn[data-view="${view}"]`);
    if (btn) btn.classList.add('active');
  });
  renderAllCronViews();
}

function changeCalMonth(delta) {
  if (delta === 0) currentCalDate = new Date();
  else currentCalDate.setMonth(currentCalDate.getMonth() + delta);
  renderAllCronViews();
}

function renderAllCronViews() {
  ['overview', 'crons'].forEach(ctx => {
    renderDynamicLegend(ctx);
    if(cronCurrentView === 'heatmap') rebuildHeatmapGrid(ctx);
    if(cronCurrentView === 'monthly') rebuildCalendarView(ctx);
    if(cronCurrentView === 'agenda') rebuildAgendaView(ctx);
  });
}

function renderDynamicLegend(ctx) {
  const el = document.getElementById(`dynamic-legend-${ctx}`);
  if (!el) return;
  let html = '';
  HEATMAP_TIERS.forEach(t => {
    if (hmActiveTiers.has(t) && D.crons.some(c => c.tier === t)) {
      let colorId = getTierColor(t);
      let hex = getTierHex(colorId);
      html += `<div style="display:flex;align-items:center;gap:5px"><div style="width:8px;height:8px;border-radius:2px;background:${hex}"></div>${e(t)}</div>`;
    }
  });
  el.innerHTML = html;
}

function rebuildHeatmapGrid(ctx) {
  const gridEl = document.getElementById(`hm-grid-${ctx}`);
  if (!gridEl) return;
  const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const grid = Array.from({length:7}, () => Array(24).fill(0));
  const cellJobs = Array.from({length:7}, () => Array.from({length:24}, () => []));

  D.crons.forEach(c => {
    if (!hmActiveTiers.has(c.tier)) return;
    (c.heat_slots || []).forEach(([d,h]) => {
      if (d>=0 && d<7 && h>=0 && h<24) { grid[d][h]++; cellJobs[d][h].push(c.label); }
    });
  });

  const max = Math.max(1, ...grid.flat());
  const levelFor = n => n===0 ? 0 : (n/max>.75?4 : n/max>.5?3 : n/max>.25?2 : 1);

  let html = `<div class="hm-corner"></div>`;
  for(let h=0; h<24; h++) html += `<div class="hm-hour-label">${h % 3 === 0 ? h : ''}</div>`;
  for(let d=0; d<7; d++) {
    html += `<div class="hm-day-label">${DAYS[d]}</div>`;
    for(let h=0; h<24; h++) {
      const n = grid[d][h], level = levelFor(n), jobs = cellJobs[d][h];
      const tip = n===0 ? '' : (n===1 ? jobs[0] : `${n} jobs: ${jobs.slice(0,4).join(', ')}${jobs.length>4?'…':''}`);
      html += `<div class="hm-cell" data-level="${level}" data-tip="${e(tip)} ${n>0?('· '+String(h).padStart(2,'0')+':00'):''}"></div>`;
    }
  }
  gridEl.innerHTML = html;
}

function rebuildCalendarView(ctx) {
  const el = document.getElementById(`cal-grid-${ctx}`);
  if (!el) return;
  const y = currentCalDate.getFullYear(), m = currentCalDate.getMonth();
  const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  document.getElementById(`cal-title-${ctx}`).innerText = `${monthNames[m]} ${y}`;
  
  const firstDay = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const prevMonthDays = new Date(y, m, 0).getDate();
  const totalCells = Math.ceil((firstDay + daysInMonth) / 7) * 7;
  const todayStr = new Date().toDateString();
  
  let html = `<div class="cal-header-day">Sun</div><div class="cal-header-day">Mon</div><div class="cal-header-day">Tue</div><div class="cal-header-day">Wed</div><div class="cal-header-day">Thu</div><div class="cal-header-day">Fri</div><div class="cal-header-day">Sat</div>`;
  
  for (let i = 0; i < totalCells; i++) {
    let dObj, isOther = false, dayNum;
    if (i < firstDay) { dayNum = prevMonthDays - firstDay + i + 1; dObj = new Date(y, m - 1, dayNum); isOther = true; }
    else if (i >= firstDay + daysInMonth) { dayNum = i - firstDay - daysInMonth + 1; dObj = new Date(y, m + 1, dayNum); isOther = true; }
    else { dayNum = i - firstDay + 1; dObj = new Date(y, m, dayNum); }
    
    let dayJobsMap = new Map();
    D.crons.forEach(c => {
      if(hmActiveTiers.has(c.tier) && getDailyRunTimes(c, dObj).length > 0) {
        if (!dayJobsMap.has(c.label)) dayJobsMap.set(c.label, c.tier);
      }
    });
    
    let sortedJobs = Array.from(dayJobsMap.entries()).sort((a,b) => a[0].localeCompare(b[0]));
    
    let jobsHtml = sortedJobs.slice(0, 3).map(([jName, jTier]) => {
      const tColor = getTierColor(jTier);
      return `<div class="cal-job b-${tColor}" title="${e(jName)}">${e(jName)}</div>`;
    }).join('');
    
    if(sortedJobs.length > 3) jobsHtml += `<div class="cal-more">+${sortedJobs.length - 3} more</div>`;
    
    const classes = `cal-cell ${isOther ? 'other-month' : ''} ${dObj.toDateString() === todayStr ? 'today' : ''}`;
    html += `<div class="${classes}"><div class="cal-day-num">${dayNum}</div>${jobsHtml}</div>`;
  }
  el.innerHTML = html;
}

function rebuildAgendaView(ctx) {
  const el = document.getElementById(`agenda-list-${ctx}`);
  if (!el) return;
  const today = new Date(), tomorrow = new Date(); tomorrow.setDate(today.getDate() + 1);
  let todayJobs = [], tomJobs = [];
  
  D.crons.forEach(c => {
    if (!hmActiveTiers.has(c.tier) || !c._parsed) return;
    getDailyRunTimes(c, today).forEach(t => todayJobs.push({ time: t.h*60 + t.m, h: t.h, m: t.m, job: c.label, tier: c.tier }));
    getDailyRunTimes(c, tomorrow).forEach(t => tomJobs.push({ time: t.h*60 + t.m, h: t.h, m: t.m, job: c.label, tier: c.tier }));
  });
  
  const sortFn = (a,b) => a.time - b.time || a.job.localeCompare(b.job);
  todayJobs.sort(sortFn); tomJobs.sort(sortFn);
  
  function renderSec(title, list) {
    if(!list.length) return '';
    const max = 250;
    let html = `<div class="agenda-section"><div class="agenda-header">${title}</div>`;
    list.slice(0, max).forEach(item => {
      const cColor = getTierColor(item.tier);
      html += `<div class="agenda-row">
        <div class="agenda-time">${String(item.h).padStart(2,'0')}:${String(item.m).padStart(2,'0')}</div>
        <div class="agenda-title" title="${e(item.job)}">${e(item.job)}</div>
        <div class="agenda-badge"><span class="bdg b-${cColor}">${e(item.tier)}</span></div>
      </div>`;
    });
    if(list.length > max) html += `<div class="agenda-row" style="justify-content:center;color:var(--ink-3);font-size:11px;border:none;">+ ${list.length - max} more occurrences omitted</div>`;
    return html + `</div>`;
  }
  
  let out = renderSec("TODAY", todayJobs) + renderSec("TOMORROW", tomJobs);
  el.innerHTML = out || `<div style="text-align:center;padding:2rem;color:var(--ink-3)">No scheduled runs for selected filters.</div>`;
}

function renderCronSchedule(context) {
  const totalSlots = D.crons.reduce((acc, c) => acc + (c.heat_slots||[]).length, 0);
  if (totalSlots === 0) return '';
  const tiersInData = new Set(D.crons.map(c => c.tier));
  
  const freqBtns = HEATMAP_TIERS.filter(t => tiersInData.has(t)).map(t => {
    let cColor = getTierColor(t);
    return `<button class="hm-freq-btn on" data-tier="${e(t)}" data-color="${cColor}" onclick="toggleHmTier('${e(t)}')">${e(t)}</button>`;
  }).join('');
  
  const isCollapsed = hmCollapsed[context] || false;

  return `<div class="cron-schedule-card" id="cron-card-${context}">
    <div class="heatmap-header" onclick="toggleCronSchedule('${context}')">
      <div class="heatmap-header-left">
        <span class="heatmap-title-icon">🗓️</span>
        <span class="heatmap-title">Cron Schedule</span>
      </div>
      <span class="heatmap-toggle-arrow ${isCollapsed?'':'open'}" id="cron-arrow-${context}">▾</span>
    </div>
    <div class="heatmap-body" id="cron-body-${context}" style="display:${isCollapsed?'none':''}">
      
      <div class="cron-filters-row">${freqBtns}</div>
      
      <div class="cron-view-controls">
        <div class="cron-view-tabs">
          <button class="cron-tab-btn active" data-view="heatmap" onclick="setCronView('heatmap')">📊 Heat map</button>
          <button class="cron-tab-btn" data-view="monthly" onclick="setCronView('monthly')">📅 Monthly</button>
          <button class="cron-tab-btn" data-view="agenda" onclick="setCronView('agenda')">📋 Agenda</button>
        </div>
        <div id="dynamic-legend-${context}" class="dynamic-legend" style="display:flex; gap:12px; font-size:10.5px; color:var(--ink-3); align-items:center;"></div>
      </div>

      <div id="cron-content-heatmap-${context}" class="cron-content-pane active">
        <div class="heatmap-grid" id="hm-grid-${context}"></div>
        <div class="heatmap-legend" style="margin-top:14px">
          <span>Fewer</span><span class="hm-cell" data-level="0"></span><span class="hm-cell" data-level="1"></span><span class="hm-cell" data-level="2"></span><span class="hm-cell" data-level="3"></span><span class="hm-cell" data-level="4"></span><span>More</span>
        </div>
      </div>

      <div id="cron-content-monthly-${context}" class="cron-content-pane">
        <div class="calendar-header">
          <div class="calendar-title" id="cal-title-${context}"></div>
          <div class="calendar-nav">
            <button onclick="changeCalMonth(-1)">&lt; Prev</button>
            <button onclick="changeCalMonth(0)">Today</button>
            <button onclick="changeCalMonth(1)">Next &gt;</button>
          </div>
        </div>
        <div class="calendar-grid" id="cal-grid-${context}"></div>
      </div>

      <div id="cron-content-agenda-${context}" class="cron-content-pane">
        <div id="agenda-list-${context}"></div>
      </div>

    </div>
  </div>`;
}

function renderOverview() {
  const warns = D.scripts.filter(s => s.warnings.length).length + D.envs.filter(ev => ev.mismatch).length;
  let bannerHtml = warns === 0 ? `<div class="health-banner ok">Everything looks healthy — no issues found</div>` : `<div class="health-banner warn">${warns} thing${warns!==1?'s':''} could use a look — check the Scripts or Variables tab for details</div>`;
  
  let narrativeHtml = `<div class="overview-title" style="margin-top:8px;">What runs on this server?</div><div class="overview-sub">A categorized summary of background tasks, grouped by purpose.</div>`;
  const grouped = {};
  D.scripts.forEach(s => { if(s.desc && s.freq) { grouped[s.semantic_cat] = grouped[s.semantic_cat] || []; grouped[s.semantic_cat].push(s); } });
  
  Object.keys(grouped).sort().forEach(cat => {
    narrativeHtml += `<details class="narrative-card" open><summary><span>${e(cat)}</span><span class="group-count">${grouped[cat].length}</span></summary><div class="narrative-body">`;
    grouped[cat].forEach(s => {
      const warnIcon = s.warnings.length ? `<span style="color:var(--danger);cursor:help;font-weight:600" title="${e(s.warnings.join(', '))}">⚠</span> ` : '';
      narrativeHtml += `<div class="task-row">${warnIcon}<code class="mono-script">${e(s.name)}</code><span class="action-separator">—</span><span>${e(s.desc)}</span><span class="time-badge">${e(s.freq)}</span></div>`;
    });
    narrativeHtml += `</div></details>`;
  });

  document.getElementById('view-overview').innerHTML = `${bannerHtml}${renderCronSchedule('overview')}${narrativeHtml}<div style="text-align:center;margin-top:20px"><button class="fbtn" onclick="switchTab('scripts')" style="padding:9px 18px;font-size:13px">Explore full script inventory →</button></div>`;
}

function renderStats() {
  const warns = D.scripts.filter(s => s.warnings.length).length + D.envs.filter(e => e.mismatch).length;
  document.getElementById('stats').innerHTML = `<div class="stat"><div class="stat-n">${D.scripts.length}</div><div class="stat-l">Scripts</div></div><div class="stat"><div class="stat-n">${D.crons.length}</div><div class="stat-l">Cron jobs</div></div><div class="stat"><div class="stat-n">${D.envs.length}</div><div class="stat-l">Variables</div></div><div class="stat${warns>0?' is-warn':''}"><div class="stat-n">${warns}</div><div class="stat-l">Warnings</div></div>`;
}

function renderScripts() {
  const grouped = {};
  D.scripts.forEach((s, idx) => { s._idx = idx; grouped[s.category] = grouped[s.category] || []; grouped[s.category].push(s); });
  const cats = Object.keys(grouped).sort((a,b) => a==="Core Scripts"?-1:b==="Core Scripts"?1:a.localeCompare(b));

  let html = `<div class="group-container" id="guide-container"><div class="group-head guide-head" id="guide-head" onclick="toggleGroup('guide')"><div>How to document new scripts</div><span class="group-count guide-status" id="guide-status">Expand</span></div><div class="group-body" id="guide-body" style="grid-template-columns:1fr"><div style="color:var(--ink-2);margin-bottom:10px;font-size:13px">To automatically include a new <code>.sh</code> or <code>.py</code> script in this inventory, add this header block right below your shebang:</div><div class="mono-cmd">#!/bin/bash\n# @DESCRIPTION: One-line summary.\n# @FREQUENCY:   How often it runs\n# @CRON:        User, Root\n# @USES_ENV:    VAR1, VAR2</div></div></div>`;

  cats.forEach((cat, c_idx) => {
    const scripts = grouped[cat], gid = `s-cat-${c_idx}`, warnsInCat = scripts.filter(s => s.warnings.length).length;
    html += `<div class="group-container ui-group" data-group="${gid}"><div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')"><div>${e(cat)}</div><div style="display:flex;gap:8px;align-items:center">${warnsInCat>0?bdg('b-red',`${warnsInCat} warning${warnsInCat!==1?'s':''}`):''}<span class="group-count">${scripts.length}</span></div></div><div class="group-body is-open" id="${gid}-body">`;
    scripts.forEach(s => {
      const badges = [];
      if(s.freq) badges.push(bdg('b-green', s.freq));
      if(s.cron_ctx) { if(s.cron_ctx.toLowerCase().includes('root')) badges.push(bdg('b-root', 'Root')); if(s.cron_ctx.toLowerCase().includes('user')) badges.push(bdg('b-user', 'User')); }
      s.env.forEach(ev => badges.push(bdg('b-env', ev)));
      const warnHtml = s.warnings.length ? `<div class="card-desc" style="border-color:var(--danger);background:var(--danger-soft)"><ul class="warning-list">${s.warnings.map(w=>`<li>${e(w)}</li>`).join('')}</ul></div>` : '';
      html += `<div class="card item-card" data-idx="${s._idx}"><div class="card-title"><span>${e(s.name)}</span></div><div class="card-sub">${e(s.path)}</div>${s.desc?`<div class="card-desc">${e(s.desc)}</div>`:''}${warnHtml}<div class="badges">${badges.join('')}</div></div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-scripts').innerHTML = html;
}

function renderCrons() {
  const grouped = { "User cron": [], "Root cron": [] };
  D.crons.forEach((c, idx) => { c._idx = idx; grouped[c.is_root?"Root cron":"User cron"].push(c); });
  
  let html = `<div id="cron-heatmap-wrapper">${renderCronSchedule('crons')}</div>`;
  Object.keys(grouped).forEach((owner, c_idx) => {
    if (!grouped[owner].length) return;
    const gid = `c-cat-${c_idx}`;
    html += `<div class="group-container ui-group" data-group="${gid}"><div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')"><div>${e(owner)}</div><span class="group-count">${grouped[owner].length} jobs</span></div><div class="group-body is-open" id="${gid}-body">`;
    let currentTier = null;
    grouped[owner].forEach((c, i) => {
      if (c.tier !== currentTier) { currentTier = c.tier; html += `<div class="tier-header ui-tier" data-tier="${c.tier}">${e(c.tier)}</div><div class="ui-tier-grid" style="display:grid;gap:9px;margin-bottom:12px">`; }
      html += `<div class="card item-card" data-idx="${c._idx}"><div class="card-title"><span>${e(c.label)}</span><span class="card-sub" style="color:var(--ink-2)">${e(c.human_desc)}</span></div><div class="card-sub">${e(c.raw_schedule)}</div><div class="mono-cmd">${e(c.command)}</div><div class="badges">${c.env.map(ev=>bdg('b-env',ev)).join('')}</div></div>`;
      if (!grouped[owner][i+1] || grouped[owner][i+1].tier !== currentTier) html += `</div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-crons').innerHTML = html;
}

function renderEnvs() {
  const grouped = { "Action required (mismatches)": [], "Healthy variables": [] };
  D.envs.forEach((env, idx) => { env._idx = idx; grouped[env.mismatch?"Action required (mismatches)":"Healthy variables"].push(env); });
  let html = '';
  Object.keys(grouped).forEach((cat, c_idx) => {
    if (!grouped[cat].length) return;
    const gid = `e-cat-${c_idx}`;
    html += `<div class="group-container ui-group" data-group="${gid}"><div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')"><div>${e(cat)}</div><span class="group-count">${grouped[cat].length}</span></div><div class="group-body is-open" id="${gid}-body">`;
    grouped[cat].forEach(env => {
      html += `<div class="card interactive item-card" data-idx="${env._idx}"><div class="card-head" onclick="document.getElementById('eb${env._idx}').classList.toggle('open')"><div style="font-weight:600;font-family:var(--mono);font-size:13px">${e(env.name)}</div><div>${env.mismatch?bdg('b-red','Mismatch'):''}</div></div><div class="card-body" id="eb${env._idx}"><div class="detail"><span class="dlabel">In .env.example</span><span class="dval">${env.env_used.map(s=>bdg('b-blue',s)).join(' ')||'<span style="color:var(--ink-3)">—</span>'}</span></div><div class="detail"><span class="dlabel">In script @USES_ENV</span><span class="dval">${env.script_used.map(s=>bdg('b-green',s)).join(' ')||'<span style="color:var(--ink-3)">—</span>'}</span></div></div></div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-envs').innerHTML = html;
}

function renderDirectoryFilters() {
  const parentContainer = document.getElementById('scripts-parent-dirs');
  const childContainer = document.getElementById('scripts-child-dirs');
  if (!parentContainer) return;
  const allPaths = [...new Set(D.scripts.map(s => s.dir_path))], topLevelsSet = new Set();
  allPaths.forEach(p => topLevelsSet.add(p === "Core Scripts" ? p : p.split('/')[0]));
  const topLevels = [...topLevelsSet].sort();
  
  let parentHtml = `<span class="sub-filter-label">Folders</span><button class="fbtn ${activeParentDir === null ? 'on' : ''}" onclick="selectParentDir(null)">All</button>`;
  topLevels.forEach(p => parentHtml += `<button class="fbtn ${activeParentDir === p ? 'on' : ''}" onclick="selectParentDir('${p}')">${e(p==="Core Scripts"?p:p.replace(/_/g,' ').replace(/-/g,' '))}</button>`);
  parentContainer.innerHTML = parentHtml;

  if (activeParentDir && activeParentDir !== "Core Scripts") {
    const children = allPaths.filter(p => p.startsWith(activeParentDir + '/')).sort();
    if (children.length > 0) {
      childContainer.style.display = 'flex';
      let childHtml = `<span class="sub-filter-label" style="font-size:10px">Subfolders</span><button class="fbtn ${activeChildDir === null ? 'on' : ''}" onclick="selectChildDir(null)">All ${e(activeParentDir.replace(/_/g,' ').replace(/-/g,' '))}</button>`;
      children.forEach(c => childHtml += `<button class="fbtn ${activeChildDir === c ? 'on' : ''}" onclick="selectChildDir('${c}')">${e(c.substring(activeParentDir.length + 1).replace(/_/g,' ').replace(/-/g,' '))}</button>`);
      childContainer.innerHTML = childHtml;
    } else childContainer.style.display = 'none';
  } else childContainer.style.display = 'none';
}

function selectParentDir(dir) { activeParentDir = dir; activeChildDir = null; renderDirectoryFilters(); applyFilter(); }
function selectChildDir(dir) { activeChildDir = dir; renderDirectoryFilters(); applyFilter(); }

function switchTab(t, btn) {
  activeTab = t;
  document.querySelectorAll('.tab, .view').forEach(el => el.classList.remove('active'));
  if(btn) btn.classList.add('active'); else document.querySelector(`.tab[onclick*="${t}"]`).classList.add('active');
  document.getElementById(`view-${t}`).classList.add('active');
  document.getElementById('global-filter-bar').style.display = t==='overview'?'none':'flex';
  document.getElementById('sub-filter-bar').style.display = t==='overview'?'none':'flex';
  document.querySelectorAll('.sub-filter-group').forEach(el => el.style.display = 'none');
  if(document.getElementById(`sub-${t}`)) document.getElementById(`sub-${t}`).style.display = 'flex';
  
  if(t!=='scripts') { activeParentDir = null; activeChildDir = null; } else renderDirectoryFilters();
  if(t!=='crons') { F.docker = F.quick = false; document.getElementById('f-docker').classList.remove('on'); document.getElementById('f-quick').classList.remove('on'); }
  if(t!=='envs') { F.secrets = F.orphans = false; document.getElementById('f-secrets').classList.remove('on'); document.getElementById('f-orphans').classList.remove('on'); }
  applyFilter();
  renderAllCronViews();
}

function tog(k) { F[k] = !F[k]; document.getElementById('f-'+k).classList.toggle('on', F[k]); applyFilter(); }

function applyFilter() {
  const q = document.getElementById('search').value.toLowerCase(); let count = 0;
  if (activeTab === 'scripts') {
    document.querySelectorAll('#view-scripts .item-card').forEach(el => {
      const s = D.scripts[el.dataset.idx], txt = JSON.stringify(s).toLowerCase();
      const dirMatch = activeParentDir ? (activeChildDir ? s.dir_path===activeChildDir : s.dir_path===activeParentDir||s.dir_path.startsWith(activeParentDir+'/')) : true;
      const match = (!q || txt.includes(q)) && (!F.warn || s.warnings.length) && (!F.root || s.cron_ctx?.toLowerCase().includes('root')) && (!F.user || s.cron_ctx?.toLowerCase().includes('user')) && dirMatch;
      el.style.display = match ? '' : 'none'; if(match) count++;
    });
  } else if (activeTab === 'crons') {
    document.querySelectorAll('#view-crons .item-card').forEach(el => {
      const c = D.crons[el.dataset.idx], txt = JSON.stringify(c).toLowerCase();
      const match = (!q || txt.includes(q)) && !F.warn && (!F.root || c.is_root) && (!F.user || !c.is_root) && (!F.docker || /\bdocker\s+(exec|run|compose)\b/i.test(c.command)) && (!F.quick || c.tier_order <= 1);
      el.style.display = match ? '' : 'none'; if(match) count++;
    });
    document.querySelectorAll('#view-crons .ui-tier-grid').forEach(g => {
      const hasVis = Array.from(g.querySelectorAll('.item-card')).some(c => c.style.display !== 'none');
      g.style.display = hasVis ? 'grid' : 'none'; g.previousElementSibling.style.display = hasVis ? 'block' : 'none';
    });
    const hw = document.getElementById('cron-heatmap-wrapper');
    if (hw) hw.style.display = (q || F.root || F.user || F.docker || F.quick) ? 'none' : '';
  } else if (activeTab === 'envs') {
    document.querySelectorAll('#view-envs .item-card').forEach(el => {
      const env = D.envs[el.dataset.idx], txt = JSON.stringify(env).toLowerCase();
      const match = (!q || txt.includes(q)) && (!F.warn || env.mismatch) && (!F.root && !F.user) && (!F.secrets || /token|key|pass|secret|auth|id/i.test(env.name)) && (!F.orphans || !env.script_used.length);
      el.style.display = match ? '' : 'none'; if(match) count++;
    });
  }
  document.querySelectorAll(`#view-${activeTab} .ui-group`).forEach(g => g.style.display = Array.from(g.querySelectorAll('.item-card')).some(c => c.style.display !== 'none') ? 'block' : 'none');
  const guide = document.getElementById('guide-container'); if(guide) guide.style.display = (activeTab==='scripts' && !(q||F.warn||F.root||F.user||activeParentDir)) ? 'block' : 'none';
  document.getElementById('no-match').style.display = (activeTab!=='overview' && !count) ? 'block' : 'none';
}

renderStats(); renderOverview(); renderScripts(); renderCrons(); renderEnvs();
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
    data = {"scripts": scripts, "crons": crons, "envs": envs}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_out = HTML.replace("__DATE__", now).replace("__DATA__", json.dumps(data, ensure_ascii=False, sort_keys=True))
    out_path.write_text(html_out, encoding="utf-8")
    print(f"\n✅ Dashboard generated: {out_path.resolve()}")

if __name__ == "__main__":
    main()
