#!/usr/bin/env python3
# @DESCRIPTION: Terminal AI assistant powered by Google Gemini supporting massive piped logs, script reviews, multi-turn chat, and auto-failover streaming
# @FREQUENCY: On Demand
# @USES_ENV: GEMINI_API_KEY, GEMINI_MODEL
# @CAPABILITIES:
#   • Streams AI responses up to 200k characters, preserving full log context for deep diagnostics.
#   • Auto‑fails over to lighter models on 503 spikes, keeping analysis uninterrupted.
#   • Redacts API keys, tokens, passwords, and certificates from piped logs, safeguarding secrets.
#   • Gracefully handles binary or corrupted streams, preventing crashes from non‑UTF8 input.
#   • Supports multi‑turn chat, script reviews, and log diagnostics via pipe or interactive mode, accelerating troubleshooting.

import json
import os
import re
import sys
import time
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

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest").strip()
if not DEFAULT_MODEL:
    DEFAULT_MODEL = "gemini-flash-lite-latest"

# High-concurrency fallback cascade if the primary model hits a Google 503 capacity spike
MODEL_CASCADE = [
    DEFAULT_MODEL,
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite"
]

# ── Help / TLDR Manual ────────────────────────────────────────────────────────
def print_help():
    help_text = """\033[1;36moracle\033[0m — Terminal AI Assistant & Diagnostics Engine (Powered by Gemini)

\033[1;33mUSAGE:\033[0m
  oracle [FLAGS] [QUERY]
  <command> | oracle [FLAGS] [QUERY]

\033[1;33mMODES:\033[0m
  \033[1mPiped Input (Stdin)\033[0m   Pipe command output, logs, or scripts directly to oracle
  \033[1mOne-Shot Prompt\033[0m       oracle "your question" (instant answer streamed in terminal)
  \033[1mInteractive Chat\033[0m      oracle (with no arguments launches a multi-turn terminal chat)

\033[1;33mFLAGS:\033[0m
  \033[1;32m-h, --help\033[0m            Show this documentation and exit
  \033[1;32m-avail, --models\033[0m      List available models on your account & show active model
  \033[1;32m-m <model>\033[0m            Override model (e.g. -m gemini-flash-lite-latest)

\033[1;33mEXAMPLES:\033[0m
  \033[2m# Diagnose full Docker logs without truncation:\033[0m
  docker logs pocket-id 2>&1 | oracle "diagnose any errors in these logs"

  \033[2m# Review full scripts from top to bottom:\033[0m
  cat local-opt-backup.sh | oracle "analyze this script and suggest edge-case improvements"

  \033[2m# Audit pre-commit diffs for bugs:\033[0m
  git diff | oracle "check these changes before I commit"

  \033[2m# One-shot CLI cheat-sheet query:\033[0m
  oracle "write a sed command to replace a line starting with FOO"

\033[1;33mBUILT-IN SAFEGUARDS:\033[0m
  • \033[1mAuto-Failover:\033[0m Automatically switches to high-concurrency lite models if Google hits 503 capacity spikes.
  • \033[1mSecret Masking:\033[0m Automatically redacts API keys, tokens, and passwords in piped text.
  • \033[1mHigh Context Allowance:\033[0m Handles up to 200,000 characters without trimming.
  • \033[1mBinary Safe:\033[0m Gracefully handles non-UTF8 or corrupted log output cleanly.
"""
    print(help_text)

# ── Model Discovery & Inspection ──────────────────────────────────────────────
def get_available_models(token: str) -> list[str]:
    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={token}",
            timeout=10
        )
        if r.status_code == 200:
            models = []
            for m in r.json().get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    models.append(m["name"].replace("models/", ""))
            return sorted(models)
    except Exception as e:
        print(f"Error fetching models: {e}", file=sys.stderr)
    return []

# ── Sanitizer (Defense-in-Depth) ──────────────────────────────────────────────
def clean_piped_text(text: str) -> str:
    """Strips terminal color codes and masks secrets."""
    cleaned = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

    patterns = [
        # 1. Platform-Specific High-Entropy Prefixes (Zero false positive risk)
        (r'AIzaSy[a-zA-Z0-9_\-]{33}', '[REDACTED_GEMINI_KEY]'),
        (r'gsk_[a-zA-Z0-9]{40,}', '[REDACTED_GROQ_KEY]'),
        (r'ghp_[a-zA-Z0-9]{36}', '[REDACTED_GITHUB_TOKEN]'),
        (r'github_pat_[a-zA-Z0-9_]{82}', '[REDACTED_GITHUB_PAT]'),
        (r'sk-[a-zA-Z0-9]{48}', '[REDACTED_OPENAI_KEY]'),

        # 2. Private Keys & Certificates
        (r'-----BEGIN [A-Z ]+PRIVATE KEY----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----', '[REDACTED_PRIVATE_KEY]'),
        (r'-----BEGIN CERTIFICATE----[\s\S]*?-----END CERTIFICATE-----', '[REDACTED_CERTIFICATE]'),

        # 3. Network & Auth Headers
        (r'(?i)bearer\s+[a-zA-Z0-9_\-\.]+', 'Bearer [REDACTED]'),
        (r'(?i)authorization:\s*[^\n]+', 'Authorization: [REDACTED]'),
        (r'(?i)(postgres|mysql|mongodb|redis|amqp|smtp)://([^:]+):([^@]+)@', r'\1://\2:[REDACTED]@'),

        # 4. Comprehensive Assignment Secrets (Catches password, passwd, secret, key, token, auth, api, credential, jwt, salt, hash, salt, etc.)
        (r'(?i)\b(password|passwd|secret|api[\s_\-]?key|token|auth|credential|jwt|salt|hash|client[\s_\-]?secret)\s*[:=]\s*["\']?([^"\'\s,;]+)["\']?', r'\1: [REDACTED]'),
    ]
    for pattern, repl in patterns:
        cleaned = re.sub(pattern, repl, cleaned)
    return cleaned

# ── Streaming Query Engine with Auto-Failover ────────────────────────────────
def stream_gemini(messages: list[dict], requested_model: str):
    url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    headers = {
        "Authorization": f"Bearer {GEMINI_KEY}",
        "Content-Type": "application/json"
    }

    models_to_try = [requested_model]
    for m in MODEL_CASCADE:
        if m not in models_to_try:
            models_to_try.append(m)

    for idx, model in enumerate(models_to_try):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True
        }

        try:
            r = requests.post(url, headers=headers, json=payload, stream=True, timeout=60)

            if r.status_code in (503, 500):
                next_model = models_to_try[idx + 1] if idx + 1 < len(models_to_try) else None
                if next_model:
                    print(f"⏳ \033[1;33m{model} busy (503). Auto-switching to {next_model}...\033[0m\n", file=sys.stderr)
                    time.sleep(1.0)
                    continue
                else:
                    print(f"\n❌ Gemini API Error ({r.status_code}): {r.text}", file=sys.stderr)
                    return ""

            if r.status_code != 200:
                print(f"\n❌ Gemini API Error ({r.status_code}): {r.text}", file=sys.stderr)
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
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        full_reply.append(delta)
                    except Exception:
                        pass
            print()
            return "".join(full_reply)

        except KeyboardInterrupt:
            print("\n\033[2m[Interrupted by user]\033[0m")
            return ""
        except Exception as e:
            if idx == len(models_to_try) - 1:
                print(f"\n❌ Connection Error: {e}", file=sys.stderr)
                return ""
            time.sleep(1.0)

    return ""

# ── Interactive Chat Mode ─────────────────────────────────────────────────────
def interactive_chat(model: str):
    print(f"💬 Starting interactive session with \033[1;36m{model}\033[0m")
    print("   Type 'exit', 'quit', or Ctrl+C to stop.\n")

    system_prompt = {
        "role": "system",
        "content": (
            "You are a versatile, highly skilled technical assistant and systems specialist. "
            "While your core strengths are Linux systems, DevOps, and infrastructure, you can assist with "
            "software development in any language, networking, databases, general technology, and problem solving. "
            "Be direct, technically precise, and avoid conversational fluff.\n\n"
            "TERMINAL FORMATTING RULES:\n"
            "- Output plain text strictly formatted for a Linux CLI terminal.\n"
            "- Do NOT use markdown headers (no '#', '##', or '###'). Write section headings in clean UPPERCASE followed by a colon.\n"
            "- Do NOT use markdown bold/italic asterisks (no '**' or '*'). Write terms cleanly without asterisks.\n"
            "- Use clean unicode bullets ('•') for lists."
        )
    }
    history = [system_prompt]

    while True:
        try:
            prompt = input("\033[1;32moracle>\033[0m ").strip()
            if not prompt: continue
            if prompt.lower() in {"exit", "quit", "q"}: break

            history.append({"role": "user", "content": prompt})
            print("\033[1;34m[AI]\033[0m ", end="", flush=True)
            reply = stream_gemini(history, model)
            if reply:
                history.append({"role": "assistant", "content": reply})
            print()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

# ── Main Entrypoint ───────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    # Fast Help Flag Check
    if any(arg in {"-h", "--help", "help"} for arg in args):
        print_help()
        sys.exit(0)

    if not GEMINI_KEY:
        print("❌ Error: GEMINI_API_KEY missing in /opt/rabbit-hole/.env", file=sys.stderr)
        print("   Get your free key at https://aistudio.google.com and add it to .env", file=sys.stderr)
        sys.exit(1)

    # Check for -avail or --models flag
    if any(arg in {"-avail", "--avail", "--models", "-m-list"} for arg in args):
        models = get_available_models(GEMINI_KEY)
        print("\n📋 Available Gemini Models on your account:")
        for m in models:
            tag = " \033[1;32m(DEFAULT)\033[0m" if m == DEFAULT_MODEL else ""
            print(f"  • {m}{tag}")
        print()
        sys.exit(0)

    # Check for model override: -m <model_name>
    model = DEFAULT_MODEL
    if "-m" in args:
        idx = args.index("-m")
        if idx + 1 < len(args):
            model = args[idx + 1]
            del args[idx:idx + 2]
    elif "--model" in args:
        idx = args.index("--model")
        if idx + 1 < len(args):
            model = args[idx + 1]
            del args[idx:idx + 2]

    # --- HARDENED SAFEGUARD ---
    if not model or not model.strip():
        model = "gemini-flash-lite-latest"

    # Check for piped input (stdin) with Binary/Corrupt Safety
    piped_context = ""
    was_trimmed = False
    is_piped = not sys.stdin.isatty()

    if is_piped:
        try:
            raw_bytes = sys.stdin.buffer.read()
            raw_input = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            raw_input = ""

        if raw_input.strip():
            MAX_SAFE_CHARS = 200000

            if len(raw_input) > MAX_SAFE_CHARS:
                lines = raw_input.splitlines()
                trimmed = []
                char_tally = 0
                for line in reversed(lines):
                    if char_tally + len(line) > MAX_SAFE_CHARS:
                        break
                    trimmed.append(line)
                    char_tally += len(line) + 1
                trimmed.reverse()
                piped_context = clean_piped_text("\n".join(trimmed))
                was_trimmed = True
            else:
                piped_context = clean_piped_text(raw_input)

    user_query = " ".join(args).strip()

    # Guard: If stdin was piped but completely empty and no query provided
    if is_piped and not piped_context and not user_query:
        print("ℹ️  Piped input was empty. Nothing to analyze.")
        sys.exit(0)

    # Case 1: Piped context + query
    if piped_context:
        instruction = user_query if user_query else "Analyze this output/code, explain any issues, and suggest improvements or fixes:"
        full_prompt = (
            f"{instruction}\n\n"
            f"```text\n{piped_context}\n```"
        )
        messages = [
            {"role": "system", "content": (
                "You are a strict, direct Linux Systems and DevOps command-line assistant. "
                "Analyze the provided terminal output, logs, or code.\n"
                "RULES:\n"
                "- Answer the user's specific query immediately and directly. Do NOT start with conversational fillers like 'Everything is running normally', 'The provided input is...', or 'Here is the analysis'.\n"
                "- If analyzing code/scripts, give straight answers, explanations, or code improvements.\n"
                "- If analyzing logs with no errors, state that plainly in one short sentence without a meta-commentary preamble.\n"
                "- Avoid markdown tables unless comparing multiple distinct entities.\n\n"
                "TERMINAL FORMATTING RULES:\n"
                "- Output plain text strictly formatted for a Linux CLI terminal.\n"
                "- Do NOT use markdown headers (no '#', '##', or '###'). Write section headings in clean UPPERCASE followed by a colon.\n"
                "- Do NOT use markdown bold/italic asterisks (no '**' or '*'). Write terms cleanly without asterisks.\n"
                "- Use clean unicode bullets ('•') for lists."
            )},
            {"role": "user", "content": full_prompt}
        ]
        trim_notice = " \033[1;33m[Trimmed: tail kept to protect size]\033[0m" if was_trimmed else " \033[1;32m[Full Context]\033[0m"
        print(f"\033[2m[Model: {model} | Lines: {len(piped_context.splitlines())}{trim_notice}]\033[0m\n")
        stream_gemini(messages, model)
        sys.exit(0)

    # Case 2: One-shot CLI question
    if user_query:
        messages = [
            {"role": "system", "content": (
                "You are a strict, direct Linux Systems and DevOps command-line assistant. "
                "Answer the user's question directly and concisely. Never use introductory filler or conversational preamble. Output clean terminal text only."
            )},
            {"role": "user", "content": user_query}
        ]
        print(f"\033[2m[Model: {model}]\033[0m\n")
        stream_gemini(messages, model)
        sys.exit(0)

    # Case 3: Interactive Chat Mode (no query, no pipe)
    interactive_chat(model)

if __name__ == "__main__":
    main()
