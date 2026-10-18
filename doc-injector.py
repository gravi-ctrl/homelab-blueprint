#!/usr/bin/env python3
# @DESCRIPTION: Analyzes homelab scripts and automatically injects an AI-generated capabilities header block using Groq
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
# @USES_ENV: GROQ_API_KEY, GROQ_MODEL
# @CRON: user

import os
import sys
import re
import json
import hashlib
import time
import requests
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
CACHE_FILE = SCRIPT_DIR / ".doc_hash_cache.json"

# Exclusions — never mutate self or the parent runner script while it's executing!
IGNORE_DIRS = {".git", "venv", "node_modules", "_archive", "run_once"}
IGNORE_FILES = {Path(__file__).name, "backup-scripts-git.sh"}

# Priority order for preferred text models
PREFERRED_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "allam-2-7b"
]

# Substrings identifying non-general-chat models to ignore
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
    """Dynamically queries Groq to pick an active text model, preventing deprecation breakages."""
    # 1. Check if user specified a manual override in .env
    override = os.environ.get("GROQ_MODEL", "").strip()
    if override:
        return override

    try:
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        if r.status_code == 200:
            available = [m["id"] for m in r.json().get("data", [])]
            
            # Match against preferred priority list
            for pref in PREFERRED_MODELS:
                if pref in available:
                    return pref
            
            # Dynamic fallback: pick first text model that isn't audio/guardrail
            chat_models = [
                m for m in available
                if not any(pattern in m.lower() for pattern in EXCLUDE_PATTERNS)
            ]
            if chat_models:
                return chat_models[0]
    except Exception as e:
        print(f"    ⚠ Auto-discovery error ({e}), using default fallback.", file=sys.stderr)

    # Static default if API discovery fails
    return "openai/gpt-oss-20b"

def get_file_hash(content: str) -> str:
    # Hash code without the existing @CAPABILITIES block to detect real functional edits
    stripped = re.sub(r'# @CAPABILITIES:.*?(?=\n# [A-Z_]+:|\n# ===|\n[^#]|\Z)', '', content, flags=re.DOTALL)
    return hashlib.sha256(stripped.strip().encode("utf-8")).hexdigest()

import time

def query_groq_capabilities(file_name: str, code: str, model_id: str, max_retries: int = 4) -> list[str]:
    url = "https://api.groq.com/openai/v1/chat/completions"
    prompt = (
        f"You are a principal systems and DevOps documentation engine. Analyze the following Linux script ('{file_name}').\n"
        "Extract between 3 to 5 concise, technical bullet points highlighting its core capabilities, safeguards, "
        "and architectural mechanisms (e.g. signal traps, locking, encryption, health checks, rate limits).\n"
        "RULES:\n"
        "- Return ONLY a raw JSON array of strings.\n"
        "- Do NOT include markdown backticks (no ```json).\n"
        "- Each bullet must be an active, precise technical statement under 20 words.\n"
        "- No introductory or conversational filler.\n\n"
        f"CODE:\n{code[:7000]}"
    )

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=25)

            # Handle free-tier rate limits (429) automatically
            if r.status_code == 429:
                wait_time = 14.0  # Safe default
                m = re.search(r'try again in (\d+(?:\.\d+)?)s', r.text)
                if m:
                    wait_time = float(m.group(1)) + 1.5
                elif r.headers.get("retry-after"):
                    try: wait_time = float(r.headers.get("retry-after")) + 1.0
                    except ValueError: pass

                print(f"    ⏳ Rate limit (TPM) reached. Waiting {wait_time:.1f}s before retry (attempt {attempt}/{max_retries})...")
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
            time.sleep(3)

    return []

def inject_capabilities(path: Path, bullets: list[str]) -> bool:
    if not bullets: return False
    content = path.read_text(encoding="utf-8")

    formatted_lines = ["# @CAPABILITIES:"]
    for b in bullets:
        formatted_lines.append(f"#   • {b}")
    block_str = "\n".join(formatted_lines) + "\n"

    # Replace existing block or insert after @CRON / @USES_ENV / @FREQUENCY
    if "# @CAPABILITIES:" in content:
        new_content = re.sub(
            r'# @CAPABILITIES:.*?(?=\n# @|\n# ===|\n[^#]|\Z)',
            block_str.strip(),
            content,
            flags=re.DOTALL
        )
    else:
        pattern = r'(# @(?:CRON|USES_ENV|FREQUENCY|DESCRIPTION):.*?\n)'
        matches = list(re.finditer(pattern, content))
        if matches:
            last_match = matches[-1]
            idx = last_match.end()
            new_content = content[:idx] + block_str + content[idx:]
        else:
            return False

    path.write_text(new_content, encoding="utf-8")
    return True

def main():
    force = "--force" in sys.argv
    if not GROQ_KEY:
        print("⏭️  GROQ_API_KEY missing in .env. Skipping capability generation.")
        sys.exit(0)

    # 1. Discover or resolve active text model
    active_model = discover_best_model(GROQ_KEY)
    print(f"🤖 Connected to Groq using live model: {active_model}")

    cache = {}
    if CACHE_FILE.exists():
        try: cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception: cache = {}

    scripts_to_scan = []
    if len([a for a in sys.argv[1:] if not a.startswith("--")]) > 0:
        targets = [Path(a).resolve() for a in sys.argv[1:] if not a.startswith("--")]
    else:
        targets = [SCRIPT_DIR]

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
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception: continue

        # Verify it has shebang
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
                time.sleep(2)  # Pace batch runs

    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"✅ Capabilities check complete. ({updated_count} file(s) updated)")

if __name__ == "__main__":
    main()
