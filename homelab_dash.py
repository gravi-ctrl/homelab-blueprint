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
                            "env": env_vars
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
  --bg:#fff;--bg2:#f7f6f3;--bg3:#efede8;--bg4:#e4e2dc;
  --tx:#181816;--tx2:#5c5c58;--tx3:#9a9a96;
  --br:rgba(0,0,0,.09);--br2:rgba(0,0,0,.18);
  --r:8px;--r2:12px;
  --blue-bg:#e6f1fb;--blue-tx:#0c447c;--blue-bd:#aacbee;
  --green-bg:#e1f5ee;--green-tx:#085041;--green-bd:#7dcdb5;
  --amber-bg:#faeeda;--amber-tx:#633806;--amber-bd:#e8b96a;
  --purple-bg:#eeedfe;--purple-tx:#3c3489;--purple-bd:#b3a9ec;
  --gray-bg:#f1efe8;--gray-tx:#444441;
  --red-bg:#fcebeb;--red-tx:#a32d2d;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#1c1c1a;--bg2:#232320;--bg3:#2a2a27;--bg4:#323230;
  --tx:#f0efeb;--tx2:#a0a09c;--tx3:#66665f;
  --br:rgba(255,255,255,.09);--br2:rgba(255,255,255,.18);
  --blue-bg:#0c447c;--blue-tx:#b5d4f4;--blue-bd:#2060a0;
  --green-bg:#085041;--green-tx:#9fe1cb;--green-bd:#0f6e56;
  --amber-bg:#4a2800;--amber-tx:#fac775;--amber-bd:#854f0b;
  --purple-bg:#2e2870;--purple-tx:#cecbf6;--purple-bd:#534ab7;
  --gray-bg:#3a3a38;--gray-tx:#d3d1c7;
  --red-bg:#501313;--red-tx:#f7c1c1;
}}
body{font-family:system-ui,-apple-system,sans-serif;font-size:14px;color:var(--tx);background:var(--bg3);line-height:1.5}
.page{max-width:960px;margin:0 auto;padding:2rem 1rem 5rem}
.hdr{display:flex;align-items:baseline;gap:.75rem;margin-bottom:.2rem;flex-wrap:wrap}
h1{font-size:20px;font-weight:500}
.gen-time{font-size:12px;color:var(--tx3);margin-bottom:1.5rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:1.25rem}
.stat{background:var(--bg);border:.5px solid var(--br);border-radius:var(--r);padding:.8rem 1rem}
.stat-n{font-size:26px;font-weight:500;line-height:1.1}
.stat-l{font-size:12px;color:var(--tx2);margin-top:2px}
.nav-tabs{display:flex;gap:10px;margin-bottom:1rem;border-bottom:.5px solid var(--br);padding-bottom:10px;overflow-x:auto}
.tab{background:none;border:none;color:var(--tx2);font-size:15px;font-weight:500;cursor:pointer;padding:5px 10px;border-radius:var(--r);white-space:nowrap}
.tab.active{background:var(--bg);color:var(--tx);box-shadow:0 1px 3px rgba(0,0,0,.1)}

/* Filters Layout */
.filter-bar{display:flex;gap:8px;margin-bottom:1rem;flex-wrap:wrap;align-items:center}
.filter-bar.sub-filters{margin-top:-8px; margin-bottom:1.5rem; padding-left:4px;}
.filter-bar input{flex:1;min-width:160px;padding:7px 11px;font-size:13px;border:.5px solid var(--br2);border-radius:var(--r);background:var(--bg);color:var(--tx);outline:none}
.filter-bar input:focus{border-color:var(--blue-tx)}
.fbtn{padding:5px 13px;font-size:12px;border:.5px solid var(--br2);border-radius:100px;background:var(--bg);color:var(--tx2);cursor:pointer;white-space:nowrap;transition:all .15s}
.fbtn.on{background:var(--blue-bg);color:var(--blue-tx);border-color:var(--blue-bd);font-weight:500}
.sub-filter-group{display:none; gap:6px; flex-wrap:wrap; align-items:center;}
.sub-filter-label{color:var(--tx3); font-size:11px; text-transform:uppercase; letter-spacing:0.04em; margin-right:4px; font-weight:600;}

/* Structuring Groups */
.group-container{margin-bottom:24px; border:.5px solid var(--br); border-radius:var(--r2); background:var(--bg); overflow:hidden;}
.group-head{padding:.8rem 1rem; display:flex; justify-content:space-between; align-items:center; background:var(--bg2); cursor:pointer; user-select:none; font-weight:600; font-size:15px; border-bottom:.5px solid transparent;}
.group-head:hover{background:var(--bg4);}
.group-head.is-open{border-bottom-color:var(--br);}
.group-body{padding:1rem; background:var(--bg); display:none;}
.group-body.is-open{display:grid;}
.group-count{font-size:12px; font-weight:400; color:var(--tx2); background:var(--br); padding:2px 8px; border-radius:100px;}
.tier-header{font-size:14px; font-weight:600; margin:14px 0 6px; color:var(--tx2); padding-bottom:4px; text-transform:uppercase; letter-spacing:0.03em;}
.tier-header:first-child{margin-top:0;}

/* Collapsible Narrative Cards for Overview */
.narrative-card { background: var(--bg); border: 1px solid var(--br); border-radius: var(--r2); margin-bottom: 12px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }
.narrative-card summary { padding: 14px 18px; font-size: 15px; font-weight: 600; cursor: pointer; display: flex; align-items: center; justify-content: space-between; user-select: none; background: var(--bg2); }
.narrative-card summary::-webkit-details-marker { display: none; }
.narrative-card summary::after { content: '▼'; font-size: 11px; color: var(--tx3); transition: transform 0.2s; }
.narrative-card[open] summary::after { transform: rotate(180deg); }
.narrative-card[open] summary { border-bottom: 1px solid var(--br); }
.narrative-body { padding: 16px 18px; }
.task-row { position: relative; padding-left: 18px; margin-bottom: 16px; font-size: 14px; color: var(--tx); line-height: 1.5; }
.task-row:last-child { margin-bottom: 0; }
.task-row::before { content: "•"; position: absolute; left: 0; top: 0; color: var(--tx3); font-size: 18px; }
.time-badge { font-weight: 600; color: var(--blue-tx); background: var(--blue-bg); padding: 3px 8px; border-radius: 6px; font-size: 12px; display: inline-block; margin-right: 6px; letter-spacing: 0.02em; white-space: nowrap; }
.action-text { color: var(--tx); }
.action-separator { color: var(--tx3); font-weight: 400; margin: 0 4px; }

/* Cards */
.card{background:var(--bg);border:.5px solid var(--br);border-radius:var(--r);padding:1rem;display:flex;flex-direction:column;gap:8px;box-shadow:0 1px 2px rgba(0,0,0,0.02)}
.card.interactive{padding:0;cursor:pointer}
.card.interactive .card-head{padding:.8rem 1rem;display:flex;justify-content:space-between;align-items:center}
.card.interactive .card-head:hover{background:var(--bg2);border-radius:var(--r)}
.card-body{padding:.8rem 1rem;border-top:.5px solid var(--br);display:none;background:var(--bg2);border-radius:0 0 var(--r) var(--r)}
.card-body.open{display:block}
.card-title{font-weight:600;font-size:15px;display:flex;justify-content:space-between;align-items:flex-start;gap:10px;word-break:break-all}
.card-sub{font-family:ui-monospace,monospace;font-size:11px;color:var(--tx3)}
.card-desc{font-size:13px;color:var(--tx2);background:var(--bg2);padding:8px 10px;border-radius:var(--r);border:.5px solid var(--br)}
.badges{display:flex;gap:5px;flex-wrap:wrap;margin-top:auto;padding-top:5px}
.bdg{font-size:11px;padding:2px 8px;border-radius:100px;display:inline-flex;align-items:center;line-height:1.5;white-space:nowrap}
.b-blue{background:var(--blue-bg);color:var(--blue-tx)}
.b-green{background:var(--green-bg);color:var(--green-tx)}
.b-amber{background:var(--amber-bg);color:var(--amber-tx)}
.b-red{background:var(--red-bg);color:var(--red-tx)}
.b-gray{background:var(--gray-bg);color:var(--gray-tx)}
.b-purple{background:var(--purple-bg);color:var(--purple-tx)}
.mono-cmd{font-family:ui-monospace,monospace;font-size:12px;background:var(--bg4);padding:4px 6px;border-radius:4px;color:var(--tx);overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.warning-list{margin:0;padding-left:15px;color:var(--red-tx);font-size:11.5px;margin-top:2px}
.detail{display:flex;gap:.6rem;font-size:13px;align-items:flex-start;margin-bottom:6px}
.dlabel{color:var(--tx3);min-width:140px;flex-shrink:0;font-size:11px;text-transform:uppercase;letter-spacing:.04em;line-height:1.8}
.dval{color:var(--tx);display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.view{display:none}
.view.active{display:block}
.no-match{color:var(--tx2);font-size:13px;padding:2rem 0;text-align:center;display:none}
</style>
</head>
<body>
<div class="page">
  <div class="hdr">
    <h1>🖥️ Homelab Automation Dashboard</h1>
  </div>
  <div class="gen-time">Generated __DATE__</div>
  <div class="stats" id="stats"></div>
  
  <div class="nav-tabs">
    <button class="tab active" onclick="switchTab('overview', this)">👋 Overview</button>
    <button class="tab" onclick="switchTab('scripts', this)">📂 Scripts Inventory</button>
    <button class="tab" onclick="switchTab('crons', this)">📅 Cron Schedule</button>
    <button class="tab" onclick="switchTab('envs', this)">🔑 Variables</button>
  </div>

  <!-- Global Filters (Top Row) -->
  <div class="filter-bar" id="global-filter-bar">
    <input type="text" id="search" placeholder="Search names, paths, variables..." oninput="applyFilter()">
    <button class="fbtn" id="f-warn" onclick="tog('warn')">⚠️ Warnings</button>
    <button class="fbtn" id="f-user" onclick="tog('user')">👤 User</button>
    <button class="fbtn" id="f-root" onclick="tog('root')">⚡ Root</button>
  </div>

  <!-- Contextual Sub-Filters (Dynamic Bottom Row) -->
  <div class="filter-bar sub-filters" id="sub-filter-bar">
    
    <!-- Scripts Dynamic Directory Tree Filters -->
    <div id="sub-scripts" class="sub-filter-group" style="display: flex; flex-direction: column; gap: 8px; width: 100%;">
      <div id="scripts-parent-dirs" style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center;"></div>
      <div id="scripts-child-dirs" style="display: none; gap: 6px; flex-wrap: wrap; align-items: center; padding-left: 14px; border-left: 2.5px solid var(--br);"></div>
    </div>
    
    <!-- Crons Sub-Filters -->
    <div id="sub-crons" class="sub-filter-group">
      <span class="sub-filter-label">Runtime:</span>
      <button class="fbtn" id="f-docker" onclick="tog('docker')">🐳 Container Tasks</button>
      <button class="fbtn" id="f-quick" onclick="tog('quick')">⏱️ Frequent Jobs</button>
    </div>
    
    <!-- Variables Sub-Filters -->
    <div id="sub-envs" class="sub-filter-group">
      <span class="sub-filter-label">Audit:</span>
      <button class="fbtn" id="f-secrets" onclick="tog('secrets')">🔒 Secrets/Tokens</button>
      <button class="fbtn" id="f-orphans" onclick="tog('orphans')">🍃 Unused (.env only)</button>
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

// Folder Tree Selection State
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
        if (status) {
            status.textContent = head.classList.contains('is-open') ? 'Collapse' : 'Expand';
        }
    }
}

function renderOverview() {
  const scriptWarnCount = D.scripts.filter(s => s.warnings.length > 0).length;
  const envMismatchCount = D.envs.filter(ev => ev.mismatch).length;
  const totalIssues = scriptWarnCount + envMismatchCount;

  // ── Health Banner ──────────────────────────────────────────────
  let bannerHtml;
  if (totalIssues === 0) {
    bannerHtml = `
      <div style="display:flex;align-items:center;gap:10px;padding:14px 16px;border-radius:var(--r2);background:var(--green-bg);margin-bottom:24px;">
        <span style="font-size:18px;">✅</span>
        <span style="font-size:15px;font-weight:500;color:var(--green-tx);">Everything looks healthy — no issues found</span>
      </div>`;
  } else {
    bannerHtml = `
      <div style="display:flex;align-items:center;gap:10px;padding:14px 16px;border-radius:var(--r2);background:var(--amber-bg);margin-bottom:24px;">
        <span style="font-size:18px;">⚠️</span>
        <span style="font-size:15px;font-weight:500;color:var(--amber-tx);">${totalIssues} things could use a look — check the Full Script Inventory or Warnings filters</span>
      </div>`;
  }

  // ── Group Scripts by Automated Semantic Category ────────────────
  const grouped = {};
  D.scripts.forEach(s => {
    if (!s.desc || !s.freq) return; // Skip completely undocumented internal scripts
    if (!grouped[s.semantic_cat]) grouped[s.semantic_cat] = [];
    grouped[s.semantic_cat].push(s);
  });

  // ── Dynamic Narrative Timeline Cards ─────────────────────────────
  let narrativeHtml = `
    <h2 style="font-size: 20px; font-weight: 600; margin-bottom: 8px;">What runs on this server?</h2>
    <div style="font-size: 14px; color: var(--tx2); margin-bottom: 20px;">A human-readable summary of background tasks automatically categorized by their purpose.</div>
  `;

  // Iterating dynamically means ONLY populated categories render. Vague/empty ones are skipped.
  Object.keys(grouped).sort().forEach(category => {
    const scripts = grouped[category];
    
    narrativeHtml += `
      <details class="narrative-card" open>
        <summary>
          <div>${category}</div>
          <span style="font-size: 12px; font-weight: 400; color: var(--tx3); margin-left: 10px; background: var(--bg); padding: 2px 8px; border-radius: 10px; border: 1px solid var(--br);">${scripts.length} tasks</span>
        </summary>
        <div class="narrative-body">
    `;

    scripts.forEach(s => {
      let warnIcon = s.warnings.length ? `<span style="font-size:12px; cursor:help;" title="${e(s.warnings.join(', '))}">⚠️</span>` : '';
      narrativeHtml += `
          <div class="task-row">
            <span class="time-badge">${e(s.freq)}</span> ${warnIcon}
            <span class="action-separator">──</span> 
            <span class="action-text">${e(s.desc)}</span>
          </div>
      `;
    });

    narrativeHtml += `
        </div>
      </details>
    `;
  });

  document.getElementById('view-overview').innerHTML = `
    ${bannerHtml}
    ${narrativeHtml}
    <div style="text-align:center;margin-top:24px;">
      <button class="fbtn" onclick="switchTab('scripts')" style="padding:10px 20px;font-size:14px;">📂 Explore Full Script Inventory →</button>
    </div>
  `;
}

function renderStats() {
  let warns = D.scripts.filter(s => s.warnings.length > 0).length + D.envs.filter(e => e.mismatch).length;
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="stat-n">${D.scripts.length}</div><div class="stat-l">Total Scripts</div></div>
    <div class="stat"><div class="stat-n">${D.crons.length}</div><div class="stat-l">Active Cron Jobs</div></div>
    <div class="stat"><div class="stat-n">${D.envs.length}</div><div class="stat-l">Tracked Variables</div></div>
    <div class="stat"><div class="stat-n" style="color:var(--red-tx)">${warns}</div><div class="stat-l">Total Warnings</div></div>
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
      if(a === "Core Scripts") return -1;
      if(b === "Core Scripts") return 1;
      return a.localeCompare(b);
  });

  let html = `
  <div class="group-container" id="guide-container" style="border-color: var(--blue-bd);">
    <div class="group-head" id="guide-head" onclick="toggleGroup('guide')" style="background: var(--blue-bg); color: var(--blue-tx);">
      <div>📝 How to Document New Scripts</div>
      <span class="group-count" id="guide-status" style="background: var(--blue-bd); color: var(--blue-tx); font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;">Expand</span>
    </div>
    <div class="group-body" id="guide-body" style="background: var(--bg); grid-template-columns: 1fr;">
      <div style="color:var(--tx2); margin-bottom:10px;">To automatically include a new <code>.sh</code> or <code>.py</code> script in this inventory, add this header block right below your shebang:</div>
      <div class="mono-cmd" style="padding:10px; font-size:13px; line-height:1.4; border: .5px solid var(--br);">#!/bin/bash
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
    let warnsInCat = scripts.filter(s => s.warnings.length > 0).length;
    let warnBadge = warnsInCat > 0 ? bdg('b-red', `⚠️ ${warnsInCat}`) : '';

    html += `<div class="group-container ui-group" data-group="${gid}">
      <div class="group-head is-open" id="${gid}-head" onclick="toggleGroup('${gid}')">
        <div>📁 ${e(cat)}</div>
        <div style="display:flex;gap:10px;align-items:center;">${warnBadge}<span class="group-count">${scripts.length}</span></div>
      </div>
      <div class="group-body grid is-open" id="${gid}-body">`;

    scripts.forEach(s => {
      let badges = [];
      if(s.freq) badges.push(bdg('b-green', s.freq));
      if(s.cron_ctx) {
          if(s.cron_ctx.toLowerCase().includes('root')) badges.push(bdg('b-red', '⚡ Root'));
          if(s.cron_ctx.toLowerCase().includes('user')) badges.push(bdg('b-blue', '👤 User'));
      }
      s.env.forEach(ev => badges.push(bdg('b-amber', ev)));
      
      let warnHtml = s.warnings.length ? `<div class="card-desc" style="background:var(--red-bg);border-color:var(--red-tx)"><ul class="warning-list">${s.warnings.map(w=>`<li>${e(w)}</li>`).join('')}</ul></div>` : '';

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
  const grouped = { "👤 User Cron": [], "⚡ Root Cron": [] };
  D.crons.forEach((c, idx) => {
    c._idx = idx;
    const key = c.is_root ? "⚡ Root Cron" : "👤 User Cron";
    grouped[key].push(c);
  });

  let html = '';
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
    grouped[owner].forEach(c => {
      if (c.tier !== currentTier) {
        currentTier = c.tier;
        html += `<div class="tier-header ui-tier" data-tier="${c.tier}">${e(c.tier)}</div><div class="grid ui-tier-grid" style="margin-bottom:12px;">`;
      }
      
      let badges = [];
      c.env.forEach(ev => badges.push(bdg('b-amber', ev)));
      
      html += `<div class="card item-card" data-idx="${c._idx}">
        <div class="card-title"><span>${e(c.label)}</span><span class="card-sub" style="color:var(--tx2)">${e(c.human_desc)}</span></div>
        <div class="card-sub">${e(c.raw_schedule)}</div>
        <div class="mono-cmd">${e(c.command)}</div>
        <div class="badges">${badges.join('')}</div>
      </div>`;
      
      if(grouped[owner].indexOf(c) === grouped[owner].length - 1 || grouped[owner][grouped[owner].indexOf(c)+1].tier !== currentTier){
          html += `</div>`;
      }
    });
    html += `</div></div>`;
  });
  document.getElementById('view-crons').innerHTML = html;
}

function renderEnvs() {
  const grouped = { "⚠️ Action Required (Mismatches)": [], "✅ Healthy Variables": [] };
  D.envs.forEach((env, idx) => {
    env._idx = idx;
    if(env.mismatch) grouped["⚠️ Action Required (Mismatches)"].push(env);
    else grouped["✅ Healthy Variables"].push(env);
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
      <div class="group-body grid is-open" id="${gid}-body">`;

    grouped[cat].forEach(env => {
      let headerBadges = env.mismatch ? bdg('b-red', '⚠️ Mismatch') : '';
      let b1 = env.env_used.map(s => bdg('b-blue', s)).join(' ');
      let b2 = env.script_used.map(s => bdg('b-green', s)).join(' ');
      
      html += `<div class="card interactive item-card" data-idx="${env._idx}">
        <div class="card-head" onclick="document.getElementById('eb${env._idx}').classList.toggle('open')">
          <div style="font-weight:600;font-family:ui-monospace,monospace">${e(env.name)}</div>
          <div>${headerBadges}</div>
        </div>
        <div class="card-body" id="eb${env._idx}">
          <div class="detail"><span class="dlabel">Declared in .env.example</span><span class="dval">${b1 || '<span style="color:var(--tx3)">—</span>'}</span></div>
          <div class="detail"><span class="dlabel">Found in script @USES_ENV</span><span class="dval">${b2 || '<span style="color:var(--tx3)">—</span>'}</span></div>
        </div>
      </div>`;
    });
    html += `</div></div>`;
  });
  document.getElementById('view-envs').innerHTML = html;
}

// 📁 Dynamic Directory Filtering Logic
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

  // 1. Render Parents Row
  let parentHtml = '<span class="sub-filter-label">Folders:</span>';
  parentHtml += `<button class="fbtn ${activeParentDir === null ? 'on' : ''}" onclick="selectParentDir(null)">📁 All</button>`;
  
  topLevels.forEach(p => {
      const label = p === "Core Scripts" ? p : p.replace(/_/g, ' ').replace(/-/g, ' ').toUpperCase();
      const activeClass = activeParentDir === p ? 'on' : '';
      parentHtml += `<button class="fbtn ${activeClass}" onclick="selectParentDir('${p}')">📁 ${e(label)}</button>`;
  });
  parentContainer.innerHTML = parentHtml;

  // 2. Render Children Row (Nested structure support)
  if (activeParentDir && activeParentDir !== "Core Scripts") {
      const children = allPaths.filter(p => p.startsWith(activeParentDir + '/')).sort();
      if (children.length > 0) {
          childContainer.style.display = 'flex';
          let childHtml = '<span class="sub-filter-label" style="font-size:10px;">Subfolders:</span>';
          childHtml += `<button class="fbtn ${activeChildDir === null ? 'on' : ''}" onclick="selectChildDir(null)">↳ All ${e(activeParentDir.replace(/_/g, ' ').replace(/-/g, ' ').toUpperCase())}</button>`;
          
          children.forEach(c => {
              const subName = c.substring(activeParentDir.length + 1);
              const label = subName.replace(/_/g, ' ').replace(/-/g, ' ').toUpperCase();
              const activeClass = activeChildDir === c ? 'on' : '';
              childHtml += `<button class="fbtn ${activeClass}" onclick="selectChildDir('${c}')">↳ ${e(label)}</button>`;
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
  activeChildDir = null; // Clear active child context
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

  // Overview is a calm landing page — no filter bar at all
  const globalBar = document.getElementById('global-filter-bar');
  const subBar = document.getElementById('sub-filter-bar');
  if (t === 'overview') {
      globalBar.style.display = 'none';
      subBar.style.display = 'none';
  } else {
      globalBar.style.display = 'flex';
      subBar.style.display = 'flex';
  }

  // Toggle sub-filters row
  document.querySelectorAll('.sub-filter-group').forEach(el => el.style.display = 'none');
  const activeSub = document.getElementById(`sub-${t}`);
  if(activeSub) activeSub.style.display = 'flex';
  
  // Clean states to avoid overlapping view calculations
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
      
      // Dynamic Folder/Tree evaluations
      let dirMatch = true;
      if (activeParentDir) {
          if (activeParentDir === "Core Scripts") {
              dirMatch = (s.dir_path === "Core Scripts");
          } else {
              if (activeChildDir) {
                  dirMatch = (s.dir_path === activeChildDir);
              } else {
                  dirMatch = (s.dir_path === activeParentDir || s.dir_path.startsWith(activeParentDir + '/'));
              }
          }
      }

      const match = (!q || txt.includes(q)) &&
                    (!F.warn || s.warnings.length > 0) &&
                    (!F.root || isRoot) &&
                    (!F.user || isUser) &&
                    dirMatch;
      el.style.display = match ? '' : 'none';
      if(match) count++;
    });
  } 
  else if (activeTab === 'crons') {
    document.querySelectorAll('#view-crons .item-card').forEach(el => {
      const c = D.crons[el.dataset.idx];
      const txt = JSON.stringify(c).toLowerCase();
      
      // Strict regex matching to ensure docker is actually executing a container task
      const hasDocker = /\bdocker\s+(exec|run|compose)\b/i.test(c.command);
      const isQuick = c.tier_order <= 1; 
      
      const match = (!q || txt.includes(q)) &&
                    (!F.warn) && 
                    (!F.root || c.is_root) &&
                    (!F.user || !c.is_root) &&
                    (!F.docker || hasDocker) &&
                    (!F.quick || isQuick);
      el.style.display = match ? '' : 'none';
      if(match) count++;
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
      if(match) count++;
    });
  }

  // Globally hide empty groups
  document.querySelectorAll(`#view-${activeTab} .ui-group`).forEach(group => {
    const hasVisible = Array.from(group.querySelectorAll('.item-card')).some(c => c.style.display !== 'none');
    group.style.display = hasVisible ? 'block' : 'none';
  });

  // Dynamically hide the How-to Guide if any filters or search are active
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
</html>"""

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