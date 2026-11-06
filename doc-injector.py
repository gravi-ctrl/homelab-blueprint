#!/usr/bin/env python3
# @DESCRIPTION: Analyzes ~/scripts and automatically injects an AI-generated capabilities header block using Groq
# @FREQUENCY: On Demand
# @USES_ENV: GROQ_API_KEY, GROQ_MODEL
# @CAPABILITIES:
#   • Automatically generates and injects evidence-verified capabilities headers into shell and Python scripts using Groq AI.
#   • Tracks documentation drift through Git history, proposing only edits, additions, or removals justified by subsequent code changes.
#   • Rejects unsupported claims using literal code-evidence matching and an independent second-pass AI review.
#   • Protects existing documentation by requiring at least two verified bullets before applying header changes.
#   • Requires explicit confirmation for updates, with dry-run previews and non-interactive write protection.
#   • Prevents incomplete documentation from being written when scripts exceed the analysis limit, unless explicitly permitted.
#   • Skips the model entirely when no code has changed since the header’s commit
#   • Handles API rate limits with adaptive retry delays and falls back to an available preferred model when discovery fails
# ==============================================================================
# USAGE
#   ./doc-injector.py [flags] [script ...]    (no script = scan the whole scripts folder)
#
#   Script WITH a capabilities header  -> update mode: diffs the script against the commit that
#       last touched its header and asks the model only what that diff made wrong or missing.
#       No code changes = no model call. The proposed change is shown and needs a y/N confirmation.
#   Script WITHOUT a header            -> first-draft mode: the model reads it and writes one.
#
#   --dry-run          show proposed changes, write nothing
#   --yes              apply update-mode changes without asking
#   --force            regenerate the whole header from scratch (skips update mode)
#   --partial          allow writing a first draft when the model only saw part of a big script
#   --no-review        skip the second-pass reviewer (saves one model call per script)
#   --print-prompt F   print the full, untruncated prompt for script F, to paste into a bigger model
# ==============================================================================

import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

# Exclusions
IGNORE_DIRS = {".git", "venv", "node_modules", "_archive"}
IGNORE_FILES = {Path(__file__).name}

PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "allam-2-7b"
]
PROMPT_CHAR_LIMIT = 12000
MIN_VERIFIED_BULLETS = 2
DIFF_CHAR_LIMIT = 9000  # update mode refuses bigger diffs instead of guessing
UNCOMMITTED = "UNCOMMITTED"
REASONING_EFFORT = "medium"  # gpt-oss models only: low | medium | high
TEMPERATURE = 0.2  # 0 can send gpt-oss reasoning into loops that burn the whole token budget
EXCLUDE_PATTERNS = ["whisper", "guard", "orpheus", "vision"]

def load_dotenv(path):
    if not path.is_file(): return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"\''))

load_dotenv(ENV_PATH)
GROQ_KEY = os.environ.get("GROQ_API_KEY")

def discover_best_model(token: str) -> str:
    override = os.environ.get("GROQ_MODEL", "").strip()
    if override: return override
    reason = "unknown error"
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if r.status_code == 200:
            available = [m["id"] for m in r.json().get("data", [])]
            for pref in PREFERRED_MODELS:
                if pref in available: return pref
            chat_models = [m for m in available if not any(p in m.lower() for p in EXCLUDE_PATTERNS)]
            if chat_models: return chat_models[0]
            reason = "no usable chat model in the list"
        else:
            reason = f"model list returned HTTP {r.status_code}"
    except Exception as e:
        reason = f"model list request failed ({type(e).__name__})"
    # Fall back to the best model rather than the smallest: small reasoning models run out of tokens on this task.
    print(f"⚠ Could not discover models ({reason}); falling back to {PREFERRED_MODELS[0]}", file=sys.stderr)
    return PREFERRED_MODELS[0]

def display_name(p: Path) -> str:
    try:
        return str(p.relative_to(SCRIPT_DIR))
    except ValueError:
        return str(p)

def strip_capabilities(content: str) -> str:
    return re.sub(r'# @CAPABILITIES:.*?(?=\n# [A-Z_]+:|\n# ===|\n[^#]|\Z)', '', content, flags=re.DOTALL)

def code_only(content: str) -> str:
    """Drop every full-line comment (keeps the shebang). Comments describe intent,
    which is exactly what drifts; the model should describe what the code does."""
    kept = []
    for i, line in enumerate(content.splitlines()):
        if line.lstrip().startswith("#") and not (i == 0 and line.startswith("#!")):
            continue
        kept.append(line)
    return "\n".join(kept)

def leading_comment_block(content: str) -> str:
    """The documentation block at the top of a script: the shebang plus consecutive comment/blank lines
    up to the first real line of code. Inline comments are what drifts; this block is the author's overview."""
    out = []
    for line in content.splitlines():
        if line.strip() == "" or line.lstrip().startswith("#"):
            out.append(line)
        else:
            break
    return "\n".join(out)

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def _squash(s: str) -> str:
    """Ignore whitespace, semicolons, quote characters and backslashes so a model joining lines with ';' doesn't cause false drops."""
    return re.sub(r"[\s;'\"`\\]+", "", s)

def evidence_ok(evidence, code_squashed: str) -> bool:
    """Evidence is one snippet or a list of them; '...' skips text. Every part must appear in the code."""
    snippets = evidence if isinstance(evidence, list) else [evidence]
    parts = []
    for sn in snippets:
        parts += [_squash(x) for x in re.split(r"\.\.\.|…", str(sn))]
    parts = [x for x in parts if len(x) >= 5]
    return bool(parts) and all(x in code_squashed for x in parts)

def verify_bullets(items, code_squashed: str):
    """Keep only claims whose quoted evidence literally appears in the code (comments excluded)."""
    good, dropped = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        claim = str(it.get("claim", "")).strip()
        if not claim:
            continue
        if evidence_ok(it.get("evidence", ""), code_squashed):
            good.append(claim)
        else:
            dropped.append((claim, it.get("evidence", "")))
    return good, dropped

def groq_chat(prompt: str, model_id: str, label: str, effort: str | None = None, max_retries: int = 6):
    """One chat call with 429-aware retries. Returns (reply_text, finish_reason); (None, None) on failure."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {"model": model_id, "messages": [{"role": "user", "content": prompt}], "temperature": TEMPERATURE}
    if "gpt-oss" in model_id:
        payload["reasoning_effort"] = effort or REASONING_EFFORT
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)

            if r.status_code == 429:
                wait_time = None
                # Try to parse the exact time from Groq's error message
                m = re.search(r'try again in (\d+(?:\.\d+)?)s', r.text)
                if m:
                    wait_time = float(m.group(1)) + 2.0

                # If parsing fails (e.g., hit RPM limit instead), use exponential backoff: 15s, 30s, 60s...
                if not wait_time:
                    wait_time = 15.0 * (2 ** (attempt - 1))

                print(f"    ⏳ Rate limit reached. Waiting {wait_time:.1f}s before retry (attempt {attempt}/{max_retries})...")
                time.sleep(wait_time)
                continue

            if r.status_code != 200:
                print(f"    ⚠ Groq API Error ({r.status_code}) on {label}: {r.text}", file=sys.stderr)
                return None, None

            choice = r.json()["choices"][0]
            raw = (choice["message"].get("content") or "").strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            return re.sub(r'\s*```$', '', raw), choice.get("finish_reason")

        except Exception as e:
            if attempt == max_retries:
                print(f"    ⚠ Request failed on {label}: {e}", file=sys.stderr)
                return None, None
            time.sleep(5)
    return None, None

def parse_json_list(raw: str):
    """Tolerant JSON-array parser: finds the outermost [...], allows raw control chars, repairs invalid backslash escapes."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return None
    chunk = raw[start:end + 1]
    repaired = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', chunk)
    for text in (chunk, repaired):
        try:
            return [i for i in json.loads(text, strict=False) if isinstance(i, dict)]
        except Exception:
            continue
    return None

def groq_json_list(prompt: str, model_id: str, label: str, none_on_fail: bool = False):
    """Chat call that must return a JSON array of objects. A retry with an identical request would repeat itself,
    so the second attempt lowers reasoning effort to low and tells the model its last reply was unusable.
    Returns [] on failure, or None when none_on_fail is set (so an empty valid answer can be told apart)."""
    fail = None if none_on_fail else []
    attempts = [
        (REASONING_EFFORT, prompt),
        ("low", prompt + "\n\nIMPORTANT: your previous reply was empty or not valid JSON. Think briefly, then reply with ONLY the raw JSON array."),
    ]
    for effort, pr in attempts:
        raw, finish = groq_chat(pr, model_id, label, effort=effort)
        if raw is None:
            return fail
        parsed = parse_json_list(raw)
        if parsed is not None:
            return parsed
        print(f"    ⚠ Unparseable reply on {label} (finish_reason={finish}, effort={effort}); "
              f"starts {raw[:150]!r} ... ends {raw[-100:]!r}", file=sys.stderr)
    return fail

def build_generation_prompt(file_name: str, code: str, limit: int | None, paste: bool = False) -> str:
    """limit=None sends everything (used by --print-prompt). paste=True asks for ready-to-paste header bullets."""
    truncated = limit is not None and len(code) > limit
    shown = code if limit is None else code[:limit]
    rules = (
        f"You are a DevOps engineer writing a 'Features & Safeguards' list for an infrastructure dashboard. Analyze this script ('{file_name}').\n"
        "Extract between 2 to 8 bullet points highlighting its practical utility, unique features, and operational safeguards.\n"
        "STRICT RULES:\n"
        "1. Describe ONLY behavior the code below actually performs. Never infer from names or intent. If unsure, omit it.\n"
        "2. Inline comments have been removed on purpose. Any comment block at the very top is the author's overview and may be outdated: when it conflicts with the code, trust the code.\n"
        "3. Scale the number of bullets to the script's complexity (2 for simple scripts, up to 8 for large ones).\n"
        "4. Focus on WHAT it achieves and WHY it's useful. Do NOT state obvious mechanics ('Uses Python'), and skip argument validation, "
        "usage errors, path setup and bullets that merely list which commands are called (stat, grep, sort); describe outcomes and safeguards, "
        "not steps, unless that IS the script's purpose. Never write bullets about shell options (set -e, pipefail), umask, shebangs or imports.\n"
        "5. Do NOT mention environment variables (handled elsewhere).\n"
        "6. Each bullet must be an active, precise statement under 20 words.\n"
    )
    if paste:
        rules += "7. Return ONLY the bullets, one per line, each starting exactly with '#   • ' (a comment marker, three spaces, a bullet). No numbering, no commentary, no markdown.\n"
    else:
        rules += (
            "7. Every claim needs an 'evidence' field: a JSON list of 1-3 short snippets (each under 100 chars) copied EXACTLY from the code, "
            "covering the key lines behind the claim. You may write '...' inside a snippet to skip text, and you should OMIT double quotes and backslashes from snippets "
            "(they are ignored when matching and they break JSON). A claim can describe the overall "
            "behavior these lines produce together. If no exact snippets support the claim, drop it.\n"
            "8. Return ONLY a raw JSON array like [{\"claim\": \"...\", \"evidence\": [\"...\", \"...\"]}]. No markdown backticks.\n"
        )
        if truncated:
            rules += "9. The code was truncated. For the part you cannot see you may rely on the top comment block, quoting it as evidence, but never invent anything it does not say.\n"
    return rules + f"\nCODE:\n{shown}"

def query_groq_capabilities(file_name: str, code: str, model_id: str) -> list[dict]:
    return groq_json_list(build_generation_prompt(file_name, code, PROMPT_CHAR_LIMIT), model_id, file_name)

def review_claims(file_name: str, code: str, claims: list[str], model_id: str) -> list[tuple[str, str]]:
    """Second pass: a strict reviewer re-checks each claim against the code. Returns [(claim, reason)] to reject."""
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1))
    prompt = (
        f"You are a strict code reviewer checking documentation for the script '{file_name}'. Below are the script (comments removed) "
        "and numbered claims about it.\n"
        "For EACH claim, trace the actual control flow (what `continue`, `||`, `&&`, `set -e`, `if` branches really do) and decide whether "
        "the code does exactly what the claim says. Mark a claim unsupported if it is wrong, reversed, exaggerated, or only loosely related.\n"
        "Return ONLY a raw JSON array like [{\"n\": 1, \"supported\": true, \"reason\": \"under 15 words\"}], one entry per claim. "
        "No markdown backticks.\n\n"
        f"CLAIMS:\n{numbered}\n\nCODE:\n{code[:PROMPT_CHAR_LIMIT]}"
    )
    rejected = []
    for v in groq_json_list(prompt, model_id, f"{file_name} (review)"):
        try:
            n = int(v.get("n"))
        except (TypeError, ValueError):
            continue
        if v.get("supported") is False and 1 <= n <= len(claims):
            rejected.append((claims[n - 1], str(v.get("reason", "")).strip()))
    return rejected

def find_block(lines):
    """(start, end) line indexes of the @CAPABILITIES block within the first 40 lines, or None."""
    for i, line in enumerate(lines[:40]):
        if line.startswith("# @CAPABILITIES:"):
            j = i + 1
            while j < len(lines) and lines[j].startswith("#   "):
                j += 1
            return i, j
    return None

def parse_bullets(lines, start, end) -> list[str]:
    out = []
    for line in lines[start + 1:end]:
        m = re.match(r"#\s+•\s+(.*\S)\s*$", line)
        if m:
            out.append(m.group(1))
    return out

def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)

def header_base_commit(path: Path, first: int, last: int):
    """Newest commit that touched the header's bullet lines (1-based, inclusive). Returns (sha, error)."""
    try:
        r = _git(["blame", "--porcelain", "-L", f"{first},{last}", "--", path.name], path.parent)
    except Exception as e:
        return None, f"git unavailable: {type(e).__name__}"
    if r.returncode != 0:
        return None, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "git blame failed"
    times, shas, cur = {}, [], None
    for line in r.stdout.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \d+ \d+", line)
        if m:
            cur = m.group(1)
            shas.append(cur)
        elif line.startswith("committer-time ") and cur:
            times[cur] = int(line.split()[1])
    if not shas:
        return None, "empty blame output"
    if any(set(x) == {"0"} for x in shas):
        return UNCOMMITTED, None
    candidates = set(shas)
    if len(candidates) == 1:
        return shas[0], None
    # Newest commit = first one met walking the file's history from HEAD. Commit timestamps alone can tie
    # (same second), so topological order decides; timestamps are only a fallback.
    try:
        r = _git(["rev-list", "--topo-order", "HEAD", "--", path.name], path.parent)
        if r.returncode == 0:
            for sha in r.stdout.split():
                if sha in candidates:
                    return sha, None
    except Exception:
        pass
    return max(candidates, key=lambda x: times.get(x, 0)), None

def diff_since(path: Path, sha: str):
    try:
        r = _git(["diff", "-U3", "-w", "--ignore-blank-lines", "--no-color", sha, "--", path.name], path.parent)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None

def has_code_changes(diff: str) -> bool:
    """True if any added or removed line is real code (not blank, not a comment)."""
    for line in diff.splitlines():
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
            body = line[1:]
            if body.strip() and not body.lstrip().startswith("#"):
                return True
    return False

def diff_code_squashed(diff: str) -> str:
    """Code lines of a diff (both sides, markers and comments removed), squashed for evidence matching."""
    out = []
    for line in diff.splitlines():
        if line.startswith(("diff ", "index ", "--- ", "+++ ", "@@", "\\ No newline")):
            continue
        body = line[1:] if line[:1] in ("+", "-", " ") else line
        if body.lstrip().startswith("#"):
            continue
        out.append(body)
    return _squash("\n".join(out))

def query_header_updates(file_name: str, bullets: list[str], diff: str, model_id: str):
    numbered = "\n".join(f"{i}. {b}" for i, b in enumerate(bullets, 1))
    prompt = (
        f"You maintain the 'Features & Safeguards' header of the script '{file_name}'. The header currently lists:\n"
        f"{numbered}\n\n"
        "Below is the diff of the script since that header was last edited. The diff is all you can see of the code.\n"
        "Decide which bullets the diff makes wrong or incomplete.\n"
        "STRICT RULES:\n"
        "1. Propose a change ONLY when the diff directly contradicts a bullet, removes the behavior it describes, or adds a clearly notable behavior no bullet covers.\n"
        "2. Never reword a bullet the diff does not affect, and never make cosmetic edits. If the header is still accurate, return [].\n"
        "3. Match the existing style: active, precise, under 20 words, no environment variables, no obvious mechanics.\n"
        "4. Every change needs 'evidence': a JSON list of 1-3 short snippets (each under 100 chars) copied EXACTLY from the diff's code lines, "
        "without the leading + or - and with double quotes and backslashes omitted. For a removal, quote the removed code.\n"
        "5. Return ONLY a raw JSON array, no markdown. Each item is one of:\n"
        '   {"op": "edit", "n": 2, "text": "new bullet text", "evidence": ["..."]}\n'
        '   {"op": "remove", "n": 3, "evidence": ["..."]}\n'
        '   {"op": "add", "text": "new bullet text", "evidence": ["..."]}\n\n'
        f"DIFF:\n{diff}"
    )
    return groq_json_list(prompt, model_id, f"{file_name} (update)", none_on_fail=True)

def apply_ops(bullets: list[str], ops: list[dict], diff_sq: str):
    """Apply only the ops whose evidence appears in the diff. Returns (new_bullets, applied_descriptions, dropped_ops)."""
    edits, removes, adds, applied, dropped = {}, set(), [], [], []
    for op in ops:
        kind = str(op.get("op", "")).lower()
        text = str(op.get("text", "")).strip()
        try:
            n = int(op.get("n")) if op.get("n") is not None else None
        except (TypeError, ValueError):
            n = None
        in_range = n is not None and 1 <= n <= len(bullets)
        shape_ok = (kind == "add" and bool(text)) or (kind == "edit" and bool(text) and in_range) or (kind == "remove" and in_range)
        if not (shape_ok and evidence_ok(op.get("evidence", ""), diff_sq)):
            dropped.append(op)
            continue
        if kind == "add":
            adds.append(text)
            applied.append(f"add: {text}")
        elif n in edits or n in removes:
            continue
        elif kind == "edit":
            edits[n] = text
            applied.append(f"edit #{n}: {text}")
        else:
            removes.add(n)
            applied.append(f"remove #{n}: {bullets[n - 1]}")
    new = [edits.get(i, b) for i, b in enumerate(bullets, 1) if i not in removes]
    return new + adds, applied, dropped

def update_script(p: Path, content: str, block, model_id: str, dry_run: bool, assume_yes: bool) -> bool:
    """Update mode for a script that already has a header. Returns True if the header was written."""
    name = display_name(p)
    print(f"  → Checking for drift: {name}")
    lines = content.splitlines()
    start, end = block
    bullets = parse_bullets(lines, start, end)
    if not bullets:
        print("    ⚠ Could not read the existing bullets; use --force to regenerate the header.", file=sys.stderr)
        return False
    base, err = header_base_commit(p, start + 2, end)
    if base is None:
        print(f"    ⚠ No usable git history for this header ({err}). Use --force to regenerate it.", file=sys.stderr)
        return False
    if base == UNCOMMITTED:
        print("    ✓ Header has uncommitted edits; assuming it is current. Commit it and drift is tracked from that commit.")
        return False
    diff = diff_since(p, base)
    if diff is None:
        print("    ⚠ Could not diff against the header's commit; use --force to regenerate.", file=sys.stderr)
        return False
    if not diff.strip():
        print(f"    ✓ Header up to date (no code changes since {base[:8]})")
        return False
    if not has_code_changes(diff):
        print(f"    ✓ Header up to date (only comments or blank lines changed since {base[:8]})")
        return False
    if len(diff) > DIFF_CHAR_LIMIT:
        print(f"    ⚠ {len(diff)} chars of changes since {base[:8]}: too large for update mode. Edit the header by hand, or use --force.")
        return False
    changed = sum(1 for ln in diff.splitlines() if ln[:1] in ("+", "-") and not ln.startswith(("+++", "---")))
    print(f"    ↻ {changed} changed line(s) since {base[:8]}; asking the model what the header is missing...")
    ops = query_header_updates(p.name, bullets, diff, model_id)
    if ops is None:
        print("    ⚠ Model reply unusable; header left as is.", file=sys.stderr)
        return False
    new_bullets, applied, dropped = apply_ops(bullets, ops, diff_code_squashed(diff))
    for op in dropped:
        print(f"    ✗ Dropped (unverified or malformed): {json.dumps(op, ensure_ascii=False)[:260]}")
    if not applied:
        print("    ✓ Header still looks accurate.")
        return False
    if len(new_bullets) < MIN_VERIFIED_BULLETS:
        print("    ⚠ Result would leave fewer than 2 bullets; not applying.", file=sys.stderr)
        return False
    for a in applied:
        print(f"    • {a}")
    inject_capabilities(p, new_bullets, dry_run=True)
    if dry_run:
        return False
    if not assume_yes:
        if not sys.stdin.isatty():
            print("    (non-interactive run: not written. Re-run with --yes to apply.)")
            return False
        if input("    Apply this change? [y/N] ").strip().lower() != "y":
            print("    Skipped.")
            return False
    if inject_capabilities(p, new_bullets):
        print("    ✓ Header updated")
        return True
    return False

def build_injected(content: str, bullets: list[str]) -> str | None:
    if not bullets:
        return None
    lines = content.splitlines(keepends=True)

    # Restrict header inspection strictly to the first 40 lines of the file
    header_limit = min(40, len(lines))
    header_chunk = "".join(lines[:header_limit])
    body_chunk = "".join(lines[header_limit:])

    formatted_lines = ["# @CAPABILITIES:"]
    for b in bullets:
        formatted_lines.append(f"#   • {b}")
    block_str = "\n".join(formatted_lines) + "\n"

    if "# @CAPABILITIES:" in header_chunk:
        new_header = re.sub(
            r'# @CAPABILITIES:.*?(?=\n# @|\n# ===|\n[^#]|\Z)',
            lambda _m: block_str.strip(),
            header_chunk,
            flags=re.DOTALL
        )
    else:
        pattern = r'(# @(?:CRON|USES_ENV|FREQUENCY|DESCRIPTION):.*?\n)'
        matches = list(re.finditer(pattern, header_chunk))
        if not matches:
            return None
        idx = matches[-1].end()
        new_header = header_chunk[:idx] + block_str + header_chunk[idx:]

    return new_header + body_chunk

def inject_capabilities(path: Path, bullets: list[str], dry_run: bool = False) -> bool:
    old = path.read_text(encoding="utf-8")
    new = build_injected(old, bullets)
    if new is None:
        return False
    if dry_run:
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), "current", "proposed", lineterm="", n=0)
        print("\n".join("    " + d for d in diff) or "    (no change)")
        return True
    path.write_text(new, encoding="utf-8")
    return True

def main():
    force = "--force" in sys.argv
    dry_run = "--dry-run" in sys.argv
    review = "--no-review" not in sys.argv
    allow_partial = "--partial" in sys.argv
    assume_yes = "--yes" in sys.argv

    if "--print-prompt" in sys.argv:
        paths = [a for a in sys.argv[1:] if not a.startswith("--")]
        if len(paths) != 1 or not Path(paths[0]).is_file():
            print("Usage: doc-injector.py --print-prompt <script>", file=sys.stderr)
            sys.exit(2)
        target = Path(paths[0]).resolve()
        text = target.read_text(encoding="utf-8", errors="ignore")
        body = strip_capabilities(text)
        lead = leading_comment_block(body)
        code = code_only("\n".join(body.splitlines()[len(lead.splitlines()):]))
        prompt = build_generation_prompt(target.name, f"{lead}\n{code}" if lead else code, None, paste=True)
        print(prompt)
        print(f"# prompt: {len(prompt)} chars (~{len(prompt) // 4} tokens), nothing truncated", file=sys.stderr)
        sys.exit(0)

    if not GROQ_KEY:
        print("⏭️  GROQ_API_KEY missing in .env. Skipping capability generation.")
        sys.exit(0)

    active_model = discover_best_model(GROQ_KEY)
    print(f"🤖 Connected to Groq using live model: {active_model}")

    scripts_to_scan = []
    targets = [SCRIPT_DIR]
    if len([a for a in sys.argv[1:] if not a.startswith("--")]) > 0:
        targets = [Path(a).resolve() for a in sys.argv[1:] if not a.startswith("--")]

    for target in targets:
        if target.is_file():
            scripts_to_scan.append(target)
        else:
            for root, dirs, files in os.walk(target):
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                for file in files:
                    p = Path(root) / file
                    if p.name in IGNORE_FILES: continue
                    if p.suffix in {".sh", ".py"}:
                        scripts_to_scan.append(p)

    updated_count = 0
    print(f"🔍 Checking capabilities for {len(scripts_to_scan)} script(s)...")

    for p in sorted(scripts_to_scan):
        try: content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception: continue

        if not (content.startswith("#!/bin") or content.startswith("#!/usr")):
            continue

        block = find_block(content.splitlines())
        has_block = block is not None

        if has_block and not force:
            if update_script(p, content, block, active_model, dry_run, assume_yes):
                updated_count += 1
            continue

        name = display_name(p)
        print(f"  → Analyzing: {name}")
        body = strip_capabilities(content)
        lead = leading_comment_block(body)
        rest = "\n".join(body.splitlines()[len(lead.splitlines()):])
        code = code_only(rest)
        prompt_src = f"{lead}\n{code}" if lead else code
        partial = len(prompt_src) > PROMPT_CHAR_LIMIT
        if partial:
            pct = PROMPT_CHAR_LIMIT * 100 // len(prompt_src)
            print(f"    ⚠ Large script: the model sees about {pct}% of it. Review is skipped, and nothing is written without --partial.")
        items = query_groq_capabilities(p.name, prompt_src, active_model)
        # Evidence must come from real code. With a partial view the model can only know the unseen part
        # from the top comment block, so that block counts as evidence too.
        evidence_src = _squash(f"{lead}\n{code}" if partial else code)
        bullets, dropped = verify_bullets(items, evidence_src)
        for claim, ev in dropped:
            print(f"    ✗ Dropped: {claim}")
            print(f"        evidence given: {json.dumps(ev, ensure_ascii=False)[:300]}")
        if dropped and len(dropped) >= len(bullets):
            print(f"    ⚠ Low confidence: {len(dropped)} dropped vs {len(bullets)} kept; review the diff carefully", file=sys.stderr)
        if review and bullets and not partial:
            rejected = review_claims(p.name, code, bullets, active_model)
            for claim, reason in rejected:
                print(f"    ✗ Rejected by review: {claim}")
                print(f"        reason: {reason}")
            bullets = [b for b in bullets if b not in {c for c, _ in rejected}]
        if len(bullets) < MIN_VERIFIED_BULLETS:
            print(f"    ⚠ Only {len(bullets)} verified bullet(s); keeping existing header for {name}", file=sys.stderr)
            continue
        if partial and not allow_partial and not dry_run:
            inject_capabilities(p, bullets, dry_run=True)
            print("    ⚠ Partial view: NOT written. Re-run with --partial to write it anyway.")
            continue
        if inject_capabilities(p, bullets, dry_run=dry_run):
            if dry_run:
                if partial:
                    print("    ⚠ Partial view: this proposal would not be written without --partial.")
                continue
            updated_count += 1
            print(f"    ✓ Injected @CAPABILITIES ({len(bullets)} verified items)")
            time.sleep(2)

    print(f"✅ Capabilities check complete. ({updated_count} file(s) updated)")

if __name__ == "__main__":
    main()
