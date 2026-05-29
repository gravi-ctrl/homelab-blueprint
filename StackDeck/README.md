# StackDeck 🐳

A lightweight, automated dashboard generator for your self-hosted Docker stacks. It crawls your GitHub repository for compose files, queries Nginx Proxy Manager (NPM) for active domain mappings, and produces a single, beautiful HTML dashboard.

## Features
- **Automatic Stack Detection:** Dynamically crawls and parses your Compose files.
- **Reverse Proxy Mapping:** Matches containers to live NPM domain URLs.
- **Environment Template Parsing:** Documents expected variables from `.env.example` files (auto-masking secrets).
- **Interactive UI:** Searchable, responsive dashboard with dark mode and active filtering.

---

## Local Setup

1. **Clone the repository and install requirements:**
   ```bash
   pip install pyyaml requests python-dotenv
   ```

2. **Configure environment variables:**
   Create a `.env` file in the root directory:
   ```ini
   GITHUB_REPO="https://github.com/your-username/your-repo"
   GITHUB_BRANCH="main"
   EXCLUDE_DIRS="_archive"
   OUTPUT_FILE="index.html"

   # Optional: Nginx Proxy Manager Credentials
   NPM_URL="http://192.168.1.109:81"
   NPM_EMAIL="admin@example.com"
   NPM_PASSWORD="your-secure-password"
   ```

3. **Generate your dashboard:**
   ```bash
   python3 docker_dash.py
   ```
