#!/usr/bin/env python3
# @DESCRIPTION: Terminal AI assistant powered by Google Gemini: piped logs/diffs/scripts, multi-turn chat, commit/PR/changelog writer, container log diagnosis, linter-backed script review, command suggester, read-only investigation agent
# @FREQUENCY: On Demand
# @USES_ENV: GEMINI_API_KEY, GEMINI_MODEL
# @CAPABILITIES:
#   • Streams AI responses; smart log handling (dedupe, line numbers, error windows) instead of blind tail-trimming.
#   • Auto-fails over to other models on 429/404/5xx, keeping analysis uninterrupted on the free tier.
#   • Redacts API keys, tokens, passwords, env-style secrets and certificates from everything it sends.
#   • `commit`, `pr`, `changelog`: writes messages/descriptions from your git history and diffs.
#   • `logs`: gathers docker/systemd context automatically, then diagnoses with evidence + next step.
#   • `review`: runs real linters (shellcheck, hadolint, ansible-lint...) and lets the AI triage the findings.
#   • `run`: runs any command and only asks the AI if it fails. `do`: plain English -> one command, with tiered confirmation.
#   • `agent`: the model can request read-only commands from an allowlist; you approve every one.
#   • ~/.oracle/context.md: permanent background about your setup, added to every request.

import argparse
import difflib
import hashlib
import json
import os
import platform
import pty
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import requests

ENV_PATH = Path("/opt/rabbit-hole/.env")

# ── Load Environment ──────────────────────────────────────────────────────────
def load_dotenv(path):
    if not path.is_file(): return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            val = v.split("#")[0].strip().strip('"\'')
            os.environ.setdefault(k.strip(), val)

load_dotenv(ENV_PATH)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

# Advanced/testing: point at another OpenAI-compatible endpoint.
BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest").strip()
if not DEFAULT_MODEL:
    DEFAULT_MODEL = "gemini-flash-lite-latest"

# --deep uses this. A "-latest" alias keeps working as Google rolls models forward.
# If it is rate-limited / unavailable on your free key, failover drops to the lite cascade below.
DEEP_MODEL = os.environ.get("GEMINI_DEEP_MODEL", "").strip() or "gemini-flash-latest"

# High-concurrency fallback cascade if the primary model hits a 429/503/404
LITE_CASCADE = [
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

# Global switches (set in main)
QUIET = False
USE_CONTEXT = True

# ── Terminal helpers ──────────────────────────────────────────────────────────
def paint(code: str, text: str, stream=None) -> str:
    stream = stream or sys.stderr
    if os.environ.get("NO_COLOR") or not stream.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

def note(msg: str = ""):
    """Status line on stderr (keeps stdout clean for pipes). Silenced by -q."""
    if not QUIET:
        print(msg, file=sys.stderr)

def warn(msg: str):
    print(paint("1;33", msg), file=sys.stderr)

def die(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)

def banner(model: str, extra: str = ""):
    note(paint("2", f"[Model: {model}{(' | ' + extra) if extra else ''}]"))
    note()

# ── Sanitizer (Defense-in-Depth) ──────────────────────────────────────────────
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')

_SENS_STRONG = (r'(?:password|passwd|passphrase|secret|token|api[_\-]?key|apikey|credential|'
                r'jwt|private[_\-]?key|access[_\-]?key|client[_\-]?secret|[_\-]key)')   # [_-]key: ENCRYPTION_KEY, APP_KEY, ssh-key ...
_SENS_WEAK = r'(?<![A-Za-z0-9])(?:auth|salt|pass)(?![A-Za-z0-9])'
# Names that end in _key but are not secrets
_NOT_SECRET_KEYS = re.compile(r'(?i)^(primary|foreign|sort|partition|cache|public|row|map|object|index|lookup|shard|group|order|unique)[_\-]key$')
_ASSIGN_RE = re.compile(
    r'(?i)([A-Za-z0-9_.\-]*' + _SENS_STRONG + r'[A-Za-z0-9_.\-]*|' + _SENS_WEAK + r')'
    r'(["\']?\s*[:=]\s*["\']?)'
    r'([^"\'\s,;&}\]]+)'
)
# Words that follow "token:" / "auth:" etc. in ordinary diagnostics. Keep them: they are the clue, not the secret.
_BENIGN_VALUES = {
    "true", "false", "none", "null", "nil", "yes", "no", "on", "off", "expired", "failed", "invalid",
    "missing", "required", "empty", "denied", "unauthorized", "forbidden", "error", "ok", "success",
    "valid", "revoked", "refreshed", "mismatch", "unset", "disabled", "enabled", "not",
}

def _mask_assignment(m):
    key, sep, val = m.group(1), m.group(2), m.group(3)
    if val.lower() in _BENIGN_VALUES or val.startswith("[REDACTED") or _NOT_SECRET_KEYS.match(key):
        return m.group(0)
    return f"{key}{sep}[REDACTED]"

_SECRET_PATTERNS = [
    # 1. Platform-specific high-entropy prefixes (near-zero false positives)
    (r'AIzaSy[a-zA-Z0-9_\-]{33}', '[REDACTED_GEMINI_KEY]'),
    (r'gsk_[a-zA-Z0-9]{40,}', '[REDACTED_GROQ_KEY]'),
    (r'gh[pousr]_[a-zA-Z0-9]{30,}', '[REDACTED_GITHUB_TOKEN]'),
    (r'github_pat_[a-zA-Z0-9_]{60,}', '[REDACTED_GITHUB_PAT]'),
    (r'glpat-[a-zA-Z0-9_\-]{20,}', '[REDACTED_GITLAB_TOKEN]'),
    (r'(?<![A-Za-z0-9])sk-[a-zA-Z0-9_\-]{20,}', '[REDACTED_API_KEY]'),
    (r'(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])', '[REDACTED_AWS_KEY]'),
    (r'xox[abprs]-[a-zA-Z0-9\-]{10,}', '[REDACTED_SLACK_TOKEN]'),
    (r'eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}', '[REDACTED_JWT]'),

    # 2. Private keys & certificates (also matches PKCS#8 "BEGIN PRIVATE KEY")
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----', '[REDACTED_PRIVATE_KEY]'),
    (r'-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----', '[REDACTED_CERTIFICATE]'),

    # 3. Network & auth headers, credentials embedded in URLs
    (r'(?i)bearer\s+[a-zA-Z0-9_\-\.=]+', 'Bearer [REDACTED]'),
    (r'(?i)authorization:\s*[^\n]+', 'Authorization: [REDACTED]'),
    (r'(?i)\b([a-z][a-z0-9+.\-]*)://([^:/\s@]+):([^@\s/]+)@', r'\1://\2:[REDACTED]@'),

    # 4. CLI flags: --password hunter2
    (r'(?i)(--?(?:password|passwd|token|secret|api-?key)[ ]+)(?!-)(\S+)', r'\1[REDACTED]'),
]

def clean_piped_text(text: str) -> str:
    """Strips terminal color codes and masks secrets (key=value, env-style, prefixed keys, certs)."""
    cleaned = ANSI_RE.sub('', text)
    for pattern, repl in _SECRET_PATTERNS:
        cleaned = re.sub(pattern, repl, cleaned)
    # 5. Assignments: PASSWORD=x, "api_key": "x", POSTGRES_PASSWORD=x, token: x ...
    cleaned = _ASSIGN_RE.sub(_mask_assignment, cleaned)
    return cleaned

# ── Personal context file ─────────────────────────────────────────────────────
CONTEXT_TEMPLATE = """# About my setup
(Everything in this file is sent to Gemini with every request. No passwords, keys or tokens here.)

## Host
- OS / distro / kernel:
- Hardware (CPU, RAM, disks):
- Hostname / role:

## Docker & stacks
- Where stacks live (paths):
- How they are deployed (compose, Portainer, ...):
- Reverse proxy / auth / DNS:
- Backups (what, how, where):

## Tools & conventions
- Shell / editor:
- Git hosting & workflow:
- Languages I am comfortable with:

## Rules for oracle
- Prefer: (e.g. short answers, Bash over Python, compose v2 syntax)
- Never suggest: (e.g. running as root, curl | sh, deleting volumes)
"""
CONTEXT_MAX_CHARS = 8000

def context_path() -> Path:
    custom = os.environ.get("ORACLE_CONTEXT", "").strip()
    return Path(custom).expanduser() if custom else Path.home() / ".oracle" / "context.md"

def load_context() -> str:
    if not USE_CONTEXT:
        return ""
    try:
        text = context_path().read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text or text == CONTEXT_TEMPLATE.strip():
        return ""  # untouched template: don't waste tokens
    text = clean_piped_text(text)
    if len(text) > CONTEXT_MAX_CHARS:
        text = text[:CONTEXT_MAX_CHARS] + "\n[context file truncated]"
    return text

def env_snapshot() -> str:
    """Tiny, non-sensitive description of where commands will run."""
    os_name = platform.system()
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    return (f"ENVIRONMENT: {os_name}, kernel {platform.release()}, shell {os.path.basename(os.environ.get('SHELL', 'sh'))}, "
            f"cwd {os.getcwd()}, date {time.strftime('%Y-%m-%d %H:%M %Z')}.")

TERMINAL_RULES = (
    "TERMINAL FORMATTING RULES:\n"
    "- Output plain text strictly formatted for a Linux CLI terminal.\n"
    "- Do NOT use markdown headers (no '#', '##', or '###'). Write section headings in clean UPPERCASE followed by a colon.\n"
    "- Do NOT use markdown bold/italic asterisks (no '**' or '*'). Write terms cleanly without asterisks.\n"
    "- Use clean unicode bullets ('•') for lists."
)

def system_prompt(role: str, terminal: bool = True, with_env: bool = False) -> str:
    parts = [role]
    if terminal:
        parts.append(TERMINAL_RULES)
    if with_env:
        parts.append(env_snapshot())
    ctx = load_context()
    if ctx:
        parts.append("BACKGROUND ABOUT THE USER'S SETUP (apply it silently when relevant; never repeat it back):\n" + ctx)
    return "\n\n".join(parts)

# ── Model Discovery & Inspection ──────────────────────────────────────────────
def get_available_models(token: str) -> list[str]:
    try:
        root = BASE_URL.rsplit("/openai", 1)[0]
        r = requests.get(f"{root}/models", headers={"x-goog-api-key": token}, timeout=10)
        if r.status_code == 200:
            models = []
            for m in r.json().get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    models.append(m["name"].replace("models/", ""))
            return sorted(models)
    except Exception as e:
        print(f"Error fetching models: {e}", file=sys.stderr)
    return []

# ── API layer with auto-failover ──────────────────────────────────────────────
# 429 = free-tier rate limit (quota is per model, so another model usually works),
# 404 = model not available to this key, 5xx = Google capacity spike.
FAILOVER_STATUS = {429, 404, 500, 502, 503, 504}

def model_chain(requested: str) -> list[str]:
    chain = [requested]
    for m in [DEFAULT_MODEL] + LITE_CASCADE:
        if m not in chain:
            chain.append(m)
    return chain

def api_request(payload: dict, models: list[str], stream: bool, read_timeout: int = 90):
    """POST with failover across `models`. Returns (response, model_used) or (None, None) after printing the error."""
    headers = {"Authorization": f"Bearer {GEMINI_KEY}", "Content-Type": "application/json"}
    last_err = ""
    for idx, model in enumerate(models):
        body = dict(payload, model=model, stream=stream)
        nxt = models[idx + 1] if idx + 1 < len(models) else None
        try:
            r = requests.post(f"{BASE_URL}/chat/completions", headers=headers, json=body,
                              stream=stream, timeout=(10, read_timeout))
        except KeyboardInterrupt:
            raise
        except requests.RequestException as e:
            last_err = f"Connection Error: {e}"
            if nxt:
                time.sleep(1.0)
                continue
            break
        if r.status_code == 200:
            return r, model
        last_err = f"Gemini API Error ({r.status_code}): {r.text[:600]}"
        if r.status_code in FAILOVER_STATUS and nxt:
            why = {429: "rate-limited (429)", 404: "unavailable (404)"}.get(r.status_code, f"busy ({r.status_code})")
            warn(f"⏳ {model} {why}. Auto-switching to {nxt}...")
            time.sleep(0.5 if r.status_code in (429, 404) else 1.0)
            continue
        break
    print(f"\n❌ {last_err}", file=sys.stderr)
    if "(429)" in last_err:
        print("   Free-tier quota is per model and per project. Wait a minute, or try again later in the day.",
              file=sys.stderr)
    return None, None

def stream_gemini(messages: list[dict], requested_model: str, echo: bool = True) -> str:
    """Streams the reply to stdout (if echo) and returns the full text ('' on failure)."""
    try:
        r, _ = api_request({"messages": messages, "temperature": 0.2}, model_chain(requested_model), stream=True, read_timeout=60)
        if r is None:
            return ""
        full_reply = []
        for line in r.iter_lines():
            if not line: continue
            decoded = line.decode("utf-8", errors="replace")
            if decoded.startswith("data: "):
                data_str = decoded[6:].strip()
                if data_str == "[DONE]": break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"].get("content") or ""
                except Exception:
                    continue
                if echo:
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                full_reply.append(delta)
        if echo:
            print()
        return "".join(full_reply)
    except KeyboardInterrupt:
        print("\n\033[2m[Interrupted by user]\033[0m", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"\n❌ Stream Error: {e}", file=sys.stderr)
        return ""

def ask_gemini(messages: list[dict], requested_model: str, tools: list | None = None, temperature: float = 0.2):
    """Non-streaming call. Returns the raw assistant message dict (so tool calls / signatures survive) or None."""
    payload = {"messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
    try:
        r, _ = api_request(payload, model_chain(requested_model), stream=False, read_timeout=120)
        if r is None:
            return None
        return r.json()["choices"][0]["message"]
    except KeyboardInterrupt:
        print("\n\033[2m[Interrupted by user]\033[0m", file=sys.stderr)
        return None
    except Exception as e:
        print(f"\n❌ Bad API response: {e}", file=sys.stderr)
        return None

# ── Small utilities ───────────────────────────────────────────────────────────
def sh(cmd: list[str], timeout: int = 30, merge: bool = True, cwd: str | None = None, stdin_text: str | None = None):
    """Run a command (no shell). Returns (returncode, output). merge=True folds stderr into stdout in order."""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
            timeout=timeout, text=True, errors="replace", cwd=cwd,
        )
        return p.returncode, p.stdout or ""
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"

def fence(text: str, lang: str = "text") -> str:
    """Wrap text in a code fence that can't be broken by backticks inside the text."""
    longest = max((len(m) for m in re.findall(r'`+', text)), default=0)
    tick = "`" * max(3, longest + 1)
    return f"{tick}{lang}\n{text}\n{tick}"

def strip_code_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r'^```[a-zA-Z]*\n([\s\S]*?)\n```$', t)
    return m.group(1).strip() if m else t

def trim_middle(text: str, budget: int) -> tuple[str, bool]:
    """Keep the head (60%) and tail (40%) of text, drop the middle by whole lines."""
    if len(text) <= budget:
        return text, False
    lines = text.splitlines()
    head, tail, used = [], [], 0
    hb, tb = int(budget * 0.6), int(budget * 0.4)
    for l in lines:
        if used + len(l) + 1 > hb: break
        head.append(l); used += len(l) + 1
    used = 0
    for l in reversed(lines[len(head):]):
        if used + len(l) + 1 > tb: break
        tail.append(l); used += len(l) + 1
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join(head + [f"[... {omitted} lines omitted from the middle ...]"] + tail), True

def number_lines(text: str) -> str:
    return "\n".join(f"{i:>5}| {l}" for i, l in enumerate(text.splitlines(), 1))

# ── Smart log handling ────────────────────────────────────────────────────────
_TS_RE = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|^\s*[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\b\d{2}:\d{2}:\d{2}\b')
_LEVEL_RE = re.compile(r'\b(ERROR|WARN(?:ING)?|INFO|DEBUG|FATAL|CRITICAL|TRACE|NOTICE|level=\w+)\b|\[(?:err|warn|info|debug|notice)\]', re.IGNORECASE)
_ERR_RE = re.compile(
    r"\b(error|fatal|panic|exception|traceback|critical|fail(?:ed|ure|ing)?|denied|refused|timed? ?out|"
    r"oom|out of memory|killed|segfault|cannot|can't|unable|not found|no such|unhealthy|exited|exit code|"
    r"unreachable|corrupt\w*|deadlock|crash\w*)\b", re.IGNORECASE)

_NORM_SUBS = [
    (re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?'), '<TS>'),
    (re.compile(r'\b[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}\b'), '<TS>'),
    (re.compile(r'\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b'), '<T>'),
    (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<UUID>'),
    (re.compile(r'\b0x[0-9a-fA-F]+\b'), '<HEX>'),
    (re.compile(r'\b[0-9a-f]{12,}\b'), '<HEX>'),
    (re.compile(r'\b\d+(?:\.\d+)?(?:ms|µs|us|s)\b'), '<DUR>'),
    (re.compile(r'\b\d{5,}\b'), '<N>'),  # long ids/counters; short numbers (ports, exit codes) stay distinct
]

def normalize_line(line: str) -> str:
    for rx, rep in _NORM_SUBS:
        line = rx.sub(rep, line)
    return line.strip()

def looks_like_log(text: str) -> bool:
    lines = text.splitlines()
    if len(lines) < 20:
        return False
    sample = lines[:200]
    hits = sum(1 for l in sample if _TS_RE.search(l) or _LEVEL_RE.search(l))
    return hits / len(sample) >= 0.4

REPEAT_LIMIT = 4      # a pattern seen more often than this is "noise" and gets collapsed
MAX_LINE_CHARS = 1000

def prepare_log(text: str, budget: int = 60000):
    """
    Turns a raw log into a compact, line-numbered view:
      1. repeated patterns collapsed (first occurrence kept + count + last line number)
      2. if still too big: head + first errors (with context) + later errors + tail
    Returns (text, stats). Line numbers always refer to the ORIGINAL log.
    """
    lines = text.splitlines()
    keys = [normalize_line(l) for l in lines]
    counts = Counter(k for k, l in zip(keys, lines) if l.strip())
    last_at = {}
    for i, k in enumerate(keys):
        last_at[k] = i
    seen, kept, collapsed = set(), [], 0
    for i, (l, k) in enumerate(zip(lines, keys)):
        if not l.strip():
            continue
        body = l if len(l) <= MAX_LINE_CHARS else l[:MAX_LINE_CHARS] + f" …[+{len(l) - MAX_LINE_CHARS} chars]"
        if counts[k] > REPEAT_LIMIT:
            if k in seen:
                continue
            seen.add(k)
            collapsed += 1
            body += f"   [x{counts[k]} similar; last at line {last_at[k] + 1}]"
        kept.append((i + 1, body))

    fmt = lambda n, b: f"{n:>5}| {b}"
    total_chars = sum(len(fmt(n, b)) + 1 for n, b in kept)
    stats = {"total": len(lines), "collapsed": collapsed, "trimmed": False}
    if total_chars <= budget:
        stats["shown"] = len(kept)
        return "\n".join(fmt(n, b) for n, b in kept), stats

    stats["trimmed"] = True
    chosen, used = set(), 0
    def add(positions):
        nonlocal used
        for p in positions:
            if 0 <= p < len(kept) and p not in chosen:
                cost = len(fmt(*kept[p])) + 1
                if used + cost > budget:
                    return False
                chosen.add(p); used += cost
        return True

    err_pos = [p for p, (_, b) in enumerate(kept) if _ERR_RE.search(b)]
    n = len(kept)
    add(range(min(20, n)))                                      # head: startup banner / config
    add(range(max(0, n - 100), n))                              # tail: how it ended
    for p in err_pos[:3]:                                       # first errors: usually the root cause
        add(range(p - 6, p + 7))
    for p in err_pos[3:]:                                       # later errors, thin context
        if not add(range(p - 1, p + 2)): break
    add(range(max(0, n - 400), max(0, n - 100)))                # spend leftover budget extending the tail upward

    out, prev = [], None
    for p in sorted(chosen):
        if prev is not None and p != prev + 1:
            out.append(f"      ⋮ ({kept[p][0] - kept[prev][0] - 1} original lines omitted)")
        out.append(fmt(*kept[p]))
        prev = p
    stats["shown"] = len(chosen)
    return "\n".join(out), stats

# ── Input assembly (stdin + attached files) ───────────────────────────────────
TOTAL_INPUT_BUDGET = 200000   # characters, same ceiling as before

def read_text_file(path: str):
    p = Path(path).expanduser()
    if not p.is_file():
        warn(f"⚠️  Not a file, skipping: {path}")
        return None
    if p.stat().st_size > 50 * 1024 * 1024:
        warn(f"⚠️  Too large (>50 MB), skipping: {path}")
        return None
    raw = p.read_bytes()
    if b"\0" in raw[:2048]:
        warn(f"⚠️  Looks binary, skipping: {path}")
        return None
    return raw.decode("utf-8", errors="replace")

def build_input_block(label: str, raw: str, per_budget: int, number: bool = False, mode: str = "auto"):
    """Redact + shape one input. mode: 'log' (always log view), 'text' (never), 'auto' (detect). Returns (note, body)."""
    cleaned = clean_piped_text(raw)
    if mode == "log" or (mode == "auto" and looks_like_log(cleaned)):
        body, st = prepare_log(cleaned, min(per_budget, 60000))
        extra = f"log view, line-numbered; {st['total']} lines"
        if st["collapsed"]: extra += f", {st['collapsed']} repeating patterns collapsed"
        if st["trimmed"]:   extra += f", trimmed to {st['shown']} lines (head + errors + tail)"
        return extra, body
    body, was_trimmed = trim_middle(cleaned, per_budget)
    if number:
        body = number_lines(body)
    extra = f"{len(raw.splitlines())} lines" + (", middle trimmed" if was_trimmed else "")
    return extra + (", line-numbered" if number else ""), body

def compose_inputs(instruction: str, inputs: list[tuple[str, str, bool, str]]):
    """
    inputs = [(label, raw_text, number_lines, mode)] with mode in {'auto','log','text'}.
    Builds the user message and a one-line stats string. A lone, non-log stdin keeps the old compact format.
    """
    per = max(20000, TOTAL_INPUT_BUDGET // max(1, len(inputs)))
    blocks, stats = [], []
    for label, raw, number, mode in inputs:
        extra, body = build_input_block(label, raw, per, number=number, mode=mode)
        stats.append(f"{label}: {extra}")
        blocks.append((label, extra, body))
    legend = ("Lines prefixed 'N| ' are numbered for reference only (not part of the content). "
              "Entries ending in [xN similar; ...] are collapsed repeats of the same message.")
    if len(blocks) == 1 and blocks[0][0] == "stdin" and "line-numbered" not in blocks[0][1]:
        return f"{instruction}\n\n{fence(blocks[0][2])}", "; ".join(stats)
    parts = [instruction, "", legend, ""]
    for i, (label, extra, body) in enumerate(blocks, 1):
        parts.append(f"=== INPUT {i}: {label} ({extra}) ===")
        parts.append(fence(body))
        parts.append("")
    return "\n".join(parts), "; ".join(stats)

# ── Git helpers ───────────────────────────────────────────────────────────────
NOISE_EXCLUDES = [
    ":(exclude)*.lock", ":(exclude)package-lock.json", ":(exclude)pnpm-lock.yaml", ":(exclude)yarn.lock",
    ":(exclude)go.sum", ":(exclude).terraform.lock.hcl", ":(exclude)*.min.js", ":(exclude)*.min.css", ":(exclude)*.map",
]

def trim_diff(diff: str, per_file: int = 6000, total: int = 60000) -> str:
    chunks = re.split(r'(?m)^(?=diff --git )', diff)
    out = []
    for ch in chunks:
        if len(ch) > per_file:
            ch = ch[:per_file] + f"\n[... {len(ch) - per_file} more chars of this file's diff omitted ...]\n"
        out.append(ch)
    joined = "".join(out)
    if len(joined) > total:
        joined = joined[:total] + "\n[... diff truncated; see stat above for the full list of changed files ...]\n"
    return joined

def in_git_repo() -> bool:
    rc, out = sh(["git", "rev-parse", "--is-inside-work-tree"], merge=False)
    return rc == 0 and out.strip() == "true"

# ── Interactive helpers ───────────────────────────────────────────────────────
def interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()

def prompt_line(prompt: str, default: str | None = None) -> str:
    """input() that can pre-fill an editable default (readline), falling back to plain input()."""
    if default is not None:
        try:
            import readline
            readline.set_startup_hook(lambda: readline.insert_text(default))
            try:
                return input(prompt)
            finally:
                readline.set_startup_hook()
        except ImportError:
            pass
    return input(prompt)

# ── Git: commit / pr / changelog ──────────────────────────────────────────────
COMMIT_ROLE = (
    "You write git commit messages. Output ONLY the commit message: no code fences, no quotes, no commentary.\n"
    "- Subject line: imperative mood, at most 72 characters, no trailing period.\n"
    "- If the change has more than one logical part or the 'why' is not obvious, add a blank line and a short body "
    "(wrapped near 72 columns) using '-' bullets.\n"
    "- Match the style of the recent commit subjects provided (for example Conventional Commits prefixes like "
    "feat:/fix:/chore: if they use them; otherwise plain sentences).\n"
    "- Describe what changed and why, based ONLY on the diff. Never invent issue numbers, ticket IDs or motivations "
    "that are not visible.\n"
    "- Mention lockfile or generated-file changes only if they are the whole change."
)

def cmd_commit(args, model: str):
    if not in_git_repo():
        die("Not inside a git repository.")
    base = ["git", "diff", "--no-color"] + ([] if args.all else ["--staged"])
    if args.all:
        # include staged + unstaged tracked changes; fall back to staged only on a repo with no commits yet
        rc, _ = sh(["git", "rev-parse", "--verify", "HEAD"], merge=False)
        base += ["HEAD"] if rc == 0 else ["--staged"]
    _, stat = sh(base + ["--stat=160"], merge=False)
    if not stat.strip():
        _, unstaged = sh(["git", "diff", "--stat"], merge=False)
        if unstaged.strip():
            die("Nothing is staged. Stage files with `git add`, or use `oracle commit --all` to describe all tracked changes.")
        die("No changes to describe.")
    _, diff = sh(base + ["--", "."] + NOISE_EXCLUDES, merge=False)
    _, recent = sh(["git", "log", "-n", "12", "--format=%s"], merge=False)
    _, branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], merge=False)
    hint = " ".join(args.rest[1:]).strip()

    user = [f"Branch: {branch.strip() or '(unknown)'}"]
    if hint:
        user.append(f"Extra context from the author: {hint}")
    user.append("Recent commit subjects (match this style):\n" + (recent.strip() or "(none yet)"))
    user.append("Changed files:\n" + stat.strip())
    user.append("Diff (lockfiles and minified files omitted):\n" + fence(clean_piped_text(trim_diff(diff)), "diff"))
    user_msg = "\n\n".join(user)
    messages = [{"role": "system", "content": system_prompt(COMMIT_ROLE, terminal=False)},
                {"role": "user", "content": user_msg}]

    attempt = 0
    while True:
        banner(model, f"staged diff: {len(diff.splitlines())} lines" if not args.all else f"all tracked changes: {len(diff.splitlines())} lines")
        msg = strip_code_fences(stream_gemini(messages, model, echo=not (args.print and QUIET)))
        if not msg:
            sys.exit(1)
        if args.print:
            if QUIET:
                print(msg)
            return
        if args.yes or not interactive_tty():
            sys.exit(do_commit(msg, all_tracked=args.all))
        try:
            choice = input(paint("1;32", "\n[a]ccept & commit  [e]dit in $EDITOR  [r]egenerate  [q]uit > ", sys.stdout)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if choice in {"a", "accept", "y"}:
            sys.exit(do_commit(msg, all_tracked=args.all))
        elif choice in {"e", "edit"}:
            sys.exit(do_commit(msg, all_tracked=args.all, edit=True))
        elif choice in {"r", "regen", "regenerate"}:
            attempt += 1
            first = msg.splitlines()[0] if msg else ""
            messages = messages[:2] + [{"role": "assistant", "content": msg},
                                       {"role": "user", "content": f"Write a different take (different wording/emphasis than: {first}). Output only the message."}]
            continue
        else:
            return

def do_commit(msg: str, all_tracked: bool = False, edit: bool = False) -> int:
    cmd = ["git", "commit"] + (["-a"] if all_tracked else [])
    if edit:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write(msg + "\n")
            path = tf.name
        try:
            return subprocess.run(cmd + ["-e", "-F", path]).returncode
        finally:
            os.unlink(path)
    return subprocess.run(cmd + ["-F", "-"], input=msg + "\n", text=True).returncode

def default_base_branch() -> str:
    rc, out = sh(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], merge=False)
    if rc == 0 and out.strip():
        return out.strip()
    for cand in ("origin/main", "origin/master", "main", "master"):
        if sh(["git", "rev-parse", "--verify", "--quiet", cand], merge=False)[0] == 0:
            return cand
    die("Could not find a base branch. Pass one: oracle pr <base>")

PR_ROLE = (
    "You write pull request titles and descriptions in Markdown (it will be pasted into GitHub/Codeberg/GitLab).\n"
    "Output: first line = the title (at most 72 characters, imperative, no 'Title:' prefix), a blank line, then the body "
    "with these sections: '## Summary' (2-4 sentences: what and why), '## Changes' (bullets grouped by area), "
    "'## Notes for reviewers' (ONLY if something is non-obvious: risky areas, migrations, config changes, manual steps).\n"
    "Base everything on the commits and diff provided. Do not invent testing, issue numbers or motivations. "
    "Omit a section if there is nothing real to say."
)

def cmd_pr(args, model: str):
    if not in_git_repo():
        die("Not inside a git repository.")
    base = args.rest[1] if len(args.rest) > 1 else default_base_branch()
    _, log = sh(["git", "log", f"{base}..HEAD", "--no-merges", "--format=%h %s%n%b%n--"], merge=False)
    if not log.strip():
        die(f"No commits ahead of {base}.")
    _, stat = sh(["git", "diff", "--no-color", "--stat=160", f"{base}...HEAD"], merge=False)
    _, diff = sh(["git", "diff", "--no-color", f"{base}...HEAD", "--", "."] + NOISE_EXCLUDES, merge=False)
    _, branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], merge=False)
    log, _ = trim_middle(log, 15000)
    user = (f"Branch: {branch.strip()}  (base: {base})\n\nCommits:\n{log}\n\nChanged files:\n{stat.strip()}\n\n"
            f"Diff (lockfiles and minified files omitted):\n{fence(clean_piped_text(trim_diff(diff)), 'diff')}")
    messages = [{"role": "system", "content": system_prompt(PR_ROLE, terminal=False)},
                {"role": "user", "content": user}]
    banner(model, f"{base}..HEAD: {len(log.splitlines())} log lines")
    if not stream_gemini(messages, model):
        sys.exit(1)

CHANGELOG_ROLE = (
    "You write changelog entries in the Keep a Changelog style, in Markdown, from git commit messages.\n"
    "- Group under only the non-empty headings among: ### Added, ### Changed, ### Fixed, ### Removed, ### Security.\n"
    "- Write for users/operators, not for developers: say what is different, not which files moved.\n"
    "- Merge duplicates and follow-up fixes into one line. Skip pure chores (typo fixes, CI tweaks, formatting) unless user-visible; "
    "if several internal-only changes remain, put them in one '### Internal' bullet list at the end.\n"
    "- Never invent changes. Start with '## [Unreleased]' unless the user gave a version heading to use."
)

def cmd_changelog(args, model: str):
    if not in_git_repo():
        die("Not inside a git repository.")
    arg = args.rest[1] if len(args.rest) > 1 else ""
    if ".." in arg:
        rng = arg
    elif arg:
        rng = f"{arg}..HEAD"
    else:
        rc, tag = sh(["git", "describe", "--tags", "--abbrev=0"], merge=False)
        rng = f"{tag.strip()}..HEAD" if rc == 0 and tag.strip() else "HEAD"
    cmd = ["git", "log", rng, "--no-merges", "--format=%h %s%n%b%n--"]
    if rng == "HEAD":
        cmd += ["-n", "50"]
    _, log = sh(cmd, merge=False)
    if not log.strip():
        die(f"No commits in {rng}.")
    log, _ = trim_middle(clean_piped_text(log), 30000)
    messages = [{"role": "system", "content": system_prompt(CHANGELOG_ROLE, terminal=False)},
                {"role": "user", "content": f"Commit range: {rng}\n\n{fence(log)}"}]
    banner(model, f"range {rng}")
    if not stream_gemini(messages, model):
        sys.exit(1)

# ── logs: gather container/service context, then diagnose ─────────────────────
LOGS_ROLE = (
    "You are a strict, direct Linux/Docker diagnostics assistant. You receive runtime facts about one container or systemd "
    "service (inspect summary, its compose definition, neighbouring containers, host resources) and its log. Log lines are "
    "prefixed 'N| ' with the ORIGINAL line number; lines ending in [xN similar; last at line M] are collapsed repeats.\n"
    "RULES:\n"
    "- Base every conclusion on the supplied data. Cite log lines like (L123). If the cause cannot be determined from what you "
    "were given, say so and name the single most useful extra command or file to look at.\n"
    "- The first error in a log is often the root cause; later errors are often consequences. Say which is which.\n"
    "- Do NOT start with filler. If everything looks healthy, say so in one sentence and stop.\n"
    "- Otherwise answer in EXACTLY these sections: STATUS (one line), LIKELY CAUSE, EVIDENCE (bullets with line numbers), "
    "NEXT STEP (one command to confirm the cause, then the fix), CONFIDENCE (high/medium/low and why).\n"
    "- Never suggest destructive commands (rm, prune, down -v, volume removal) as a first step."
)
LOG_ROLE_GENERIC = LOGS_ROLE.replace(
    "runtime facts about one container or systemd service (inspect summary, its compose definition, neighbouring containers, host resources) and its log",
    "one or more log excerpts and possibly related files")

def _short(s, n=300):
    s = str(s).strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"

def yaml_service_block(text: str, service: str) -> str:
    """Pull one service's block out of a compose file by indentation (no YAML library needed)."""
    lines = text.splitlines()
    in_services = False
    for i, l in enumerate(lines):
        if re.match(r'^services:\s*(#.*)?$', l):
            in_services = True
            continue
        if in_services and l.strip() and not l.startswith((" ", "\t", "#")):
            in_services = False
        if not in_services:
            continue
        m = re.match(r'^(\s+)["\']?' + re.escape(service) + r'["\']?:\s*(#.*)?$', l)
        if m:
            indent, block = len(m.group(1)), [l]
            for l2 in lines[i + 1:]:
                if l2.strip() == "" or l2.lstrip().startswith("#"):
                    block.append(l2); continue
                if len(l2) - len(l2.lstrip()) <= indent:
                    break
                block.append(l2)
            return "\n".join(block).rstrip()
    return ""

def gather_docker_context(target: str, tail: int, since: str | None):
    rc, out = sh(["docker", "inspect", target], timeout=20, merge=False)
    if rc != 0:
        return None
    try:
        d = json.loads(out)[0]
    except (ValueError, IndexError):
        return None
    st, cfg, hc, ns = d.get("State", {}), d.get("Config", {}), d.get("HostConfig", {}), d.get("NetworkSettings", {})
    labels = cfg.get("Labels") or {}
    facts = [f"CONTAINER: {d.get('Name', '').lstrip('/')}  image: {cfg.get('Image')}  created: {d.get('Created')}"]
    facts.append(f"STATE: status={st.get('Status')} running={st.get('Running')} restarting={st.get('Restarting')} "
                 f"oom_killed={st.get('OOMKilled')} exit_code={st.get('ExitCode')} error={_short(st.get('Error') or '-')} "
                 f"restart_count={d.get('RestartCount')} started={st.get('StartedAt')} finished={st.get('FinishedAt')}")
    health = st.get("Health")
    if health:
        facts.append(f"HEALTHCHECK: status={health.get('Status')} failing_streak={health.get('FailingStreak')}")
        for h in (health.get("Log") or [])[-3:]:
            facts.append(f"  last check: exit={h.get('ExitCode')} output={_short(h.get('Output', ''), 200)}")
    facts.append(f"ENTRYPOINT: {cfg.get('Entrypoint')}  CMD: {cfg.get('Cmd')}  USER: {cfg.get('User') or 'default'}")
    facts.append("ENV VAR NAMES (values withheld): " + ", ".join(sorted(e.split("=", 1)[0] for e in (cfg.get("Env") or []))))
    rp = hc.get("RestartPolicy") or {}
    facts.append(f"RESTART POLICY: {rp.get('Name') or 'no'}  MEMORY LIMIT: {hc.get('Memory') or 'none'}  "
                 f"PRIVILEGED: {hc.get('Privileged')}  NETWORK MODE: {hc.get('NetworkMode')}")
    facts.append("NETWORKS: " + ", ".join((ns.get("Networks") or {}).keys()))
    ports = {k: v for k, v in (ns.get("Ports") or {}).items() if v}
    facts.append(f"PUBLISHED PORTS: {json.dumps(ports) if ports else 'none'}")
    mounts = [f"{m.get('Type')}:{m.get('Source')}->{m.get('Destination')}({'rw' if m.get('RW') else 'ro'})" for m in d.get("Mounts", [])]
    facts.append("MOUNTS: " + ("; ".join(mounts) if mounts else "none"))

    project, service = labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")
    compose_block = ""
    if service:
        workdir = labels.get("com.docker.compose.project.working_dir", "")
        for f in (labels.get("com.docker.compose.project.config_files") or "").split(","):
            f = f.strip()
            if not f: continue
            p = Path(f) if os.path.isabs(f) else Path(workdir) / f
            if p.is_file():
                blk = yaml_service_block(p.read_text(errors="replace"), service)
                if blk:
                    compose_block = f"COMPOSE DEFINITION (service '{service}', from {p}):\n{blk}"
                    break
    ps_cmd = ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"]
    if project:
        ps_cmd += ["--filter", f"label=com.docker.compose.project={project}"]
    _, ps = sh(ps_cmd, merge=False)
    neighbours = "\n".join(ps.strip().splitlines()[:25])
    host = []
    for c in (["uptime"], ["free", "-h"], ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay"]):
        rc2, o = sh(c, merge=False)
        if rc2 == 0 and o.strip():
            host.append(o.strip())
    log_cmd = ["docker", "logs", "--tail", str(tail)] + (["--since", since] if since else []) + [target]
    _, logs = sh(log_cmd, timeout=60)
    context = "\n".join(facts)
    if compose_block: context += "\n\n" + compose_block
    if neighbours:    context += f"\n\nCONTAINERS {'IN PROJECT ' + project if project else 'ON HOST'} (name, status, image):\n{neighbours}"
    if host:          context += "\n\nHOST:\n" + "\n".join(host)
    return context, logs

def gather_systemd_context(unit: str, tail: int, since: str | None):
    rc, status = sh(["systemctl", "status", unit, "--no-pager", "-l"], timeout=15)
    low = status.lower()
    # systemctl status: 0 active, 3 inactive/failed (both are real units), 4 unit not found, 1 = other trouble
    if rc not in (0, 3) or "could not be found" in low or "not been booted" in low or "failed to connect" in low:
        return None
    _, show = sh(["systemctl", "show", unit, "-p", "ActiveState,SubState,Result,ExecMainStatus,NRestarts,ExecMainStartTimestamp,MemoryCurrent"], timeout=15)
    jc = ["journalctl", "-u", unit, "-n", str(tail), "--no-pager"] + (["--since", since] if since else [])
    _, logs = sh(jc, timeout=60)
    return f"SYSTEMD UNIT: {unit}\n{show.strip()}\n\nSTATUS OUTPUT:\n{status.strip()}", logs

def collect_attachments(paths: list[str]) -> list[tuple[str, str, bool, str]]:
    out = []
    for p in paths:
        txt = read_text_file(p)
        if txt is not None:
            out.append((f"file {p}", txt, True, "auto"))
    return out

def cmd_logs(args, model: str):
    if len(args.rest) < 2:
        die("Usage: oracle logs <container-or-unit> [-n LINES] [--since 1h]   (see: oracle logs -h)")
    target = args.rest[1]
    tail = args.tail or 2000
    banner_note = f"gathering context for '{target}'"
    note(paint("2", f"[{banner_note}]"))
    got = gather_docker_context(target, tail, args.since) if shutil.which("docker") else None
    kind = "container"
    if got is None:
        got = gather_systemd_context(target, tail, args.since) if shutil.which("systemctl") else None
        kind = "systemd unit"
    if got is None:
        names = sh(["docker", "ps", "-a", "--format", "{{.Names}}"], merge=False)[1].split() if shutil.which("docker") else []
        close = difflib.get_close_matches(target, names, n=3)
        die(f"No container or systemd unit named '{target}'." + (f" Did you mean: {', '.join(close)}?" if close else ""))
    context, logs = got
    if not logs.strip():
        logs = "(no log output in the requested window)"
    inputs = [(f"{kind} log: {target}", logs, False, "log")] + collect_attachments(args.attach)
    log_text, stats = compose_inputs(
        args.rest[2:] and " ".join(args.rest[2:]) or "Diagnose this and tell me what is wrong (or that nothing is).",
        inputs)
    full = f"RUNTIME FACTS ({kind}):\n{clean_piped_text(context)}\n\n{log_text}"
    messages = [{"role": "system", "content": system_prompt(LOGS_ROLE, with_env=True)},
                {"role": "user", "content": full}]
    banner(model, stats)
    reply = stream_gemini(messages, model)
    if reply and args.chat:
        messages.append({"role": "assistant", "content": reply})
        follow_up_chat(model, messages)

# ── review: real linters first, AI triages their findings ─────────────────────
INSTALL_HINTS = {
    "shellcheck": "apt install shellcheck",
    "hadolint": "single binary: github.com/hadolint/hadolint/releases",
    "yamllint": "apt install yamllint  (or: pipx install yamllint)",
    "ansible-lint": "pipx install ansible-lint",
    "terraform": "see developer.hashicorp.com/terraform/install",
    "tflint": "github.com/terraform-linters/tflint",
    "ruff": "pipx install ruff",
}

def detect_kind(path: Path, text: str) -> str:
    name, suf = path.name, path.suffix.lower()
    first = text.splitlines()[0] if text else ""
    if name == "Dockerfile" or name.startswith("Dockerfile.") or suf == ".dockerfile":
        return "dockerfile"
    if suf in {".sh", ".bash"} or re.match(r'#!.*\b(ba)?sh\b', first) or re.match(r'#!.*\b(dash|ksh)\b', first):
        return "bash"
    if suf == ".zsh" or re.match(r'#!.*\bzsh\b', first):
        return "zsh"
    if suf == ".py" or re.match(r'#!.*\bpython', first):
        return "python"
    if suf in {".tf", ".tfvars"}:
        return "terraform"
    if suf == ".json":
        return "json"
    if suf in {".yml", ".yaml"}:
        if re.search(r'(?m)^services:\s*$', text):
            return "compose"
        if re.search(r'(?m)^\s*-?\s*hosts:', text) and re.search(r'(?m)^\s*(tasks|roles|pre_tasks|post_tasks):|import_playbook', text):
            return "ansible"
        return "yaml"
    return "other"

def run_linters(path: Path, kind: str) -> list[tuple[str, str]]:
    """Returns [(tool, output)]. Missing tools are reported, not fatal."""
    results = []
    d = str(path.parent)
    p = str(path)
    def tool(name, cmd, cwd=None, ok_msg="no findings"):
        if not shutil.which(cmd[0]):
            results.append((name, f"(not installed — {INSTALL_HINTS.get(name, 'install it for better results')})"))
            return
        rc, out = sh(cmd, timeout=60, cwd=cwd)
        out = clean_piped_text(out).strip()
        results.append((name, out[:6000] if out else f"{ok_msg} (exit {rc})"))
    if kind == "bash":        tool("shellcheck", ["shellcheck", "-f", "gcc", p])
    elif kind == "zsh":       tool("zsh -n", ["zsh", "-n", p])
    elif kind == "dockerfile": tool("hadolint", ["hadolint", "--no-color", p])
    elif kind == "compose":
        if shutil.which("docker"):
            tool("docker compose config", ["docker", "compose", "-f", p, "config", "-q"], cwd=d, ok_msg="valid")
        tool("yamllint", ["yamllint", "-f", "parsable", p])
    elif kind == "ansible":
        if shutil.which("ansible-lint"):
            tool("ansible-lint", ["ansible-lint", "-p", p])
        else:
            results.append(("ansible-lint", f"(not installed — {INSTALL_HINTS['ansible-lint']})"))
            if shutil.which("ansible-playbook"):
                tool("ansible-playbook --syntax-check", ["ansible-playbook", "--syntax-check", p], cwd=d, ok_msg="syntax ok")
        tool("yamllint", ["yamllint", "-f", "parsable", p])
    elif kind == "yaml":      tool("yamllint", ["yamllint", "-f", "parsable", p])
    elif kind == "terraform":
        tool("terraform fmt", ["terraform", "fmt", "-check", "-diff", p])
        if (path.parent / ".terraform").is_dir() and shutil.which("terraform"):
            tool("terraform validate", ["terraform", "validate", "-no-color"], cwd=d, ok_msg="valid")
        if shutil.which("tflint"):
            tool("tflint", ["tflint", f"--chdir={d}"])
    elif kind == "python":
        if shutil.which("ruff"):
            tool("ruff", ["ruff", "check", "--output-format=concise", p])
        elif shutil.which("flake8"):
            tool("flake8", ["flake8", p])
        else:
            results.append(("ruff", f"(not installed — {INSTALL_HINTS['ruff']}) — falling back to a syntax check"))
            tool("py_compile", [sys.executable, "-m", "py_compile", p], ok_msg="compiles")
    elif kind == "json":
        try:
            json.loads(path.read_text(errors="replace"))
            results.append(("json parser", "valid JSON"))
        except ValueError as e:
            results.append(("json parser", f"INVALID JSON: {e}"))
    return results

REVIEW_ROLE = (
    "You are a senior reviewer of Linux/DevOps code and configuration (Bash, Docker, Compose, Ansible, Terraform, Python, YAML).\n"
    "You get ONE file with line numbers ('N| ' prefixes, reference only) and the raw output of automated linters that really ran "
    "(treat linter output as facts).\n"
    "Do this:\n"
    "1. Triage the linter findings: separate real risks (bugs, data loss, security, breakage) from style noise. Explain each real one in "
    "one or two sentences and why it matters. Group duplicates.\n"
    "2. Then add problems linters cannot see (logic errors, missing error handling, unsafe defaults, empty variables / spaces in paths / "
    "partial failures, non-idempotent steps in Ansible/Terraform, plaintext secrets). Label them 'AI observation' so they are "
    "distinguishable from linter facts. Report only what you can point to a line for; do not pad.\n"
    "3. Give minimal fixes as short before → after snippets. Do not rewrite the whole file.\n"
    "Never invent linter output or line numbers. If the file is clean, say so in one sentence.\n"
    "Answer in EXACTLY these sections: VERDICT (one line), MUST FIX, SHOULD FIX, OPTIONAL, FIXES. Omit empty sections."
)

def cmd_review(args, model: str):
    files = args.rest[1:]
    if not files:
        die("Usage: oracle review <file> [file...]   (see: oracle review -h)")
    for fpath in files:
        path = Path(fpath).expanduser()
        text = read_text_file(str(path))
        if text is None:
            continue
        kind = detect_kind(path, text)
        note(paint("1;36", f"\n━━ {path} ({kind}) ━━"))
        lint = run_linters(path, kind)
        for tool_name, out in lint:
            if out.startswith("(not installed"):
                warn(f"ℹ️  {tool_name}: {out[1:-1] if out.endswith(')') else out}")
        lint_block = "\n\n".join(f"--- {t} ---\n{o}" for t, o in lint) or "(no linter available for this file type)"
        body, trimmed = trim_middle(clean_piped_text(text), 90000)
        user = (f"File: {path.name}  (type: {kind})\n\n{fence(number_lines(body))}\n\n"
                f"LINTER OUTPUT:\n{fence(lint_block)}")
        messages = [{"role": "system", "content": system_prompt(REVIEW_ROLE, with_env=False)},
                    {"role": "user", "content": user}]
        banner(model, f"{len(text.splitlines())} lines; linters: {', '.join(t for t, _ in lint) or 'none'}")
        stream_gemini(messages, model)

# ── run: execute a command, only involve the AI if it fails ───────────────────
RUN_ROLE = (
    "You are a strict, direct Linux/Docker troubleshooting assistant. A command the user ran FAILED. You get the command, its exit code, "
    "and its captured output (lines prefixed 'N| ' for reference; [xN similar...] marks collapsed repeats).\n"
    "RULES: base conclusions only on the output; cite lines like (L12); no filler. If the output is not enough, name the single most "
    "useful next command. Never suggest destructive commands as a first step.\n"
    "Answer in EXACTLY these sections: WHAT FAILED (one line), LIKELY CAUSE, EVIDENCE, NEXT STEP (the command(s) to try), CONFIDENCE."
)

def run_tee(argv: list[str]) -> tuple[int, str]:
    """Run argv on a pseudo-terminal (colors/progress/prompts behave normally), tee the output, return (exit_code, text)."""
    chunks: list[bytes] = []
    def master_read(fd):
        data = os.read(fd, 4096)
        chunks.append(data)
        return data
    status = pty.spawn(argv, master_read)
    code = os.waitstatus_to_exitcode(status)
    if code < 0:
        code = 128 + (-code)
    text = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
    text = "\n".join(l.split("\r")[-1] for l in text.split("\n"))  # collapse progress-bar rewrites
    return code, text

def cmd_run(args, model: str, argv: list[str]):
    if not argv:
        die("Usage: oracle run [flags] -- <command> [args...]   (see: oracle run -h)")
    if shutil.which(argv[0]) is None:
        die(f"'{argv[0]}' is not an executable on PATH. For shell builtins/pipelines use: oracle run -- bash -c '...'")
    t0 = time.time()
    code, out = run_tee(argv)
    dur = time.time() - t0
    if code == 0:
        note(paint("2", f"\n✓ exited 0 in {dur:.1f}s — oracle stayed quiet"))
        sys.exit(0)
    if code == 130:
        sys.exit(130)  # Ctrl+C: the user cancelled, nothing to diagnose
    note(paint("1;33", f"\n✗ exit {code} after {dur:.1f}s — asking oracle..."))
    shown_cmd = clean_piped_text(shlex.join(argv))   # the command line itself can contain secrets (--password x, -e TOKEN=...)
    inputs = [(f"output of: {shown_cmd}", out, False, "log")] + collect_attachments(args.attach)
    prompt, stats = compose_inputs(f"This command failed with exit code {code} (ran for {dur:.1f}s in {os.getcwd()}):\n{shown_cmd}",
                                   inputs)
    messages = [{"role": "system", "content": system_prompt(RUN_ROLE, with_env=True)},
                {"role": "user", "content": prompt}]
    banner(model, stats)
    reply = stream_gemini(messages, model)
    if reply and args.chat:
        messages.append({"role": "assistant", "content": reply})
        follow_up_chat(model, messages)
    sys.exit(code)

# ── do: plain English -> ONE command, tiered confirmation ─────────────────────
DO_ROLE = (
    "You turn a plain-English request into ONE shell command for the user's machine (bash on Linux; see ENVIRONMENT).\n"
    "Reply with ONLY a JSON object, no code fences: "
    '{"command": "...", "explanation": "one or two short sentences", "risk": "safe|caution|destructive"}\n'
    "Rules: one command or one short pipeline; prefer read-only or dry-run forms when the request is ambiguous (list/find/--dry-run "
    "before delete); common tools only; no sudo unless truly required; avoid <placeholders> — pick a sensible default and mention it in "
    "the explanation. risk=destructive if it deletes/overwrites data, touches disks/partitions, force-pushes, removes containers/volumes/images, "
    "or reboots; caution if it changes state (install, restart, chmod/chown, edit in place); safe if read-only."
)

# (regex, reason). BLOCKED is refused outright; DESTRUCTIVE needs a typed code; CAUTION needs y.
_BLOCKED = [
    (r'\brm\s+(?:-[A-Za-z-]+\s+)*(?:--no-preserve-root\s+)?(?:/|/\*|~/?|~/\*|\$HOME/?\*?|/(?:etc|usr|var|boot|bin|sbin|lib|lib64|root|home|opt|srv|mnt|dev|proc|sys)/?\*?)(?:\s|$)',
     "recursive delete of a system or home directory"),
    (r'\bdd\b[^|;&]*\bof=/dev/(?:sd|nvme|vd|hd|mmcblk|loop)', "writes raw data over a disk device"),
    (r'\bmkfs(?:\.\w+)?\b', "formats a filesystem"),
    (r'\bwipefs\b', "wipes filesystem signatures"),
    (r'>\s*/dev/(?:sd|nvme|vd|hd|mmcblk)', "redirects output onto a disk device"),
    (r':\(\)\s*\{\s*:\s*\|\s*:&\s*\}\s*;\s*:', "fork bomb"),
    (r'\bchmod\s+-R\s+\d+\s+/(?:\s|$)', "recursive permission change on /"),
]
_DESTRUCTIVE = [
    (r'\brm\b|\brmdir\b|\bunlink\b|\bshred\b', "deletes files"),
    (r'\btruncate\b', "truncates files"),
    (r'(?<![>&\d])>(?![>&=])\s*[^\s|&;]', "a single '>' overwrites its target file"),
    (r'\bfind\b[^|;&]*(?:-delete|-exec\s+rm)', "find that deletes"),
    (r'\bdocker\s+(?:system|volume|image|container|network|builder)\s+(?:prune|rm)\b|\bdocker\s+(?:rm|rmi|kill)\b', "removes docker objects"),
    (r'\bdocker\s+compose\s+down\b[^|;&]*(?:-v|--volumes|--rmi)', "compose down that also removes volumes/images"),
    (r'\bgit\s+(?:reset\s+--hard|clean\s+-\w*[fdx]|push\s+[^|;&]*(?:--force|-f\b)|branch\s+-D|checkout\s+--\s|restore\s+(?:--source|\.)|stash\s+(?:drop|clear))', "discards git work / rewrites history"),
    (r'\b(?:drop|truncate)\s+(?:database|table|schema)\b|\bdelete\s+from\b', "destructive SQL"),
    (r'\bterraform\s+(?:destroy|apply\s+[^|;&]*-auto-approve)', "terraform destroy / auto-approved apply"),
    (r'\b(?:fdisk|parted|sgdisk|sfdisk|cfdisk|lvremove|vgremove|zpool\s+destroy)\b|\bbtrfs\s+subvolume\s+delete\b', "changes disk layout / removes volumes"),
    (r'\b(?:reboot|shutdown|poweroff|halt)\b|\binit\s+[06]\b|\bsystemctl\s+(?:reboot|poweroff|halt)\b', "takes the machine down"),
    (r'\b(?:iptables|ip6tables)\s+(?:-F|--flush)|\bnft\s+flush\b|\bufw\s+(?:reset|disable)\b', "flushes firewall rules"),
    (r'\bcrontab\s+-r\b', "deletes your whole crontab"),
    (r'\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b', "executes a script downloaded from the internet"),
    (r'\buserdel\b|\bdeluser\b|\bpasswd\b', "changes user accounts"),
]
_CAUTION = [
    (r'\bsudo\b', "runs with root privileges"),
    (r'\b(?:mv|cp)\b', "may overwrite existing files"),
    (r'\bsed\s+(?:[^|;&]*\s)?-i|\bperl\s+[^|;&]*-i', "edits files in place"),
    (r'\bch(?:mod|own|grp)\b', "changes permissions/ownership"),
    (r'>>', "appends to a file"),
    (r'\bsystemctl\s+(?:stop|restart|disable|mask|reload)\b|\bservice\s+\S+\s+(?:stop|restart)\b', "changes service state"),
    (r'\bdocker\s+(?:stop|restart|pause)\b|\bdocker\s+compose\s+(?:down|stop|restart|up|pull)\b', "changes container state"),
    (r'\b(?:kill|pkill|killall)\b', "terminates processes"),
    (r'\b(?:apt|apt-get|dnf|yum|pacman|apk|snap|pip3?|pipx|npm)\s+(?:install|remove|purge|upgrade|uninstall|-S)\b', "installs/removes software"),
    (r'\bgit\s+(?:commit|merge|rebase|pull|push|checkout|switch)\b', "changes git state"),
]

LEVEL_NAMES = {0: "safe", 1: "caution", 2: "destructive", 3: "blocked"}

def classify_command(cmd: str, model_risk: str = "safe") -> tuple[int, list[str]]:
    reasons, level = [], {"safe": 0, "caution": 1, "destructive": 2}.get((model_risk or "safe").lower(), 1)
    for lvl, table in ((3, _BLOCKED), (2, _DESTRUCTIVE), (1, _CAUTION)):
        for rx, why in table:
            if re.search(rx, cmd, re.IGNORECASE if lvl != 3 else 0):
                reasons.append(why)
                level = max(level, lvl)
    return level, list(dict.fromkeys(reasons))

_CODE_WORDS = ["maple", "otter", "cedar", "pixel", "falcon", "harbor", "ember", "quartz", "willow", "comet",
               "tundra", "lantern", "marble", "saffron", "badger", "meadow", "copper", "thistle", "orbit", "juniper"]

def make_code() -> str:
    return f"{secrets.choice(_CODE_WORDS)}{secrets.randbelow(90) + 10}"

def parse_json_object(text: str):
    t = strip_code_fences(text)
    m = re.search(r'\{[\s\S]*\}', t)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None

def confirm_and_run(cmd: str, explanation: str, model_risk: str) -> int:
    while True:
        level, reasons = classify_command(cmd, model_risk)
        print()
        print(paint("1;36", "  $ " + cmd, sys.stdout))
        if explanation:
            print(paint("2", "    " + explanation, sys.stdout))
        print(paint({0: "1;32", 1: "1;33", 2: "1;31", 3: "1;31"}[level],
                    f"    risk: {LEVEL_NAMES[level]}" + (f" — {'; '.join(reasons)}" if reasons else ""), sys.stdout))
        try:
            if level == 3:
                print(paint("1;31", "    oracle will not run this one. Edit it into something safer, or run it yourself if you truly mean it.", sys.stdout))
                ans = input("    [e]dit / [q]uit > ").strip().lower()
                if ans not in {"e", "edit"}:
                    return 1
            elif level == 2:
                code = make_code()
                ans = input(paint("1;31", f"    Type '{code}' to run it, 'e' to edit, anything else cancels > ", sys.stdout)).strip()
                if ans == code:
                    return subprocess.run(["bash", "-c", cmd]).returncode
                if ans.lower() not in {"e", "edit"}:
                    print("    cancelled."); return 1
            else:
                ans = input("    Run it? [y/N/e] > ").strip().lower()
                if ans in {"y", "yes"}:
                    return subprocess.run(["bash", "-c", cmd]).returncode
                if ans not in {"e", "edit"}:
                    print("    cancelled."); return 1
            cmd = prompt_line("    edit> ", default=cmd).strip()
            model_risk = "safe"   # re-classify the edited command from scratch
            if not cmd:
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n    cancelled."); return 1

def cmd_do(args, model: str):
    request = " ".join(args.rest[1:]).strip()
    if not request:
        die('Usage: oracle do "what you want to do"   (see: oracle do -h)')
    messages = [{"role": "system", "content": system_prompt(DO_ROLE, terminal=False, with_env=True)},
                {"role": "user", "content": request}]
    banner(model)
    msg = ask_gemini(messages, model)
    if msg is None:
        sys.exit(1)
    obj = parse_json_object(msg.get("content") or "")
    if not obj or not str(obj.get("command", "")).strip():
        die("The model did not return a usable command. Try rephrasing.")
    cmd, why, risk = str(obj["command"]).strip(), str(obj.get("explanation", "")).strip(), str(obj.get("risk", "caution"))
    if args.print or not interactive_tty():
        if why: print(f"# {why}", file=sys.stderr)
        print(cmd)
        return
    sys.exit(confirm_and_run(cmd, why, risk))

# ── agent: the model may REQUEST read-only commands; you approve each ─────────
# (prefix tuple, flags that are forbidden for this command). "*" means: no extra arguments allowed at all.
READONLY_RULES: list[tuple[tuple[str, ...], set[str]]] = [
    (("docker", "ps"), set()), (("docker", "logs"), {"-f", "--follow"}), (("docker", "inspect"), set()),
    (("docker", "images"), set()), (("docker", "stats"), set()), (("docker", "top"), set()), (("docker", "port"), set()),
    (("docker", "network", "ls"), set()), (("docker", "network", "inspect"), set()),
    (("docker", "volume", "ls"), set()), (("docker", "volume", "inspect"), set()),
    (("docker", "system", "df"), set()), (("docker", "info"), set()), (("docker", "version"), set()),
    (("docker", "compose", "ps"), set()), (("docker", "compose", "logs"), {"-f", "--follow"}),
    (("systemctl", "status"), set()), (("systemctl", "is-active"), set()), (("systemctl", "is-enabled"), set()),
    (("systemctl", "list-units"), set()), (("systemctl", "list-timers"), set()), (("systemctl", "--failed"), set()),
    (("journalctl",), {"-f", "--follow", "--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush",
                       "--sync", "--setup-keys", "--relinquish-var", "--smart-relinquish-var"}),
    (("dmesg",), {"-c", "-C", "--clear", "--read-clear", "-w", "--follow", "-W", "--follow-new"}),
    (("git", "status"), set()), (("git", "log"), set()), (("git", "diff"), set()), (("git", "show"), set()),
    (("git", "branch"), {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u"}),
    (("df",), set()), (("du",), set()), (("free",), set()), (("uptime",), {"*"}), (("lsblk",), set()), (("ss",), set()),
    (("ip", "addr"), set()), (("ip", "a"), set()), (("ip", "route"), set()), (("ip", "link"), set()),
    (("ps",), set()), (("uname",), set()), (("hostname",), {"*"}), (("whoami",), {"*"}), (("id",), set()),
    (("date",), {"-s", "--set"}), (("crontab", "-l"), set()),
    (("ls",), set()), (("stat",), set()), (("cat",), set()), (("head",), set()), (("wc",), set()), (("grep",), set()),
    (("tail",), {"-f", "-F", "--follow"}),
]
_SHELL_META = re.compile(r'[;&|<>`$(){}\n\\]')
_SAFE_EXAMPLE_SUFFIX = ("example", "sample", "template", "dist")
_SECRET_PATH_RE = re.compile(
    r'(?i)(^|/)(\.env(\.[^/]*)?|\.ssh|\.gnupg|\.aws|\.netrc|\.git-credentials|\.docker/config\.json|'
    r'id_(rsa|ed25519|ecdsa|dsa)(\.pub)?|shadow|gshadow|secrets?|[^/]*\.(pem|key|p12|pfx|kdbx|age|gpg|asc))(/|$)|/proc/\d+/environ')

def check_readonly(argv: list[str]):
    """Returns (ok, reason, rule_prefix). Pure function: easy to unit-test."""
    if not argv:
        return False, "empty command", None
    for tok in argv:
        if _SHELL_META.search(tok):
            return False, "shell operators (; & | < > ` $ ( ) { } \\) are not allowed — send one plain command", None
        if _SECRET_PATH_RE.search(tok) and not tok.lower().endswith(tuple("." + s for s in _SAFE_EXAMPLE_SUFFIX)):
            return False, f"'{tok}' looks like a secrets file", None
    for prefix, forbidden in READONLY_RULES:
        if tuple(argv[:len(prefix)]) == prefix:
            rest = argv[len(prefix):]
            if "*" in forbidden and rest:
                return False, f"'{' '.join(prefix)}' takes no arguments here", prefix
            bad = [a for a in rest if a in forbidden or any(a.startswith(f + "=") for f in forbidden if f.startswith("--"))]
            if bad:
                return False, f"flag {bad[0]} is not allowed (not read-only, or would hang on follow)", prefix
            return True, "", prefix
    return False, "not on the read-only allowlist", None

def describe_allowed() -> str:
    return "; ".join(sorted({" ".join(p) for p, _ in READONLY_RULES}))

def prepare_agent_argv(argv: list[str]) -> list[str]:
    """Nudge commands so they stay bounded and non-interactive."""
    argv = list(argv)
    if argv[:2] == ["docker", "stats"] and "--no-stream" not in argv:
        argv.insert(2, "--no-stream")
    if (argv[:2] == ["docker", "logs"] or argv[:3] == ["docker", "compose", "logs"]) and not any(a.startswith(("--tail", "-n", "--since")) for a in argv):
        argv.insert(2 if argv[1] == "logs" else 3, "--tail=200")
    if argv[0] == "journalctl":
        if "--no-pager" not in argv: argv.insert(1, "--no-pager")
        if not any(a in {"-n", "--lines"} or a.startswith("--lines=") or a.startswith("-n") or a.startswith("--since") for a in argv):
            argv += ["-n", "200"]
    if argv[:2] == ["systemctl", "status"] and "--no-pager" not in argv:
        argv.insert(2, "--no-pager")
    return argv

def execute_readonly(argv: list[str]) -> str:
    env = dict(os.environ, PAGER="cat", GIT_PAGER="cat", SYSTEMD_PAGER="cat", LC_ALL="C.UTF-8")
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=20, text=True, errors="replace", env=env)
        out, code = p.stdout, p.returncode
    except FileNotFoundError:
        return f"[exit 127] {argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return "[timed out after 20s — use a more specific/shorter command]"
    out = clean_piped_text(out)
    if len(out) > 12000:
        out = out[:4000] + f"\n[... {len(out) - 12000} chars omitted ...]\n" + out[-8000:]
    return f"[exit {code}]\n{out}"

AGENT_ROLE = (
    "You are a Linux/Docker troubleshooting agent on the user's own machine. You can call the tool run_command to execute READ-ONLY "
    "inspection commands; the user approves each one, so make every call count.\n"
    "METHOD: form a hypothesis, gather the minimum evidence with targeted commands, then answer. Prefer commands with small output "
    "(--tail, -n, --format, filters). One plain command per call: no pipes, redirects, shell operators, sudo or globs. If a call is "
    "blocked or denied, adapt; do not retry the same thing. Stop and answer as soon as you have enough evidence.\n"
    "HONESTY: never claim you checked something you did not run. Secrets in output are masked; do not ask for them.\n"
    "FINAL ANSWER (plain text sections): LIKELY CAUSE, EVIDENCE (command run -> what it showed), FIX (exact commands for the USER to run "
    "themselves; you cannot run changes), CONFIDENCE."
)

def cmd_agent(args, model: str):
    question = " ".join(args.rest[1:]).strip()
    if not question:
        die('Usage: oracle agent "what to investigate"   (see: oracle agent -h)')
    max_steps = max(1, min(args.steps or 8, 20))
    tools = [{"type": "function", "function": {
        "name": "run_command",
        "description": f"Run ONE read-only command on the user's machine (user must approve). Allowed: {describe_allowed()}.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "A single plain command, e.g. 'docker logs --tail 100 nextcloud'"},
            "reason": {"type": "string", "description": "Short reason shown to the user when asking approval"}},
            "required": ["command", "reason"]}}}]
    messages = [{"role": "system", "content": system_prompt(AGENT_ROLE, with_env=True)},
                {"role": "user", "content": question}]
    always: set[tuple[str, ...]] = set()
    banner(model, f"agent, up to {max_steps} steps — you approve every command")
    for step in range(1, max_steps + 1):
        msg = ask_gemini(messages, model, tools=tools)
        if msg is None:
            sys.exit(1)
        calls = msg.get("tool_calls") or []
        messages.append(msg)            # keep the raw message (preserves provider-specific fields)
        if not calls:
            print((msg.get("content") or "").strip())
            return
        if msg.get("content"):
            print(paint("2", (msg["content"] or "").strip(), sys.stdout))
        for i, tc in enumerate(calls):
            tc["id"] = tc.get("id") or f"call_{step}_{i}"
            fn = tc.get("function", {})
            try:
                a = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                a = {}
            command, reason = str(a.get("command", "")).strip(), str(a.get("reason", "")).strip()
            result = agent_gate(command, reason, always, step, max_steps)
            if result is None:
                print(paint("2", "\n[stopped by you]", sys.stdout)); return
            messages.append({"role": "tool", "tool_call_id": tc["id"], "name": fn.get("name", "run_command"), "content": result})
    messages.append({"role": "user", "content": "Step limit reached. Give your best final answer now from what you gathered, in the required sections."})
    final = ask_gemini(messages, model)
    if final:
        print((final.get("content") or "").strip())

def agent_gate(command: str, reason: str, always: set, step: int, max_steps: int):
    """Validate, ask the human, run. Returns the tool-result text, or None if the user quit."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"[rejected: could not parse command: {e}]"
    ok, why, prefix = check_readonly(argv)
    print(paint("1;36", f"\n🔧 step {step}/{max_steps}: {command}", sys.stdout))
    if reason:
        print(paint("2", f"   why: {reason}", sys.stdout))
    if not ok:
        print(paint("1;31", f"   blocked: {why}", sys.stdout))
        return f"[blocked by oracle: {why}]"
    argv = [os.path.expanduser(t) if t.startswith("~") else t for t in argv]
    argv = prepare_agent_argv(argv)
    if prefix not in always:
        if not sys.stdin.isatty():
            return "[denied: no interactive terminal to approve commands]"
        try:
            ans = input(f"   Run? [y]es / [n]o / [a]lways '{' '.join(prefix)}' this session / [q]uit > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if ans in {"q", "quit"}:
            return None
        if ans in {"a", "always"}:
            always.add(prefix)
        elif ans not in {"y", "yes"}:
            return "[the user declined to run this command]"
    out = execute_readonly(argv)
    print(paint("2", "   " + "\n   ".join(out.splitlines()[:6]) + ("\n   ..." if len(out.splitlines()) > 6 else ""), sys.stdout))
    return out

# ── digest: deterministic health checks, AI only explains what they found ─────
# Exit codes: 0 = healthy, 1 = problems found, 2 = nothing could be checked (blind).
# Designed to be wrapped by a scheduler/alerter (e.g. cron-guard): stdout is the alert body.
DIGEST_ROLE = (
    "You are a concise on-call assistant for one Linux/Docker host. You receive the results of deterministic health checks "
    "(already verified facts: do not second-guess them) plus supporting evidence such as container logs.\n"
    "Write a short briefing in plain text. For each problem, most severe first: WHAT (one line), LIKELY CAUSE (cite the evidence; "
    "say 'unclear' if it cannot be determined from what you were given), NEXT (one non-destructive command to confirm or fix). "
    "Finish with at most one line on whether the problems look related. No filler, no greetings.\n"
    "LENGTH: keep the whole briefing under 12 lines and 1500 characters; it is shown in a chat alert that cuts off older lines.\n"
    "Never claim anything is fine that the checks did not cover. Never suggest destructive commands as a first step."
)

def digest_state_path() -> Path:
    return Path.home() / ".oracle" / "digest_state.json"

def load_digest_state() -> dict:
    try:
        return json.loads(digest_state_path().read_text())
    except (OSError, ValueError):
        return {}

def save_digest_state(state: dict):
    try:
        p = digest_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except OSError:
        pass  # state only enables "restarted since last run"; never fail the digest over it

def journal_since(s: str) -> str:
    return f"-{s}" if re.fullmatch(r'\d+[smhd]', s) else s

def finding(sev: str, title: str, lines=None, extra: str = "", container: str | None = None) -> dict:
    return {"sev": sev, "title": clean_piped_text(title),
            "lines": [clean_piped_text(l)[:300] for l in (lines or [])][:12],
            "extra": clean_piped_text(extra), "container": container}

def container_evidence(name: str) -> str:
    _, st = sh(["docker", "inspect", "--format",
                "state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} error={{.State.Error}}", name], timeout=15)
    _, logs = sh(["docker", "logs", "--tail", "60", name], timeout=30)
    view, _ = prepare_log(clean_piped_text(logs), 5000) if logs.strip() else ("(no log output)", None)
    return f"--- evidence for container {name} ---\n{st.strip()}\nlast log lines (numbered):\n{view}"

def digest_docker(ignore: set, state: dict):
    """Returns (findings, summary, skipped_reason, new_restart_counts)."""
    if not shutil.which("docker"):
        return [], None, "docker (not installed)", {}
    rc, out = sh(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"], timeout=20)
    if rc != 0:
        first = [l for l in out.strip().splitlines()[:3]]
        return [finding("problem", "cannot talk to the docker daemon", first)], None, None, {}
    found, total, running = [], 0, 0
    for line in out.splitlines():
        if line.count("|") < 2:
            continue
        name, cstate, status = line.split("|", 2)
        total += 1
        running += cstate == "running"
        if name in ignore:
            continue
        m = re.match(r'Exited \((\d+)\)', status)
        if cstate == "restarting":
            found.append(finding("problem", f"container restarting: {name}", [status], container=name))
        elif "(unhealthy)" in status:
            found.append(finding("problem", f"container unhealthy: {name}", [status], container=name))
        elif m and m.group(1) != "0":
            found.append(finding("problem", f"container exited with code {m.group(1)}: {name}", [status], container=name))
        elif cstate in {"dead", "created", "paused"}:
            found.append(finding("note", f"container {cstate}: {name}", [status]))

    counts = {}
    _, ids = sh(["docker", "ps", "-aq"], timeout=20, merge=False)
    id_list = ids.split()
    if id_list:
        rc2, insp = sh(["docker", "inspect", "--format", "{{.Name}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.ExitCode}}"] + id_list, timeout=30, merge=False)
        prev = state.get("restarts", {})
        for line in insp.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 4:
                continue
            name, rcount, oom = parts[0].lstrip("/"), parts[1], parts[2]
            if not rcount.isdigit():
                continue
            counts[name] = int(rcount)
            if name in ignore:
                continue
            if oom == "true":
                found.append(finding("problem", f"container was OOM-killed: {name}", [f"exit code {parts[3]}, memory limit hit"], container=name))
            if name in prev and counts[name] > prev[name]:
                found.append(finding("problem", f"container restarted {counts[name] - prev[name]}x since the last digest: {name}",
                                     [f"restart count {prev[name]} -> {counts[name]}"], container=name))
    # attach logs/inspect for the first few affected containers (AI context only)
    seen = []
    for f in found:
        c = f.get("container")
        if f["sev"] == "problem" and c and c not in seen and len(seen) < 3:
            seen.append(c)
            f["extra"] = container_evidence(c)
    return found, f"{running}/{total} containers running", None, counts

def digest_disk(warn_pct: int):
    excl = ["-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs", "-x", "efivarfs"]
    rc, out = sh(["df", "-P"] + excl, timeout=20, merge=False)
    if not out.strip():
        return [], None, "disk (df failed)"
    found, worst = [], (-1, "")
    for line in out.splitlines()[1:]:
        m = re.match(r'^\S+\s+\d+\s+\d+\s+\d+\s+(\d+)%\s+(.+)$', line)
        if not m:
            continue
        pct, mount = int(m.group(1)), m.group(2).strip()
        worst = max(worst, (pct, mount))
        if pct >= warn_pct:
            found.append(finding("problem", f"disk {pct}% full: {mount}", [line.strip()]))
    _, iout = sh(["df", "-Pi"] + excl, timeout=20, merge=False)
    for line in iout.splitlines()[1:]:
        m = re.match(r'^\S+\s+\d+\s+\d+\s+\d+\s+(\d+)%\s+(.+)$', line)
        if m and int(m.group(1)) >= 90:
            found.append(finding("problem", f"inodes {m.group(1)}% used: {m.group(2).strip()}", [line.strip()]))
    summary = f"disk max {worst[0]}% ({worst[1]})" if worst[0] >= 0 else "disk ok"
    return found, summary, None

def digest_memory():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        total, avail = info["MemTotal"], info["MemAvailable"]
    except (OSError, KeyError, ValueError):
        return [], None, "memory (/proc/meminfo unreadable)"
    avail_pct = avail * 100 / total
    found = []
    if avail_pct < 10:
        found.append(finding("problem", f"low memory: only {avail_pct:.0f}% available",
                             [f"MemAvailable {avail // 1024} MB of {total // 1024} MB"]))
    swap_total, swap_free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    if swap_total and (swap_total - swap_free) * 100 / swap_total > 50:
        found.append(finding("note", f"swap more than half used ({(swap_total - swap_free) // 1024} MB)"))
    return found, f"mem {100 - avail_pct:.0f}% used", None

def digest_systemd(ignore: set):
    if not shutil.which("systemctl"):
        return [], None, "systemd (not installed)"
    rc, out = sh(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=20)
    low = out.lower()
    if rc != 0 or "not been booted" in low or "failed to connect" in low:
        return [], None, "systemd (not running here)"
    found, units = [], []
    for line in out.splitlines():
        if not line.strip():
            continue
        toks = [t for t in line.split() if t not in {"●", "*", "○", "×", "↻"}]  # bullet column appears depending on systemd version/flags
        unit = toks[0] if toks else ""
        if not unit or unit in ignore:
            continue
        units.append(unit)
        found.append(finding("problem", f"systemd unit failed: {unit}", [line.strip()]))
    for f, unit in zip(found[:3], units[:3]):
        _, st = sh(["systemctl", "status", unit, "--no-pager", "-n", "15", "-l"], timeout=15)
        f["extra"] = f"--- systemctl status {unit} ---\n{clean_piped_text(st)[:2500]}"
    return found, f"{len(units)} failed units", None

def digest_journal(since: str):
    if not shutil.which("journalctl"):
        return [], None, "journal (journalctl not installed)"
    js = journal_since(since)
    rc, out = sh(["journalctl", "-p", "err", "--since", js, "--no-pager", "-q"], timeout=40)
    if "insufficient permissions" in out.lower():
        return [], None, "journal (not readable: add this user to the adm or systemd-journal group)"
    if rc not in (0, 1) and not out.strip():
        return [], None, "journal (journalctl failed)"
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("-- ")]
    found = []
    # kernel OOM killer (separate query: this is the one that silently kills your containers)
    rc2, oom = sh(["journalctl", "-k", "-g", "Out of memory|oom-kill|Killed process", "--since", js, "--no-pager", "-q"], timeout=40)
    oom_lines = [l for l in oom.splitlines() if l.strip() and not l.startswith("-- ")] if rc2 == 0 else []
    if oom_lines:
        found.append(finding("problem", "kernel OOM killer fired", oom_lines[-4:], extra="--- kernel OOM lines ---\n" + "\n".join(oom_lines[-10:])))
    if lines:
        key = lambda l: normalize_line(re.sub(r'\[\d+\]', '[pid]', l))
        counts = Counter(key(l) for l in lines)
        sample = {}
        for l in lines:
            sample.setdefault(key(l), l)
        top = counts.most_common(3)
        worst_n = top[0][1]
        ev = [f"{n}x  {sample[k][:220]}" for k, n in top]
        if worst_n >= 20:
            found.append(finding("problem", f"error spam in the journal: one message repeated {worst_n}x", ev,
                                 extra="--- most frequent journal errors ---\n" + "\n".join(ev)))
        else:
            found.append(finding("note", f"{len(lines)} journal error line(s), {len(counts)} distinct", ev))
    return found, f"{len(lines)} journal error lines", None

def digest_briefing(host: str, stamp: str, since: str, problems: list, notes: list, model: str) -> str:
    parts = [f"Host: {host}\nTime: {stamp}\nWindow: last {since}"]
    for i, f in enumerate(problems, 1):
        parts.append(f"PROBLEM {i}: {f['title']}\n" + "\n".join(f["lines"]) + (f"\n{f['extra']}" if f["extra"] else ""))
    if notes:
        parts.append("NOTES (lower severity, not failures):\n" + "\n".join(f"- {n['title']}" for n in notes))
    body, _ = trim_middle("\n\n".join(parts), 40000)
    messages = [{"role": "system", "content": system_prompt(DIGEST_ROLE, with_env=False)},
                {"role": "user", "content": body}]
    msg = ask_gemini(messages, model)
    return (msg.get("content") or "").strip() if msg else ""

BRIEF_TTL = 6 * 3600   # reuse an identical briefing for 6h: stable alert text + no repeat API calls

def problem_set_key(problems: list) -> str:
    """Same problems (ignoring numbers like 91% -> 92% or restart counters) = same key."""
    norm = sorted(re.sub(r'\d+', '#', p["title"]) for p in problems)
    return hashlib.sha1("\n".join(norm).encode()).hexdigest()[:16]

def cmd_digest(args, model: str) -> int:
    since = args.since or "24h"
    warn_pct = args.disk_warn or 85
    ignore = set(args.ignore or [])
    state = {} if args.no_state else load_digest_state()
    findings, parts, skipped, ran = [], [], [], 0

    f, summary, skip, counts = digest_docker(ignore, state)
    findings += f
    (parts.append(summary) if summary else None)
    (skipped.append(skip) if skip else None)
    ran += 0 if skip else 1
    for check in (lambda: digest_disk(warn_pct), digest_memory, lambda: digest_systemd(ignore), lambda: digest_journal(since)):
        f, summary, skip = check()
        findings += f
        (parts.append(summary) if summary else None)
        (skipped.append(skip) if skip else None)
        ran += 0 if skip else 1
    if counts:
        state["restarts"] = counts
    state["ts"] = int(time.time())

    def finish(code: int) -> int:
        if not args.no_state:
            save_digest_state(state)
        return code

    host, stamp = platform.node() or "this host", time.strftime("%Y-%m-%d %H:%M")
    if ran == 0:
        print(f"❌ digest on {host} ({stamp}): nothing could be checked. Skipped: {'; '.join(skipped)}")
        return 2
    problems = [x for x in findings if x["sev"] == "problem"]
    notes = [x for x in findings if x["sev"] == "note"]

    if not problems:
        state.pop("brief", None)     # recovered: the next incident gets a fresh briefing
        line = f"✓ all clear on {host} ({stamp}): " + ", ".join(parts)
        if skipped:
            line += f"  [skipped: {'; '.join(skipped)}]"
        print(line)
        for n in notes:
            print(f"  note: {n['title']}")
        return finish(0)

    # Layout matters when a wrapper such as cron-guard forwards only the LAST lines of the output:
    # headline, compact raw results, and the AI briefing LAST so it is what the alert shows.
    brief, written = "", ""
    if not args.no_ai:
        key = problem_set_key(problems)
        cached = state.get("brief") or {}
        if cached.get("key") == key and cached.get("text") and time.time() - cached.get("ts", 0) < BRIEF_TTL:
            brief, written_ts = cached["text"], cached["ts"]
        else:
            brief, written_ts = digest_briefing(host, stamp, since, problems, notes, model), int(time.time())
            if brief:
                state["brief"] = {"key": key, "text": brief, "ts": written_ts}
        # Constant wording + only digits differ between a fresh and a reused briefing, so a wrapper that
        # fingerprints the output (cron-guard ignores digits) sees the same text and keeps suppressing repeats.
        written = time.strftime("%Y-%m-%d %H:%M", time.localtime(written_ts))

    print(f"⚠ {len(problems)} problem{'s' if len(problems) != 1 else ''} on {host} ({stamp}, last {since})")
    print()
    print("--- CHECK RESULTS ---")
    for p in problems:
        print(f"✗ {p['title']}")
        for l in p["lines"][:4]:
            print(f"    {l}")
    for n in notes:
        print(f"• {n['title']}")
    if skipped:
        print(f"(skipped checks: {'; '.join(skipped)})")
    if not args.no_ai:
        print()
        print(f"--- AI BRIEFING (written {written}) ---" if brief else "--- AI BRIEFING ---")
        print(brief if brief else "(unavailable: see the check results above)")
    return finish(1)

# ── Interactive chat (also used for follow-ups after a pipe) ──────────────────
CHAT_ROLE = (
    "You are a versatile, highly skilled technical assistant and systems specialist. "
    "While your core strengths are Linux systems, DevOps, and infrastructure, you can assist with "
    "software development in any language, networking, databases, general technology, and problem solving. "
    "Be direct, technically precise, and avoid conversational fluff."
)

GENERIC_ROLE = (
    "You are a strict, direct Linux Systems and DevOps command-line assistant. "
    "Analyze the provided terminal output, logs, or code.\n"
    "RULES:\n"
    "- Answer the user's specific query immediately and directly. Do NOT start with conversational fillers like "
    "'Everything is running normally', 'The provided input is...', or 'Here is the analysis'.\n"
    "- If analyzing code/scripts, give straight answers, explanations, or code improvements.\n"
    "- If analyzing logs with no errors, state that plainly in one short sentence without a meta-commentary preamble.\n"
    "- Several inputs may be provided, each labeled. Lines prefixed 'N| ' are numbered for reference only; cite them like (L123). "
    "Entries ending in [xN similar; ...] are collapsed repeats.\n"
    "- When asked to diagnose errors, answer in these sections: STATUS, LIKELY CAUSE, EVIDENCE (line numbers), NEXT STEP, CONFIDENCE. "
    "The first error is often the root cause; later ones often consequences.\n"
    "- Avoid markdown tables unless comparing multiple distinct entities."
)

def trim_history(history: list[dict], pinned: int, max_chars: int = 250000):
    """Drop the oldest non-pinned messages once the conversation gets too big for the free tier."""
    def size(): return sum(len(m.get("content") or "") for m in history)
    while size() > max_chars and len(history) > pinned + 2:
        del history[pinned:pinned + 2]

def reopen_tty() -> bool:
    """After `... | oracle`, stdin is the pipe (already at EOF). /dev/tty is always your real keyboard."""
    if sys.stdin.isatty():
        return True
    try:
        sys.stdin = open("/dev/tty", "r", encoding="utf-8", errors="replace")
        return True
    except OSError:
        return False

def interactive_chat(model: str, history: list[dict] | None = None):
    seeded = history is not None
    print(f"💬 Starting interactive session with {paint('1;36', model, sys.stdout)}")
    print("   Type 'exit' or Ctrl+C to stop.  /file <path> attaches a file, /clear resets, /help lists commands.\n")
    base = [{"role": "system", "content": system_prompt(CHAT_ROLE)}]
    if history is None:
        history = list(base)
    pinned = 2 if seeded else 1   # keep the system prompt (+ the original piped/log message when seeded)
    while True:
        try:
            prompt = input(paint("1;32", "oracle> ", sys.stdout)).strip()
            if not prompt: continue
            low = prompt.lower()
            if low in {"exit", "quit", "q"}: break
            if low == "/clear":
                history[:] = list(base); pinned = 1
                print("   (conversation cleared)"); continue
            if low == "/help":
                print("   /file <path>   add a file to the conversation (redacted first)\n   /clear         forget everything so far\n   exit           leave"); continue
            if low.startswith("/file"):
                parts = prompt.split(None, 1)
                txt = read_text_file(parts[1].strip()) if len(parts) > 1 else None
                if txt is None:
                    print("   usage: /file <path>"); continue
                _, body = build_input_block(parts[1], txt, 60000, number=True)
                history.append({"role": "user", "content": f"I am attaching the file {parts[1].strip()} (line numbers are for reference only):\n{fence(body)}\nAcknowledge in one short line; I'll ask my question next."})
                print(paint("1;34", "[AI] ", sys.stdout), end="", flush=True)
                reply = stream_gemini(history, model)
                if reply: history.append({"role": "assistant", "content": reply})
                continue
            history.append({"role": "user", "content": prompt})
            trim_history(history, pinned)
            print(paint("1;34", "[AI] ", sys.stdout), end="", flush=True)
            reply = stream_gemini(history, model)
            if reply:
                history.append({"role": "assistant", "content": reply})
            else:
                history.pop()   # failed turn: don't leave a dangling question in the history
            print()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

def follow_up_chat(model: str, messages: list[dict]):
    if not sys.stdout.isatty():
        warn("ℹ️  --chat needs an interactive terminal (stdout is not a TTY); skipping follow-up.")
        return
    if not reopen_tty():
        warn("ℹ️  Could not open /dev/tty for follow-up questions.")
        return
    print()
    interactive_chat(model, history=messages)

# ── context command ───────────────────────────────────────────────────────────
def cmd_context(args):
    sub = args.rest[1] if len(args.rest) > 1 else "show"
    path = context_path()
    if sub == "path":
        print(path); return
    if sub in {"init", "edit"}:
        existed = path.exists()
        if not existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONTEXT_TEMPLATE, encoding="utf-8")
            try: os.chmod(path, 0o600)
            except OSError: pass
            print(f"Created {path}")
        elif sub == "init":
            print(f"{path} already exists (use `oracle context edit`)")
        if sub == "edit":
            editor = os.environ.get("EDITOR") or shutil.which("nano") or "vi"
            subprocess.run(shlex.split(editor) + [str(path)])
        return
    if sub == "show":
        print(f"Context file: {path}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print("Not set up yet. Run: oracle context init   (then: oracle context edit)"); return
        active = load_context()
        print(f"Status: {'ACTIVE — sent with every request (' + str(len(active)) + ' chars)' if active else 'inactive (still the untouched template or empty)'}")
        print("-" * 60); print(text.rstrip()); return
    die("Usage: oracle context [show|init|edit|path]")

# ── Help / manual ─────────────────────────────────────────────────────────────
HELP = {}
HELP["overview"] = """[[t:oracle]] — terminal AI assistant & diagnostics engine (Gemini)

[[h:USAGE]]
  oracle [FLAGS] [QUERY]                  one-shot question (answer streams in)
  <command> | oracle [FLAGS] [QUERY]      analyze piped logs, diffs, scripts
  oracle                                  interactive multi-turn chat
  oracle <COMMAND> [ARGS] [FLAGS]         task modes, below

[[h:COMMANDS]]   (details + examples: oracle <command> -h)
  [[c:commit]] [hint]            commit message from your staged diff; accept / edit / regenerate
  [[c:pr]] [base]                PR title + description for the current branch
  [[c:changelog]] [range|tag]    changelog entries since the last tag
  [[c:logs]] <name>              diagnose a container or systemd unit; gathers the context itself
  [[c:review]] <file>...         runs real linters (shellcheck, hadolint, ...), AI triages the findings
  [[c:run]] -- <cmd...>          run a command; asks the AI only if it FAILS
  [[c:do]] "<what you want>"     plain English -> ONE command, confirmed before it runs
  [[c:agent]] "<question>"       AI investigates with read-only commands that YOU approve one by one
  [[c:digest]]                   host health check for schedulers: one line when healthy, AI briefing when not
  [[c:context]] [init|edit|show] permanent notes about your setup, sent with every request

[[h:FLAGS]]
  [[f:-h, --help]]            this manual (or one command's: oracle logs -h)
  [[f:--models, -avail]]      list models on your account and mark the default
  [[f:-m, --model <name>]]    use a specific model
  [[f:--deep]]                stronger model for hard questions (GEMINI_DEEP_MODEL, default gemini-flash-latest).
                       If your free quota can't serve it, oracle falls back to the lite models by itself.
  [[f:-a, -f, --attach F]]    add a file as a labeled input; repeat for several. -f means "file", NOT "follow".
  [[f:-i, --chat]]            keep chatting afterwards with the answer + inputs still loaded (works after a pipe)
  [[f:-l, --log]]            treat stdin as a log (collapse repeats, number lines, keep error windows)
  [[f:-q, --quiet]]           no status banners (they go to stderr anyway, so stdout is always pipe-clean)
  [[f:--no-context]]          ignore ~/.oracle/context.md for this call
  [[f:-p, --print]]           print only: don't prompt / commit / run   (commit, do)
  [[f:-y, --yes]]             commit without asking                       (commit)
  [[f:--all]]                 describe staged + unstaged tracked changes  (commit)
  [[f:-n, --tail N]]          log lines to fetch, default 2000            (logs)
  [[f:--since T]]             only logs since T, e.g. 30m, 2h, 2026-11-18 (logs, digest; digest default 24h)
  [[f:--steps N]]             max agent steps, default 8, max 20          (agent)
  [[f:--disk-warn N]]         disk usage % that counts as a problem, default 85 (digest)
  [[f:--ignore NAME]]         skip a container or systemd unit; repeatable (digest)
  [[f:--no-ai]]               digest without the AI briefing: checks only, no API call (digest)
  [[f:--no-state]]            digest ignores and leaves untouched its saved baseline/briefing (digest)

[[h:EXAMPLES]]
  [[d:# Diagnose a container; oracle collects inspect data, compose block, neighbours, host stats itself:]]
  oracle logs pocket-id
  [[d:# Or pipe a log and keep talking about it afterwards:]]
  docker logs pocket-id 2>&1 | oracle -i "why does it restart?"
  [[d:# Log + its config as separate labeled inputs:]]
  oracle -a docker-compose.yml -a app.log "why does this restart?"
  [[d:# Commit, PR text, changelog:]]
  git add -p && oracle commit
  oracle pr main | wl-copy
  oracle changelog v1.2.0
  [[d:# Linters first, AI second:]]
  oracle review backup.sh Dockerfile
  [[d:# Only bother the AI when the command fails (exit code is passed through):]]
  oracle run -- docker compose up -d
  [[d:# Plain English to a command (never runs without confirmation):]]
  oracle do "show the 10 biggest folders under /srv"
  [[d:# Let it investigate with read-only commands you approve:]]
  oracle agent "why is nextcloud slow?"
  [[d:# Health check for a scheduler / alert wrapper (exit 0 healthy, 1 problems, 2 blind):]]
  oracle digest --ignore migrate --disk-warn 90
  [[d:# Hard question on the stronger model:]]
  cat deploy.sh | oracle --deep "find edge cases that could leave this half-applied"

[[h:BUILT-IN SAFEGUARDS]]
  • [[b:Auto-Failover:]]  429 (free-tier rate limit), 404 and 5xx on a model switch to the next model automatically.
  • [[b:Secret Masking:]] API keys, tokens, passwords, env-style secrets (POSTGRES_PASSWORD=...), URL credentials,
                  private keys and certificates are redacted from everything sent (stdin, files, logs, command output).
                  It is best-effort, not a guarantee: don't send what you wouldn't paste in public.
  • [[b:Smart Logs:]]     repeated lines collapse into one with a count; line numbers let the AI cite evidence;
                  if too big, it keeps the head, the first errors (with context), later errors and the tail.
  • [[b:Never auto-runs:]] `do` asks first (destructive commands need a typed code, catastrophic ones are refused);
                  `agent` only runs allowlisted read-only commands, each approved by you.
  • [[b:Binary Safe:]]    non-UTF8 or corrupted input never crashes it.

[[h:SETUP]]
  Environment (from /opt/rabbit-hole/.env or your shell):
    GEMINI_API_KEY      required
    GEMINI_MODEL        default model (default: gemini-flash-lite-latest)
    GEMINI_DEEP_MODEL   model used by --deep (default: gemini-flash-latest)
    ORACLE_CONTEXT      path of the context file (default: ~/.oracle/context.md)
    NO_COLOR            disable colors
  Context file: `oracle context init` then `oracle context edit`. Describe your host, stacks, conventions and rules once;
  every answer becomes less generic. Never put secrets in it: it is sent with every request.

[[h:FREE-TIER NOTES]]
  • Quotas are per model and per project. Each `agent` step, each file in `review`, and each regenerate is one request.
  • Google's terms say free-tier requests may be used to improve its products. Redaction helps, but keep that in mind.
  • Start with the default lite model; add --deep only when an answer was shallow.
"""

HELP["commit"] = """[[t:oracle commit]] — commit message from your diff

[[h:USAGE]]
  oracle commit [hint words] [--all] [-p] [-y] [-m model]

  Reads `git diff --staged` itself (lockfiles and minified files are left out of the diff, but listed in the stat),
  looks at your last 12 commit subjects and the branch name so the message matches your style, then offers:
    [a]ccept & commit   [e]dit in $EDITOR   [r]egenerate (a different take)   [q]uit
  The hint is extra context for the AI:  oracle commit "fixes the restart loop from issue 12"

[[h:FLAGS]]
  --all     describe staged + unstaged tracked changes and commit them with `git commit -a`
  -p        print the message only, never commit (use with -q for scripts: msg=$(oracle commit -p -q))
  -y        commit immediately, no prompt (handy in auto-sync scripts)
"""
HELP["pr"] = """[[t:oracle pr]] — pull request title + description

[[h:USAGE]]
  oracle pr [base]

  Compares HEAD with [base] (default: origin/HEAD, else main/master) using the commits and a trimmed diff.
  Output is Markdown: title on the first line, then Summary / Changes / Notes for reviewers. It never invents tests.
  Banners go to stderr, so piping works:  oracle pr main | wl-copy
"""
HELP["changelog"] = """[[t:oracle changelog]] — release notes from commits

[[h:USAGE]]
  oracle changelog              commits since the latest tag (or the last 50 commits if there are no tags)
  oracle changelog v1.2.0       commits since that tag
  oracle changelog v1.1..v1.2   any git range

  Keep-a-Changelog style (Added / Changed / Fixed / Removed / Security), written for users, duplicates merged.
"""
HELP["logs"] = """[[t:oracle logs]] — diagnose a container or systemd unit

[[h:USAGE]]
  oracle logs <container-or-unit> [question...] [-n LINES] [--since 2h] [-a extra-file] [-i] [--deep]

  Collects on its own, then sends it all together with the log:
    • docker inspect summary: state, exit code, OOM flag, restart count, health checks, mounts, ports, networks,
      restart policy, memory limit, env VAR NAMES (values withheld)
    • the service's block from its compose file, containers in the same compose project, uptime / memory / disk
    • for systemd units: status, restart counters and the journal
  The log is condensed (repeats collapsed, line-numbered, first errors kept) and the answer comes as:
  STATUS / LIKELY CAUSE / EVIDENCE (with line numbers) / NEXT STEP / CONFIDENCE.

[[h:EXAMPLES]]
  oracle logs pocket-id
  oracle logs nextcloud --since 1h "why are uploads failing?"
  oracle logs caddy -a ./Caddyfile -i
"""
HELP["review"] = """[[t:oracle review]] — linters find, AI explains

[[h:USAGE]]
  oracle review <file> [file...] [--deep]

  Detects the file type, runs the real tools, and gives the AI the numbered file plus their raw output:
    Bash .sh -> shellcheck          Dockerfile -> hadolint        compose.yml -> docker compose config + yamllint
    Ansible playbook -> ansible-lint (or --syntax-check)           other YAML -> yamllint
    Terraform .tf -> terraform fmt -check (+ validate if initialised, tflint if installed)
    Python -> ruff / flake8 / py_compile      JSON -> parser
  Missing tools are reported with an install hint; the review still happens without them.
  Findings are triaged into VERDICT / MUST FIX / SHOULD FIX / OPTIONAL / FIXES, and anything the AI adds beyond the
  linters is labeled "AI observation". One request per file.
"""
HELP["run"] = """[[t:oracle run]] — run a command, ask the AI only if it fails

[[h:USAGE]]
  oracle run [flags] -- <command> [args...]

  YOU run the command; oracle is just a wrapper (like `time`). It runs on a pseudo-terminal, so colors, progress bars and
  prompts behave normally while the output is also captured. Exit 0: it stays silent. Non-zero (except Ctrl+C): the command,
  exit code and output go to the AI, which answers WHAT FAILED / LIKELY CAUSE / EVIDENCE / NEXT STEP / CONFIDENCE.
  The AI never executes anything here. The original exit code is passed through, so it is safe inside scripts.
  For pipelines or builtins:  oracle run -- bash -c 'cmd1 | cmd2'
  Add -i to keep chatting about the failure afterwards.
"""
HELP["do"] = """[[t:oracle do]] — plain English to ONE command, with confirmation

[[h:USAGE]]
  oracle do "<what you want>" [-p]

  The AI proposes a command + explanation + risk level. oracle also classifies the command itself with its own rules,
  and the stricter verdict wins:
    safe / caution   Run it? [y/N/e]          (caution shows why: sudo, overwrites, restarts, installs ...)
    destructive      you must TYPE a random code shown on screen (e.g. maple42); 'e' edits, anything else cancels
    blocked          refused outright (rm -rf of system dirs, dd onto disks, mkfs, wipefs, fork bombs)
  'e' opens the command pre-filled for editing; the edited command is re-classified from scratch.
  -p prints just the command to stdout (explanation to stderr):  $(oracle do -p "list listening ports")
  Nothing ever runs without your confirmation. Without a terminal it prints instead of running.
"""
HELP["agent"] = """[[t:oracle agent]] — the AI investigates, you approve each command

[[h:USAGE]]
  oracle agent "<what to investigate>" [--steps N] [--deep]

  The model may REQUEST commands (tool calling). For each one you see the command and its reason, then choose
  [y]es / [n]o / [a]lways for that command type this session / [q]uit. Output is redacted and size-capped before it goes back.

[[h:HARD LIMITS]] (enforced by oracle, not by the AI's good behavior)
  • read-only allowlist only: docker ps/logs/inspect/stats/..., systemctl status/is-active/list-*, journalctl, dmesg,
    git status/log/diff/show/branch, df, du, free, ss, ip, ps, ls, cat, head, tail, grep, stat, wc, crontab -l, ...
  • no shell operators, pipes, redirects, sudo or globs; follow flags (-f) and write flags are rejected
  • files that look like secrets (.env, .ssh, *.pem, *.key, shadow, secrets/, ...) are refused
  • 20 second timeout per command; at most --steps commands (default 8); it cannot run fixes, only tell you what to run
  Each step is one API request: mind the free-tier quota.
"""
HELP["digest"] = """[[t:oracle digest]] — host health check for schedulers

[[h:USAGE]]
  oracle digest [--since 24h] [--disk-warn 85] [--ignore NAME]... [--no-ai] [--no-state] [--deep]

  Plain, deterministic checks decide what is wrong; the AI only explains what they found:
    containers   restarting, unhealthy, exited with a non-zero code, OOM-killed, restarted since the previous digest
    disk         any filesystem at/above --disk-warn %, inodes at/above 90 %
    memory       less than 10 % available (swap over 50 % is only a note)
    systemd      failed units
    journal      kernel OOM-killer events, one error repeated 20+ times (spam); other errors are notes

[[h:FLAGS]]
  --since T        journal window, e.g. 6h, 24h, 2d (default 24h)
  --disk-warn N    disk % that counts as a problem (default 85)
  --ignore NAME    skip a container or systemd unit; repeatable (one-shot containers that exit on purpose)
  --no-ai          checks only: no API call, no briefing
  --no-state       don't read/update ~/.oracle/digest_state.json (use for a second schedule such as a heartbeat)
  --deep / -m      stronger / specific model for the briefing

[[h:WHAT YOU SEE]] (exit code 0 healthy, 1 problems, 2 blind)
  0  ONE line, no API call:
       ✓ all clear on homelab (2026-11-18 09:00): 12/12 containers running, disk max 61% (/srv), mem 38% used, 0 failed units, 0 journal error lines
     plus "note:" lines for low-severity things, and "[skipped: ...]" for checks that could not run here.
  1  headline, then the check results, then the AI briefing LAST:
       ⚠ 2 problems on homelab (2026-11-18 03:00, last 24h)

       --- CHECK RESULTS ---
       ✗ container restarting: pocket-id
           Restarting (1) 3 seconds ago
       ✗ disk 91% full: /srv
           /dev/sda1 ... 91% /srv

       --- AI BRIEFING (written 2026-11-18 03:00) ---
       <what / likely cause (cites the log lines) / next command, under 12 lines>
     If the AI is unreachable the briefing says so, but the findings and exit code 1 are unchanged.
  2  nothing could be checked (no docker, no systemd, journal unreadable ...): never reported as healthy.

[[h:USING IT WITH cron-guard]]
  cron-guard forwards only the LAST 15 lines (max 3000 chars) of the output to Telegram and keeps the full log when it is longer,
  which is why the briefing comes last. It also fingerprints that tail to decide whether an error is "the same" for --repeat-after,
  so oracle reuses an identical briefing for 6 hours while the same problems persist (stable text, and no repeat API calls;
  the "written <time>" stamp in its header shows when it was generated).
    # every 6h, alert only on trouble, remind every 12h, send a "recovered" message:
    0 */6 * * *  python3 /path/cron-guard.py --mode fail --timeout 300 --repeat-after 12h --recovery "Oracle Digest" "python3 /path/oracle.py digest --ignore migrate"
    # weekly proof of life (own state so it doesn't steal the restart baseline):
    0 9 * * 1    python3 /path/cron-guard.py --mode all --timeout 300 "Oracle Digest weekly" "python3 /path/oracle.py digest --no-state --ignore migrate"
  Use full paths in cron (its PATH is minimal). Root's crontab can read the whole journal; a user crontab needs the adm or
  systemd-journal group (otherwise the journal check shows up as skipped).

[[h:NOTES]]
  • "Restarted since last digest" needs a baseline: the first run only records restart counts (~/.oracle/digest_state.json).
    Run it as the same user (same HOME) each time; root's cron has HOME=/root, so its state differs from your shell's.
  • A healthy run clears the saved briefing, so the next incident gets a fresh one.
  • For problem containers (max 3) the last 60 log lines go to the AI, redacted and collapsed like `oracle logs`.
  • Everything printed is secret-masked, since alerts leave the machine.
"""
HELP["context"] = """[[t:oracle context]] — your permanent setup notes

[[h:USAGE]]
  oracle context init     create ~/.oracle/context.md from a template (or ORACLE_CONTEXT path)
  oracle context edit     open it in $EDITOR
  oracle context show     print it and whether it is active
  oracle context path     print the path

  Everything in the file is added to the system prompt of every call (8000 chars max, redacted like any input), so the AI
  knows your distro, where stacks live, your conventions and what to never suggest. Skip it once with --no-context.
  Never put secrets in it.
"""

def _render_help(text: str) -> str:
    def sub(m):
        kind, body = m.group(1), m.group(2)
        code = {"t": "1;36", "h": "1;33", "c": "1;36", "f": "1;32", "d": "2", "b": "1"}[kind]
        return paint(code, body, sys.stdout)
    return re.sub(r'\[\[([thcfdb]):(.*?)\]\]', sub, text)

def print_help(topic: str | None = None):
    if topic and topic in HELP:
        print(_render_help(HELP[topic]))
    else:
        if topic and topic not in HELP:
            print(f"(no dedicated help for '{topic}')\n")
        print(_render_help(HELP["overview"]))

# ── Generic mode: pipe / attach / one-shot ────────────────────────────────────
def cmd_generic(args, model: str):
    query = " ".join(args.rest).strip()
    is_piped = not sys.stdin.isatty()
    raw_input = ""
    if is_piped:
        try:
            raw_input = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        except Exception:
            raw_input = ""
    inputs: list[tuple[str, str, bool, str]] = []
    if raw_input.strip():
        inputs.append(("stdin", raw_input, False, "log" if args.log else "auto"))
    inputs += collect_attachments(args.attach)

    # Guard: stdin was piped but completely empty and nothing else to work with
    if is_piped and not inputs and not query and not args.attach:
        print("ℹ️  Piped input was empty. Nothing to analyze.")
        sys.exit(0)

    # Case 1: piped and/or attached inputs
    if inputs:
        instruction = query or "Analyze this output/code, explain any issues, and suggest improvements or fixes:"
        full_prompt, stats = compose_inputs(instruction, inputs)
        messages = [{"role": "system", "content": system_prompt(GENERIC_ROLE, with_env=False)},
                    {"role": "user", "content": full_prompt}]
        banner(model, stats)
        reply = stream_gemini(messages, model)
        if reply and args.chat:
            messages.append({"role": "assistant", "content": reply})
            follow_up_chat(model, messages)
        sys.exit(0 if reply else 1)

    # Case 2: one-shot CLI question
    if query:
        role = ("You are a strict, direct Linux Systems and DevOps command-line assistant. "
                "Answer the user's question directly and concisely. Never use introductory filler or conversational preamble. "
                "Output clean terminal text only.")
        messages = [{"role": "system", "content": system_prompt(role)},
                    {"role": "user", "content": query}]
        banner(model)
        reply = stream_gemini(messages, model)
        if reply and args.chat:
            messages.append({"role": "assistant", "content": reply})
            follow_up_chat(model, messages)
        sys.exit(0 if reply else 1)

    # Case 3: Interactive Chat Mode (no query, no pipe)
    interactive_chat(model)

# ── Argument handling ─────────────────────────────────────────────────────────
SUBCOMMANDS = {"commit", "pr", "changelog", "logs", "review", "run", "do", "agent", "digest", "context", "help"}
VALUE_FLAGS = {"-m", "--model", "-a", "-f", "--attach", "--file", "-n", "--tail", "--since", "--steps", "--disk-warn", "--ignore"}
BOOL_FLAGS = {"-h", "--help", "--models", "--deep", "-i", "--chat", "-l", "--log", "-q", "--quiet",
              "--no-context", "-p", "--print", "-y", "--yes", "--all", "--no-ai", "--no-state"}

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oracle", add_help=False, allow_abbrev=False)
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--models", action="store_true")
    p.add_argument("-m", "--model")
    p.add_argument("--deep", action="store_true")
    p.add_argument("-a", "-f", "--attach", "--file", dest="attach", action="append", default=[])
    p.add_argument("-i", "--chat", action="store_true")
    p.add_argument("-l", "--log", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--no-context", action="store_true")
    p.add_argument("-p", "--print", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("-n", "--tail", type=int)
    p.add_argument("--since")
    p.add_argument("--steps", type=int)
    p.add_argument("--disk-warn", type=int)
    p.add_argument("--ignore", action="append", default=[])
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--no-state", action="store_true")
    p.add_argument("rest", nargs="*")
    return p

def _skip_flags(tokens: list[str], start: int = 0) -> int:
    """Index of the first token that is not an oracle flag (or its value)."""
    i = start
    while i < len(tokens):
        t = tokens[i]
        if t in VALUE_FLAGS:
            i += 2
        elif t.split("=", 1)[0] in VALUE_FLAGS and t.startswith("--") and "=" in t:
            i += 1
        elif t in BOOL_FLAGS:
            i += 1
        else:
            break
    return i

def find_mode(raw: list[str]):
    """Returns (mode|None, flags_before, everything_after_the_mode_word)."""
    if "--" in raw[:_skip_flags(raw) + 1] and raw[_skip_flags(raw)] == "--":
        return None, raw, []
    i = _skip_flags(raw)
    if i < len(raw) and raw[i] in SUBCOMMANDS:
        return raw[i], raw[:i], raw[i + 1:]
    return None, raw, []

def main():
    global QUIET, USE_CONTEXT
    raw = ["--models" if a in {"-avail", "--avail", "-m-list"} else a for a in sys.argv[1:]]
    mode, pre, post = find_mode(raw)

    run_argv: list[str] = []
    if mode == "run":
        j = _skip_flags(post)
        flags = post[:j]
        rest_cmd = post[j:]
        if rest_cmd[:1] == ["--"]:
            rest_cmd = rest_cmd[1:]
        elif "--" in flags:
            pass
        run_argv = rest_cmd
        parse_list = pre + flags
    else:
        parse_list = pre + post if mode else raw

    args, unknown = build_parser().parse_known_intermixed_args(parse_list)
    words = args.rest + unknown
    args.rest = ([mode] if mode else []) + words
    QUIET = args.quiet
    USE_CONTEXT = not args.no_context

    # Help: -h anywhere, `oracle help [topic]`, or the old bare word `help`
    if args.help or mode == "help" or (mode is None and words == ["help"]):
        topic = None
        if mode == "help":
            topic = words[0] if words else None
        elif mode:
            topic = mode
        print_help(topic)
        sys.exit(0)

    if mode == "context":
        cmd_context(args)
        sys.exit(0)

    if not GEMINI_KEY:
        print("❌ Error: GEMINI_API_KEY missing in /opt/rabbit-hole/.env", file=sys.stderr)
        print("   Get your free key at https://aistudio.google.com and add it to .env", file=sys.stderr)
        sys.exit(1)

    # Model listing
    if args.models:
        models = get_available_models(GEMINI_KEY)
        print("\n📋 Available Gemini Models on your account:")
        for m in models:
            tag = " \033[1;32m(DEFAULT)\033[0m" if m == DEFAULT_MODEL else ""
            print(f"  • {m}{tag}")
        print()
        sys.exit(0)

    # Model choice: -m wins, then --deep, then the default. Never empty (hardened safeguard).
    model = (args.model or "").strip() or (DEEP_MODEL if args.deep else DEFAULT_MODEL)
    if not model:
        model = "gemini-flash-lite-latest"

    if mode == "commit":      cmd_commit(args, model)
    elif mode == "pr":        cmd_pr(args, model)
    elif mode == "changelog": cmd_changelog(args, model)
    elif mode == "logs":      cmd_logs(args, model)
    elif mode == "review":    cmd_review(args, model)
    elif mode == "run":       cmd_run(args, model, run_argv)
    elif mode == "do":        cmd_do(args, model)
    elif mode == "agent":     cmd_agent(args, model)
    elif mode == "digest":    sys.exit(cmd_digest(args, model))
    else:                     cmd_generic(args, model)

if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
