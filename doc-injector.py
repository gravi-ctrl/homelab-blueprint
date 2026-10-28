#!/usr/bin/env python3
# @DESCRIPTION: Analyzes ~/scripts and automatically injects an AI-generated capabilities header block using Groq
# @FREQUENCY: On Demand
# @USES_ENV: GROQ_API_KEY, GROQ_MODEL
# @CAPABILITIES:
#   • Auto‑generates concise feature lists for scripts, enabling instant dashboard insights without manual documentation.
#   • Caches script hashes to skip redundant AI calls, cutting bandwidth and cost.
#   • Dynamically selects the most compatible Groq model, ensuring optimal performance across environments.
#   • Parses exact rate‑limit wait times and falls back to exponential backoff, guaranteeing reliable retries.
#   • Limits header injection to the first 40 lines, preserving existing metadata and preventing accidental overwrites.

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
CACHE_FILE = SCRIPT_DIR / ".doc_hash_cache.json"

# Exclusions
IGNORE_DIRS = {".git", "venv", "node_modules", "_archive"}
IGNORE_FILES = {Path(__file__).name}

PREFERRED_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "allam-2-7b"
]
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
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if r.status_code == 200:
            available = [m["id"] for m in r.json().get("data", [])]
            for pref in PREFERRED_MODELS:
                if pref in available: return pref
            chat_models = [m for m in available if not any(p in m.lower() for p in EXCLUDE_PATTERNS)]
            if chat_models: return chat_models[0]
    except Exception: pass
    return "openai/gpt-oss-20b"

def get_file_hash(content: str) -> str:
    stripped = re.sub(r'# @CAPABILITIES:.*?(?=\n# [A-Z_]+:|\n# ===|\n[^#]|\Z)', '', content, flags=re.DOTALL)
    return hashlib.sha256(stripped.strip().encode("utf-8")).hexdigest()

def query_groq_capabilities(file_name: str, code: str, model_id: str, max_retries: int = 6) -> list[str]:
    url = "https://api.groq.com/openai/v1/chat/completions"
    prompt = (
        f"You are a DevOps engineer writing a 'Features & Safeguards' list for an infrastructure dashboard. Analyze this script ('{file_name}').\n"
        "Extract between 2 to 8 punchy bullet points highlighting its practical utility, unique features, and operational safeguards.\n"
        "STRICT RULES:\n"
        "1. Scale the number of bullets to the script's complexity (e.g., 2 bullets for simple scripts, up to 8 for massive automation engines).\n"
        "2. Focus on WHAT it achieves and WHY it's useful (e.g., 'Mute mode prevents alert fatigue', 'Self-heals via exponential backoff').\n"
        "3. DO NOT state obvious mechanics (e.g., 'Executes commands' or 'Uses Python').\n"
        "4. DO NOT mention or list environment variables (this is handled elsewhere).\n"
        "5. Return ONLY a raw JSON array of strings.\n"
        "6. Do NOT include markdown backticks (no ```json).\n"
        "7. Each bullet must be an active, precise statement under 20 words.\n\n"
        f"CODE:\n{code[:7000]}"
    )
    payload = {"model": model_id, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=25)
            
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
                print(f"    ⚠ Groq API Error ({r.status_code}) on {file_name}: {r.text}", file=sys.stderr)
                return []
            
            raw = r.json()["choices"][0]["message"]["content"].strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            bullets = json.loads(raw)
            return [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
            
        except Exception as e:
            if attempt == max_retries:
                print(f"    ⚠ Request failed on {file_name}: {e}", file=sys.stderr)
                return []
            time.sleep(5)
            
    return []

def inject_capabilities(path: Path, bullets: list[str]) -> bool:
    if not bullets: return False
    content = path.read_text(encoding="utf-8")
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
            block_str.strip(),
            header_chunk,
            flags=re.DOTALL
        )
    else:
        pattern = r'(# @(?:CRON|USES_ENV|FREQUENCY|DESCRIPTION):.*?\n)'
        matches = list(re.finditer(pattern, header_chunk))
        if matches:
            idx = matches[-1].end()
            new_header = header_chunk[:idx] + block_str + header_chunk[idx:]
        else:
            return False

    path.write_text(new_header + body_chunk, encoding="utf-8")
    return True

def main():
    force = "--force" in sys.argv
    if not GROQ_KEY:
        print("⏭️  GROQ_API_KEY missing in .env. Skipping capability generation.")
        sys.exit(0)

    active_model = discover_best_model(GROQ_KEY)
    print(f"🤖 Connected to Groq using live model: {active_model}")

    cache = {}
    if CACHE_FILE.exists():
        try: cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception: cache = {}

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

        file_hash = get_file_hash(content)
        has_block = "# @CAPABILITIES:" in content

        if not force and has_block and cache.get(str(p)) == file_hash:
            continue

        print(f"  → Analyzing: {p.relative_to(SCRIPT_DIR)}")
        bullets = query_groq_capabilities(p.name, content, active_model)
        if bullets:
            if inject_capabilities(p, bullets):
                new_content = p.read_text(encoding="utf-8")
                cache[str(p)] = get_file_hash(new_content)
                updated_count += 1
                print(f"    ✓ Injected @CAPABILITIES ({len(bullets)} items)")
                time.sleep(2)

    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"✅ Capabilities check complete. ({updated_count} file(s) updated)")

if __name__ == "__main__":
    main()
