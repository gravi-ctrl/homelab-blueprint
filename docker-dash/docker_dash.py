#!/usr/bin/env python3

# @DESCRIPTION: Reads your stacks from GitHub, pulls proxy-host URLs from Nginx Proxy Manager, and generates a self-contained HTML dashboard
# @FREQUENCY: On Demand (triggered by `backup-scripts-git.sh`)

"""
docker_dash.py  —  Docker Stack Dashboard Generator
────────────────────────────────────────────────────

Setup:
    cp .env.example .env
    # edit .env with your values
    pip install pyyaml requests python-dotenv
    python3 docker_dash.py

Service descriptions (optional):
    Add labels to any service in your compose file:
        labels:
          - "dash.description=Audiobook & podcast server"
          - "dash.notes=Hardware transcoding configured"

Options:
    --env PATH      Path to .env file (default: .env)
    --out PATH      Override output file path
    --no-npm        Skip NPM API (useful if NPM is offline)
    --verbose       Print extra debug info
"""

import sys, json, argparse, re, os
from pathlib import Path
from datetime import datetime, timezone

# ── Dependency check ──────────────────────────────────────────────
missing = []
try:    import yaml
except: missing.append("pyyaml")
try:    import requests
except: missing.append("requests")
try:    from dotenv import load_dotenv
except: missing.append("python-dotenv")
if missing:
    print(f"Missing: {', '.join(missing)}\nInstall: pip install {' '.join(missing)}")
    sys.exit(1)

# ── GitHub ────────────────────────────────────────────────────────
def github_tree(user, repo, branch):
    url = f"https://api.github.com/repos/{user}/{repo}/git/trees/{branch}?recursive=1"
    r = requests.get(url, headers={"User-Agent":"docker-dash/2.0"}, timeout=20)
    if r.status_code == 404 and branch == "main":
        print("  Branch 'main' not found, trying 'master'…")
        return github_tree(user, repo, "master")
    r.raise_for_status()
    return r.json()["tree"]

def github_raw(user, repo, branch, path):
    url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
    r = requests.get(url, headers={"User-Agent":"docker-dash/2.0"}, timeout=15)
    r.raise_for_status()
    return r.text

# ── NPM API ───────────────────────────────────────────────────────
class NPMClient:
    def __init__(self, base_url, email, password):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "docker-dash/2.0"
        self.ok = False
        try:
            r = self.session.post(f"{self.base}/api/tokens",
                json={"identity": email, "secret": password}, timeout=10)
            r.raise_for_status()
            self.session.headers["Authorization"] = f"Bearer {r.json()['token']}"
            self.ok = True
        except Exception as e:
            print(f"  ⚠ NPM login failed: {e}")

    def proxy_hosts(self):
        try:
            r = self.session.get(
                f"{self.base}/api/nginx/proxy-hosts?expand=certificate,owner,access_list",
                timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠ Could not fetch proxy hosts: {e}")
            return []

def build_npm_index(hosts):
    idx = {}
    for h in hosts:
        fwd = str(h.get("forward_host","")).lower().strip()
        if fwd:
            idx.setdefault(fwd, []).append(h)
    return idx

def npm_host_url(h):
    scheme = "https" if h.get("ssl_forced") or h.get("certificate_id") else "http"
    domains = h.get("domain_names", [])
    return f"{scheme}://{domains[0]}" if domains else ""

def npm_host_details(h):
    """Return url, forward_host, and forward_port for a proxy host."""
    return {
        "url": npm_host_url(h),
        "forward_host": h.get("forward_host", ""),
        "forward_port": str(h.get("forward_port", "")),
    }

def match_npm(stacks, npm_index, npm_self_url=None):
    NPM_CONTAINER_NAMES = {"npm", "nginx-proxy-manager", "nginxproxymanager"}
    for stack in stacks:
        for svc in stack["services"]:
            # If this service IS the NPM container, inject its own URL directly
            if npm_self_url and svc["container_name"].lower() in NPM_CONTAINER_NAMES:
                svc["npm_urls"] = [npm_self_url]
                svc["npm_details"] = [{"url": npm_self_url, "forward_host": "192.168.1.109", "forward_port": "81"}]
                continue
            details = []
            urls = []
            for key in sorted(list({svc["container_name"].lower(), svc["name"].lower()})):
                for h in npm_index.get(key, []):
                    d = npm_host_details(h)
                    if d["url"] and d["url"] not in urls:
                        urls.append(d["url"])
                        details.append(d)
            svc["npm_urls"] = urls
            svc["npm_details"] = details

# ── .env.example parser ───────────────────────────────────────────
def parse_env_example(text):
    entries, pending = [], []
    SECRET_KEYS = {"password","secret","key","token","pass","auth","api",
                   "credentials","private","salt","encryption","user","id"}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            pending = []
            continue
        if line.startswith("#"):
            pending.append(line.lstrip("#").strip())
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            is_sec = any(s in k.lower() for s in SECRET_KEYS)
            entries.append({"key":k,"default":v,"comment":" ".join(pending),"is_secret":is_sec})
            pending = []
        else:
            pending = []
    return entries

# ── Compose parser ────────────────────────────────────────────────
ROLLING_TAGS = {"latest","stable","nightly","edge","dev","master","main",
                "beta","release","current","lts","develop","latest-full"}

def classify_image(image):
    if not image or ":" not in image:
        return {"tag":"latest","pinned":False}
    tag = image.rsplit(":",1)[1]
    pinned = tag.lower() not in ROLLING_TAGS and bool(re.search(r'\d', tag))
    return {"tag":tag,"pinned":pinned}

def parse_compose(path, text):
    try:
        data = yaml.safe_load(text) or {}
    except Exception as e:
        return {"path":path,"parse_error":str(e),"services":[]}

    raw_svcs = data.get("services") or {}
    raw_nets = data.get("networks") or {}

    net_types = {}
    for name, cfg in raw_nets.items():
        net_types[name] = "internal" if (cfg and isinstance(cfg,dict) and cfg.get("internal")) else "external"

    def net_class_of(nets):
        hp = any(net_types.get(n,"external")=="external" for n in nets)
        hi = any(net_types.get(n,"external")=="internal" for n in nets)
        if hp and hi: return "both"
        if hi:        return "internal-only"
        if hp and nets: return "proxy-only"
        return "none"

    services = []
    for name, cfg in raw_svcs.items():
        cfg = cfg or {}
        network_mode = str(cfg.get("network_mode") or "").strip()
        ports   = [str(p) for p in (cfg.get("ports") or [])]
        nets_r  = cfg.get("networks") or []
        nets    = list(nets_r.keys()) if isinstance(nets_r,dict) else list(nets_r)
        env_r   = cfg.get("environment") or []
        env_d   = {}
        if isinstance(env_r,list):
            for item in env_r:
                if "=" in str(item):
                    k,_,v = str(item).partition("=")
                    env_d[k.strip()] = v.strip()
        elif isinstance(env_r,dict):
            env_d = {k:str(v) for k,v in env_r.items()}

        app_url_hint = None
        for key in ("APP_URL","VIRTUAL_HOST","PUBLIC_URL","BASE_URL","SERVER_NAME"):
            v = env_d.get(key,"")
            if v and not v.startswith("${"):
                app_url_hint = v; break

        vols  = [str(v) for v in (cfg.get("volumes") or [])]
        deps_r = cfg.get("depends_on") or []
        deps  = list(deps_r.keys()) if isinstance(deps_r,dict) else list(deps_r)
        hc    = cfg.get("healthcheck")
        has_hc = bool(hc) and not (isinstance(hc,dict) and hc.get("disable"))
        image = str(cfg.get("image") or "")
        img   = classify_image(image)

        # Labels — parse both list and dict forms
        labels_r = cfg.get("labels") or []
        labels_d = {}
        if isinstance(labels_r, list):
            for item in labels_r:
                if "=" in str(item):
                    k, _, v = str(item).partition("=")
                    labels_d[k.strip()] = v.strip()
        elif isinstance(labels_r, dict):
            labels_d = {k: str(v) for k, v in labels_r.items()}

        dash_description = labels_d.get("dash.description", "").strip()
        dash_notes       = labels_d.get("dash.notes", "").strip()

        # Security flags
        is_privileged = bool(cfg.get("privileged", False))
        cap_add = list(cfg.get("cap_add") or [])
        explicit_container_name = bool(cfg.get("container_name"))

        # network_mode handling
        # host/none/service:xxx modes mean no mapped ports but potentially exposed
        is_host_network = network_mode.lower() == "host"
        is_special_network = bool(network_mode) and network_mode.lower() not in ("bridge",)

        # Collect every ${VAR} reference anywhere in this service's raw config
        raw_svc_text = json.dumps(cfg)
        referenced_vars = set(re.findall(r'\$\{([^}]+)\}', raw_svc_text))
        all_env_keys = sorted(list(set(env_d.keys()) | referenced_vars))

        services.append({
            "name": name,
            "container_name": cfg.get("container_name") or name,
            "explicit_container_name": explicit_container_name,
            "image": image,
            "image_tag": img["tag"],
            "image_pinned": img["pinned"],
            "ports": ports,
            "networks": nets,
            "network_type": net_class_of(nets),
            "network_mode": network_mode,
            "is_host_network": is_host_network,
            "is_special_network": is_special_network,
            "app_url_hint": app_url_hint,
            "npm_urls": [],
            "npm_details": [],
            "volumes": vols,
            "restart": cfg.get("restart","no"),
            "has_healthcheck": has_hc,
            "depends_on": deps,
            "is_privileged": is_privileged,
            "cap_add": cap_add,
            "env_keys": all_env_keys,
            "env_vars": [],
            "description": dash_description,
            "notes": dash_notes,
        })

    return {"path":path,"parse_error":None,"services":services}

# ── HTML ──────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Docker Stacks — __SLUG__</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  /* ── Core palette: ink/paper with a single slate-blue accent ── */
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

  /* status */
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
  }
}

html{background:var(--bg)}
body{
  font-family:var(--sans);
  font-size:14px;
  color:var(--ink);
  background:var(--bg);
  line-height:1.55;
  -webkit-font-smoothing:antialiased;
}

.page{max-width:980px;margin:0 auto;padding:3rem 1.25rem 6rem}

/* ── Header ───────────────────────────────────────────── */
.masthead{margin-bottom:2.25rem;padding-bottom:1.1rem;border-bottom:2px solid var(--accent)}
.masthead-top{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:.4rem}
.brand{display:flex;align-items:baseline;gap:.6rem}
.brand-mark{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:22px;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
.meta-line{font-size:12.5px;color:var(--ink-3);display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.meta-line a{color:var(--ink-2);text-decoration:none;border-bottom:1px solid var(--border-2)}
.meta-line a:hover{color:var(--accent);border-color:var(--accent-bd)}
.meta-sep{color:var(--border-2)}

/* ── Stat strip ───────────────────────────────────────── */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:1px;
  background:var(--border);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden;margin:1.75rem 0 1.5rem}
.stat{background:var(--surface);padding:.95rem 1.1rem}
.stat-n{font-size:24px;font-weight:600;letter-spacing:-.02em;color:var(--ink);font-family:var(--mono)}
.stat-l{font-size:11px;color:var(--ink-3);margin-top:3px;text-transform:uppercase;letter-spacing:.05em}
.stat.is-warn .stat-n{color:var(--warn)}

/* ── Filter bar ───────────────────────────────────────── */
.filter-bar{display:flex;gap:8px;margin-bottom:1.5rem;flex-wrap:wrap;align-items:center}
.filter-bar input{
  flex:1;min-width:200px;padding:9px 13px;font-size:13px;font-family:var(--sans);
  border:1px solid var(--border-2);border-radius:var(--radius-sm);
  background:var(--surface);color:var(--ink);outline:none;transition:border-color .15s;
}
.filter-bar input::placeholder{color:var(--ink-3)}
.filter-bar input:focus{border-color:var(--accent)}
.fbtn{
  padding:7px 14px;font-size:12.5px;border:1px solid var(--border-2);border-radius:100px;
  background:var(--surface);color:var(--ink-2);cursor:pointer;white-space:nowrap;
  transition:background .15s,color .15s,border-color .15s;font-family:var(--sans);
}
.fbtn:hover{border-color:var(--ink-3)}
.fbtn.on{background:var(--accent-soft);color:var(--accent);border-color:var(--accent-bd);font-weight:600}
.fbtn.warn-btn.on{background:var(--danger-soft);color:var(--danger);border-color:var(--danger)}

/* ── Cards ────────────────────────────────────────────── */
.stacks{display:grid;gap:10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;
  transition:border-color .15s}
.card:hover{border-color:var(--border-2)}
.card-head{padding:.85rem 1.15rem;display:flex;align-items:flex-start;gap:12px;cursor:pointer;
  user-select:none;border-bottom:1px solid transparent;flex-wrap:wrap}
.card-head:hover{background:var(--surface-2)}
.card-head.is-open{border-bottom-color:var(--border)}
.stack-icon{color:var(--ink-3);font-size:13px;flex-shrink:0;padding-top:1px}
.stack-name{font-weight:600;font-size:14px;color:var(--ink);letter-spacing:-.005em;
  flex-shrink:0;max-width:100%;padding-top:1px}
.head-badges{display:flex;gap:5px;flex-wrap:wrap;align-items:center;flex:1;min-width:140px}
.chev-wrap{margin-left:auto;flex-shrink:0}
.chev{color:var(--ink-3);transition:transform .18s;flex-shrink:0;font-size:11px}
.is-open .chev{transform:rotate(180deg)}

/* ── Badges: ghost/outline style, reserve fill for warnings ── */
.bdg{font-size:11px;padding:3px 9px;border-radius:100px;display:inline-flex;align-items:center;
  gap:4px;line-height:1.5;white-space:nowrap;flex-shrink:0;font-weight:500;
  border:1px solid transparent;font-family:var(--sans)}
.b-svc   {background:transparent;color:var(--ink-2);border-color:var(--border-2)}
.b-port  {background:transparent;color:var(--ink-2);border-color:var(--border-2);font-family:var(--mono);font-size:10.5px}
.b-proxy {background:var(--info-soft); color:var(--info)}
.b-int   {background:transparent; color:var(--ink-2);border-color:var(--border-2)}
.b-both  {background:var(--info-soft); color:var(--info)}
.b-url   {background:transparent;color:var(--accent);border-color:var(--accent-bd);font-family:var(--mono);font-size:10.5px}
.b-pin   {background:var(--ok-soft);    color:var(--ok)}
.b-roll  {background:var(--warn-soft); color:var(--warn)}
.b-warn  {background:var(--danger-soft);   color:var(--danger);font-weight:600}
.b-host  {background:var(--warn-soft);color:var(--warn)}
.b-priv  {background:var(--danger-soft);   color:var(--danger)}
.b-noname{background:var(--warn-soft); color:var(--warn)}
.r-always{background:var(--ok-soft);    color:var(--ok)}
.r-unless{background:var(--info-soft);  color:var(--info)}
.r-onfail{background:var(--warn-soft);  color:var(--warn)}
.r-no    {background:var(--danger-soft);   color:var(--danger)}

/* ── Card body / service detail ──────────────────────────── */
.card-body{padding:0;max-height:0;overflow:hidden;transition:max-height .25s ease}
.card-body.open{padding:.9rem 1.15rem;max-height:none}
.svc-list{display:grid;gap:9px}
.svc{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;background:var(--bg-sub)}
.svc-head{padding:.6rem .9rem;display:flex;align-items:center;gap:8px;
  background:var(--surface-2);border-bottom:1px solid var(--border);flex-wrap:wrap}
.svc-name{font-weight:600;font-size:13px;flex:1;min-width:120px;color:var(--ink)}
.svc-badges{display:flex;gap:4px;flex-wrap:wrap}
.svc-body{padding:.7rem .9rem;display:grid;gap:.6rem;background:var(--surface)}
.detail{display:flex;gap:.7rem;font-size:12px;align-items:flex-start}
.dlabel{color:var(--ink-3);min-width:80px;flex-shrink:0;padding-top:2px;
  font-size:10px;text-transform:uppercase;letter-spacing:.06em;line-height:1.7;font-weight:600}
.dval{color:var(--ink);display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.dval-col{flex-direction:column;align-items:flex-start}
.mono{font-family:var(--mono);font-size:11px;background:var(--bg-sub);
  padding:2px 6px;border-radius:4px;color:var(--ink-2);word-break:break-all;border:1px solid var(--border)}
.url-link{font-size:12px;color:var(--accent);text-decoration:none;font-weight:500;
  display:inline-flex;align-items:center;gap:4px}
.url-link:hover{text-decoration:underline}
.npm-detail{font-size:11px;color:var(--ink-3);display:inline-flex;align-items:center;
  gap:3px;font-family:var(--mono)}
.env-wrap{margin-top:2px;width:100%;overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius-sm)}
.env-table{width:100%;border-collapse:collapse;font-size:11.5px}
.env-table th{text-align:left;color:var(--ink-3);font-weight:600;font-size:10px;
  text-transform:uppercase;letter-spacing:.05em;background:var(--bg-sub);
  padding:6px 10px;border-bottom:1px solid var(--border)}
.env-table td{padding:6px 10px;vertical-align:top;color:var(--ink-2);font-size:11.5px;border-bottom:1px solid var(--border)}
.env-table tr:last-child td{border-bottom:none}
.env-table td.k{font-family:var(--mono);color:var(--ink);font-size:11px;white-space:nowrap}
.env-table td.v{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.env-secret{font-style:italic;color:var(--ink-3)}
.shared-env{margin-top:14px;padding-top:12px;border-top:1px solid var(--border)}
.shared-env-label{font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);margin-bottom:7px;font-weight:600}
.svc-desc{font-size:12px;color:var(--ink-2);padding:.5rem .9rem .15rem;font-style:italic;border-bottom:1px solid var(--border);background:var(--surface)}
.svc-notes{font-size:11.5px;color:var(--ink-3);background:var(--bg-sub);padding:.45rem .9rem;border-top:1px solid var(--border)}
.err-banner{background:var(--danger-soft);color:var(--danger);border:1px solid var(--danger);
  border-radius:var(--radius-sm);padding:.5rem .9rem;font-size:12px;margin-bottom:9px;font-weight:500}
.no-match{color:var(--ink-2);font-size:13px;padding:3rem 0;text-align:center;display:none}
</style>
</head>
<body>
<div class="page">
  <div class="masthead">
    <div class="masthead-top">
      <div class="brand">
        <span class="brand-mark">Stacks</span>
        <h1>Docker Dashboard</h1>
      </div>
    </div>
    <div class="meta-line">
      <a href="__REPO_URL__" target="_blank">__SLUG__</a>
      <span class="meta-sep">·</span>
      <span>branch <strong>__BRANCH__</strong></span>
      <span class="meta-sep">·</span>
      <span>Generated __DATE__</span>
    </div>
  </div>

  <div class="stats" id="stats"></div>

  <div class="filter-bar">
    <input type="text" id="search" placeholder="Search stacks, images, URLs, env keys…" oninput="applyFilter()">
    <button class="fbtn" id="f-ports"    onclick="tog('ports')">Open ports</button>
    <button class="fbtn" id="f-urls"     onclick="tog('urls')">Has URL</button>
    <button class="fbtn" id="f-internal" onclick="tog('internal')">Internal net</button>
    <button class="fbtn" id="f-rolling"  onclick="tog('rolling')">Rolling tag</button>
    <button class="fbtn warn-btn" id="f-security" onclick="tog('security')">Security</button>
    <button class="fbtn" id="f-host"     onclick="tog('host')">Host network</button>
  </div>

  <div class="stacks" id="stacks"></div>
  <div class="no-match" id="no-match">No stacks match your filter.</div>
</div>
<script>
const D=__DATA__;
function e(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function bdg(cls,txt){return`<span class="bdg ${cls}">${e(txt)}</span>`}
function extLink(url,label){
  return`<a class="url-link" href="${e(url)}" target="_blank">`+
    `<svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 3H3a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2v-4"/><path d="M13 1h2v2M9 7l6-6"/></svg>`+
    `${e(label||url)}</a>`}
function rcls(r){
  if(!r||r==='no')return 'r-no';
  if(r==='always')return 'r-always';
  if(r.includes('unless'))return 'r-unless';
  if(r.includes('on-failure'))return 'r-onfail';
  return 'r-no';}

function envTable(vars){
  if(!vars||!vars.length) return '';
  const trows=vars.map(ev=>`<tr>
    <td class="k">${e(ev.key)}</td>
    <td class="v">${ev.is_secret?'<span class="env-secret">••••••</span>':ev.default?e(ev.default):'<span style="color:var(--ink-3)">—</span>'}</td>
    <td>${e(ev.comment)}</td></tr>`).join('');
  return`<div class="env-wrap"><table class="env-table">
    <thead><tr><th>Variable</th><th>Default</th><th>Description</th></tr></thead>
    <tbody>${trows}</tbody></table></div>`;
}

function svcHasSecurityConcern(sv){
  return sv.is_privileged || (sv.cap_add&&sv.cap_add.length>0) || !sv.explicit_container_name || (sv.restart==='no'||!sv.restart);
}

function renderStats(){
  let svcs=0;const pts=new Set();let urls=0,roll=0,secWarn=0,hostNet=0;
  D.forEach(s=>{svcs+=s.services.length;s.services.forEach(sv=>{
    sv.ports.forEach(p=>pts.add(p));
    if(sv.npm_urls.length||sv.app_url_hint)urls++;
    if(!sv.image_pinned)roll++;
    if(svcHasSecurityConcern(sv))secWarn++;
    if(sv.is_host_network)hostNet++;
  })});
  document.getElementById('stats').innerHTML=[
    ['Stacks',D.length,false],['Services',svcs,false],
    ['Open ports',pts.size,false],['URLs found',urls,false],['Rolling tags',roll,false],
    ['Warnings',secWarn,true]
  ].map(([l,n,isWarn])=>`<div class="stat${isWarn&&n>0?' is-warn':''}"><div class="stat-n">${n}</div><div class="stat-l">${e(l)}</div></div>`).join('');
}

function buildCard(stack,i){
  const allPorts=[],allUrls=[];
  let hp=false,hi=false,hasRoll=false,hasSec=false,hasHost=false;
  stack.services.forEach(sv=>{
    sv.ports.forEach(p=>allPorts.push(p));
    if(sv.npm_urls.length)allUrls.push(...sv.npm_urls);
    else if(sv.app_url_hint)allUrls.push(sv.app_url_hint);
    if(['proxy-only','both'].includes(sv.network_type))hp=true;
    if(['internal-only','both'].includes(sv.network_type))hi=true;
    if(!sv.image_pinned)hasRoll=true;
    if(svcHasSecurityConcern(sv))hasSec=true;
    if(sv.is_host_network)hasHost=true;
  });
  const hb=[
    bdg('b-svc',`${stack.services.length} svc${stack.services.length!==1?'s':''}`),
    hp&&hi?bdg('b-both','proxy+internal'):hp?bdg('b-proxy','proxy'):hi?bdg('b-int','internal'):'',
    hasHost?bdg('b-host','host net'):'',
    ...allPorts.slice(0,3).map(p=>bdg('b-port',p)),
    allPorts.length>3?bdg('b-svc',`+${allPorts.length-3} ports`):'',
    ...allUrls.slice(0,2).map(u=>bdg('b-url',u)),
    allUrls.length>2?bdg('b-url',`+${allUrls.length-2} more`):'',
    hasRoll?bdg('b-roll','rolling'):'',
    hasSec?bdg('b-warn','⚠ warnings'):'',
  ].filter(Boolean).join('');

  const svcsHtml=stack.services.map(sv=>{
    const secFlags=[];
    if(sv.is_privileged) secFlags.push(bdg('b-priv','⚠ privileged'));
    if(sv.cap_add&&sv.cap_add.length) secFlags.push(bdg('b-priv',`⚠ cap_add: ${sv.cap_add.join(', ')}`));
    if(!sv.explicit_container_name) secFlags.push(bdg('b-noname','⚠ no container_name'));
    if(sv.is_host_network) secFlags.push(bdg('b-host','host network'));
    if(sv.is_special_network&&!sv.is_host_network) secFlags.push(bdg('b-host',`network_mode: ${sv.network_mode}`));

    const sb=[
      sv.image_pinned?bdg('b-pin',sv.image_tag):bdg('b-roll',sv.image_tag||'latest'),
      sv.has_healthcheck?bdg('b-pin','✓ health'):'',
      bdg(rcls(sv.restart),sv.restart||'no'),
      ...secFlags,
    ].filter(Boolean).join('');

    const rows=[];
    rows.push(`<div class="detail"><span class="dlabel">Image</span><span class="dval"><span class="mono">${e(sv.image)}</span></span></div>`);

    if(sv.explicit_container_name){
      rows.push(`<div class="detail"><span class="dlabel">Container</span><span class="dval"><span class="mono">${e(sv.container_name)}</span></span></div>`);
    }

    if(sv.npm_details&&sv.npm_details.length){
      const npmRows = sv.npm_details.map(d=>{
        const portPart = d.forward_port ? `<span class="npm-detail"><span class="mono">${e(d.forward_host)}:${e(d.forward_port)}</span></span>` : '';
        return `<div style="display:flex;flex-direction:column;gap:2px">${extLink(d.url)}${portPart}</div>`;
      }).join('');
      rows.push(`<div class="detail"><span class="dlabel">URL</span><span class="dval" style="flex-direction:column;align-items:flex-start">${npmRows}</span></div>`);
    } else if(sv.app_url_hint){
      rows.push(`<div class="detail"><span class="dlabel">URL</span><span class="dval">${extLink(sv.app_url_hint)}</span></div>`);
    }

    if(sv.network_mode){
      rows.push(`<div class="detail"><span class="dlabel">Net mode</span><span class="dval">${bdg(sv.is_host_network?'b-host':'b-int', sv.network_mode)}</span></div>`);
    }

    if(sv.ports.length)rows.push(`<div class="detail"><span class="dlabel">Ports</span><span class="dval">${sv.ports.map(p=>`<span class="mono">${e(p)}</span>`).join('')}</span></div>`);

    if(sv.networks.length)rows.push(`<div class="detail"><span class="dlabel">Networks</span><span class="dval">${sv.networks.map(n=>bdg(n.toLowerCase().includes('internal')?'b-int':'b-proxy',n)).join('')}</span></div>`);

    if(sv.depends_on.length)rows.push(`<div class="detail"><span class="dlabel">Depends</span><span class="dval">${sv.depends_on.map(d=>`<span class="mono">${e(d)}</span>`).join('')}</span></div>`);

    if(sv.volumes.length)rows.push(`<div class="detail"><span class="dlabel">Volumes</span><span class="dval dval-col">${sv.volumes.map(v=>`<span class="mono">${e(v)}</span>`).join('')}</span></div>`);

    if(sv.env_vars&&sv.env_vars.length){
      rows.push(`<div class="detail"><span class="dlabel">Env vars</span>${envTable(sv.env_vars)}</div>`);
    }

    return`<div class="svc"><div class="svc-head"><span class="svc-name">${e(sv.name)}</span><div class="svc-badges">${sb}</div></div>${sv.description?`<div class="svc-desc">${e(sv.description)}</div>`:''}<div class="svc-body">${rows.join('')}</div>${sv.notes?`<div class="svc-notes">📝 ${e(sv.notes)}</div>`:''}</div>`;
  }).join('');

  const orphanEnv = (stack.env_vars&&stack.env_vars.length)
    ? `<div class="shared-env">
         <div class="shared-env-label">Shared / unmatched env vars</div>
         ${envTable(stack.env_vars)}
       </div>`
    : '';

  const label=stack.path.replace(/\/(docker-compose|compose)\.ya?ml$/i,'');
  const err=stack.parse_error?`<div class="err-banner">⚠ ${e(stack.parse_error)}</div>`:'';
  return`<div class="card-head" id="h${i}" onclick="toggle(${i})"><span class="stack-icon">▣</span><span class="stack-name">${e(label)}</span><div class="head-badges">${hb}</div><span class="chev-wrap"><span class="chev">▾</span></span></div><div class="card-body" id="b${i}">${err}<div class="svc-list">${svcsHtml}</div>${orphanEnv}</div>`;
}

function renderCards(){
  const c=document.getElementById('stacks');c.innerHTML='';
  D.forEach((s,i)=>{const el=document.createElement('div');el.className='card';el.dataset.i=i;el.innerHTML=buildCard(s,i);c.appendChild(el);});
}
function toggle(i){const b=document.getElementById('b'+i),h=document.getElementById('h'+i);const o=b.classList.toggle('open');h.classList.toggle('is-open',o);}

const F={ports:false,urls:false,internal:false,rolling:false,security:false,host:false};
function tog(k){F[k]=!F[k];document.getElementById('f-'+k).classList.toggle('on',F[k]);applyFilter();}
function applyFilter(){
  const q=document.getElementById('search').value.toLowerCase();let n=0;
  document.querySelectorAll('#stacks .card').forEach(el=>{
    const s=D[+el.dataset.i];const txt=JSON.stringify(s).toLowerCase();
    const ok=(!q||txt.includes(q))
      &&(!F.ports||s.services.some(sv=>sv.ports.length>0))
      &&(!F.urls||s.services.some(sv=>sv.npm_urls.length||sv.app_url_hint))
      &&(!F.internal||s.services.some(sv=>['internal-only','both'].includes(sv.network_type)))
      &&(!F.rolling||s.services.some(sv=>!sv.image_pinned))
      &&(!F.security||s.services.some(sv=>svcHasSecurityConcern(sv)))
      &&(!F.host||s.services.some(sv=>sv.is_host_network));
    el.style.display=ok?'':'none';if(ok)n++;
  });
  document.getElementById('no-match').style.display=n?'none':'block';
}
renderStats();renderCards();
</script>
</body>
</html>
"""

# ── Main ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env",    default=".env")
    ap.add_argument("--out",    default=None)
    ap.add_argument("--no-npm", action="store_true")
    ap.add_argument("--verbose",action="store_true")
    args = ap.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        print(f"Config not found: {env_path}\nCopy .env.example → .env and fill in your values.")
        sys.exit(1)
    load_dotenv(env_path)

    repo_url  = os.environ.get("GITHUB_REPO","").strip()
    branch    = os.environ.get("GITHUB_BRANCH","main").strip()
    exclude   = [x.strip() for x in os.environ.get("EXCLUDE_DIRS","_archive").split(",") if x.strip()]
    npm_url   = os.environ.get("NPM_URL","").strip()
    npm_email = os.environ.get("NPM_EMAIL","").strip()
    npm_pass  = os.environ.get("NPM_PASSWORD","").strip()
    out_path  = Path(args.out or os.environ.get("OUTPUT_FILE","docker_dashboard.html"))

    if not repo_url:
        print("GITHUB_REPO not set in .env"); sys.exit(1)

    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if not m:
        print(f"Invalid GITHUB_REPO: {repo_url}"); sys.exit(1)
    user, repo = m.group(1), m.group(2)
    slug = f"{user}/{repo}"

    print(f"\n📦 Fetching file tree for {slug}@{branch}…")
    try:
        tree = github_tree(user, repo, branch)
    except Exception as e:
        print(f"  Error: {e}"); sys.exit(1)

    all_paths = [f["path"] for f in tree if f["type"]=="blob"]
    compose_paths = sorted([p for p in all_paths
        if re.search(r'(?:^|/)(docker-compose|compose)\.ya?ml$',p,re.I)
        and not any(p.startswith(ex) for ex in exclude)])
    env_ex_set = {p for p in all_paths
        if p.endswith(".env.example")
        and not any(p.startswith(ex) for ex in exclude)}

    print(f"  ✓ {len(compose_paths)} compose file(s), {len(env_ex_set)} .env.example file(s)")

    print("\n🔍 Parsing stacks…")
    stacks = []
    for cp in compose_paths:
        try:
            text = github_raw(user, repo, branch, cp)
            stack = parse_compose(cp, text)
        except Exception as ex:
            stack = {"path":cp,"parse_error":str(ex),"services":[]}

        stack_dir = str(Path(cp).parent)
        env_ex_path = f"{stack_dir}/.env.example" if stack_dir!="." else ".env.example"
        stack["env_vars"] = []

        if env_ex_path in env_ex_set:
            try:
                env_text = github_raw(user, repo, branch, env_ex_path)
                all_env_vars = parse_env_example(env_text)

                all_svc_keys = {k for sv in stack["services"] for k in sv.get("env_keys", [])}
                for sv in stack["services"]:
                    sv_keys = set(sv.get("env_keys", []))
                    sv["env_vars"] = [ev for ev in all_env_vars if ev["key"] in sv_keys]

                stack["env_vars"] = [ev for ev in all_env_vars if ev["key"] not in all_svc_keys]

            except Exception as ex:
                if args.verbose: print(f"    ⚠ .env.example: {ex}")

        stacks.append(stack)
        svc_env_total = sum(len(sv.get("env_vars",[])) for sv in stack["services"])

        # Security summary for this stack
        host_svcs   = [sv["name"] for sv in stack["services"] if sv.get("is_host_network")]
        priv_svcs   = [sv["name"] for sv in stack["services"] if sv.get("is_privileged")]
        noname_svcs = [sv["name"] for sv in stack["services"] if not sv.get("explicit_container_name")]

        flags = []
        if host_svcs:   flags.append(f"host-net: {', '.join(host_svcs)}")
        if priv_svcs:   flags.append(f"privileged: {', '.join(priv_svcs)}")
        if noname_svcs: flags.append(f"no container_name: {', '.join(noname_svcs)}")

        print(f"  ✓ {cp}  ({len(stack['services'])} services"
              + (f", {svc_env_total} matched + {len(stack['env_vars'])} orphan env vars" if (svc_env_total or stack['env_vars']) else "")
              + (f"  ⚠ {' | '.join(flags)}" if flags else "")
              + ("  ⚠ "+stack["parse_error"] if stack.get("parse_error") else "") + ")")

    npm_count = 0
    if not args.no_npm and npm_url and npm_email and npm_pass:
        print(f"\n🔗 Connecting to NPM at {npm_url}…")
        npm = NPMClient(npm_url, npm_email, npm_pass)
        if npm.ok:
            print("  ✓ Authenticated")
            hosts = npm.proxy_hosts()
            npm_count = len(hosts)
            print(f"  ✓ {npm_count} proxy host(s)")

            npm_index = build_npm_index(hosts)

            npm_self_url = None
            https_fallback = None
            for h in hosts:
                u = npm_host_url(h)
                if not u:
                    continue
                if str(h.get("forward_port", "")) == "81":
                    npm_self_url = u
                    break
                if not https_fallback and u.startswith("https"):
                    https_fallback = u
            if not npm_self_url:
                npm_self_url = https_fallback
            if npm_self_url:
                print(f"  ✓ NPM self-URL detected: {npm_self_url}")

            match_npm(stacks, npm_index, npm_self_url)
            matched = sum(1 for s in stacks for sv in s["services"] if sv["npm_urls"])
            print(f"  ✓ Matched URLs to {matched} service(s)")
    elif not args.no_npm:
        print("\n⚠  NPM credentials not set — skipping URL lookup")

    print("\n✍  Generating HTML…")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = (HTML
        .replace("__REPO_URL__", repo_url)
        .replace("__SLUG__",     slug)
        .replace("__BRANCH__",   branch)
        .replace("__DATE__",     now)
        .replace("__DATA__",     json.dumps(stacks, ensure_ascii=False, sort_keys=True)))
    out_path.write_text(html, encoding="utf-8")

    total_svcs  = sum(len(s["services"]) for s in stacks)
    total_ports = len({p for s in stacks for sv in s["services"] for p in sv["ports"]})
    total_urls  = sum(1 for s in stacks for sv in s["services"] if sv["npm_urls"] or sv["app_url_hint"])
    rolling     = sum(1 for s in stacks for sv in s["services"] if not sv["image_pinned"])
    host_net    = sum(1 for s in stacks for sv in s["services"] if sv["is_host_network"])
    privileged  = sum(1 for s in stacks for sv in s["services"] if sv["is_privileged"])
    no_name     = sum(1 for s in stacks for sv in s["services"] if not sv["explicit_container_name"])

    print(f"""
┌─ Done ────────────────────────────────────────
│  File      : {out_path.resolve()}
│  Stacks    : {len(stacks)}  ·  Services: {total_svcs}
│  Ports     : {total_ports}  ·  URLs: {total_urls}  ·  NPM hosts: {npm_count}
│  Rolling tags   : {rolling} (consider pinning these)
│  Host network   : {host_net}
│  Privileged     : {privileged}
│  No container_name: {no_name}
└───────────────────────────────────────────────
  Open: open {out_path}   (macOS)
        xdg-open {out_path}   (Linux)
""")

if __name__ == "__main__":
    main()
