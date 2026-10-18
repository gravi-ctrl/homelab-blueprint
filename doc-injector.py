#!/usr/bin/env python3
# @DESCRIPTION: Analyzes homelab scripts and automatically injects an AI-generated capabilities header block using Groq
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
# @USES_ENV: GROQ_API_KEY
# @CRON: user

import os
import sys
import re
import json
import hashlib
import requests
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
CACHE_FILE = SCRIPT_DIR / ".doc_hash_cache.json"

# Exclusions
IGNORE_DIRS = {".git", "venv", "node_modules", "_archive", "run_once"}
IGNORE_FILES = {Path(__file__).name}  # Don't mutate self

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

def get_file_hash(content: str) -> str:
    # Hash code without the existing @CAPABILITIES block to detect real functional edits
    stripped = re.sub(r'# @CAPABILITIES:.*?(?=\n# [A-Z_]+:|\n# ===|\n[^#]|\Z)', '', content, flags=re.DOTALL)
    return hashlib.sha256(stripped.strip().encode("utf-8")).hexdigest()

def query_groq_capabilities(file_name: str, code: str) -> list[str]:
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
        f"CODE:\n{code[:8000]}"
    )

    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2
            },
            timeout=20
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        bullets = json.loads(raw)
        return [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
    except Exception as e:
        print(f"    ⚠ Groq API Error on {file_name}: {e}", file=sys.stderr)
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
        bullets = query_groq_capabilities(p.name, content)
        if bullets:
            if inject_capabilities(p, bullets):
                # Update hash on new content
                new_content = p.read_text(encoding="utf-8")
                cache[str(p)] = get_file_hash(new_content)
                updated_count += 1
                print(f"    ✓ Updated @CAPABILITIES ({len(bullets)} items)")

    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"✅ Capabilities check complete. ({updated_count} file(s) updated)")

if __name__ == "__main__":
    main()
