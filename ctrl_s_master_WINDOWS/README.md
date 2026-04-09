# ctrl_s_master
> *Named after the universal shortcut for saving: CTRL + S.*

A robust, automated suite for creating local archives of critical data from services like Bitwarden, and for syncing other key files (like Raindrop.io, Obsidian notes, etc.). The entire process is orchestrated, logged, and sends email notifications upon completion or failure.

---

## Getting Started

Follow these steps to set up the project from scratch on a new machine.

### 1. Install Prerequisites

Make sure you have the following software installed on your system:

*   **Python**: Required for running the core automation scripts. - [Download Python](https://www.python.org/downloads/)
*   **KeePassXC**: Highly recommended for viewing the final, encrypted `.kdbx` password vaults. - [Download KeePassXC](https://keepassxc.org/download/)

### 2. Configure Secrets in the `.env` File

First, copy the `.env.example` file and rename it to `.env`. This file holds all the secrets and paths the automation suite needs to run.

```
⚠️ Security Warning

This script handles extremely sensitive information. 
The .env file should be treated with the utmost care. 
Never share it, and ensure your machine has disk encryption enabled.
```
This file holds all the secrets and paths the automation suite needs to run. Follow the guides below to obtain the necessary credentials.

##### **A. Getting Bitwarden Credentials**

The Bitwarden export process requires an `Access Token` and several `UUIDs` from Secrets Manager. Follow these steps to get them:

1.  **Create a Machine Account & Access Token:** This token allows the script to securely access your other secrets.
    *   Navigate to **Bitwarden Secrets Manager** → **Machine accounts**.
    *   Create a new machine account if you don't have one for this script.
    *   Under **Access tokens**, click **+Create access token** and give it a name (e.g., `vault-exporter`).
    *   **Copy the generated Access Token immediately as you won't see it again.** This is the value for `BW_ACCESS_TOKEN`.

2.  **Get Your Vault's API Key:** You will need a unique API key for each Bitwarden vault you intend to export.
    *   Navigate to your **Bitwarden Web Vault** → **Settings** → **Security** → **Keys**.
    *   Click **View API Key** to get your `client_id` and `client_secret`.

3.  **Create Secrets, Grant Access, and Copy UUIDs:**
    *   In Secrets Manager, create a new **Project** to hold your credentials.
    *   Inside the project, create three new secrets containing your: `client_id`, `client_secret`, and `Master Password`.
    *   In the project’s **People** tab, add your user account and grant it **Read** access. **This is a critical step.**
    *   Click each secret, then **Machine accounts**, and choose the Machine account with the Access token inside.
	*   Copy each secret's **ID (UUID)**. These are the values for the `BW_*_CLIENT_ID_UUID`, `BW_*_CLIENT_SECRET_UUID`, and `BW_*_MASTER_PASSWORD_UUID` variables.

4.  **Find Your Organization ID (If Needed):** This is only required if `BW_*_IS_ORGANIZATION` is set to `true`.
    *   In the Web Vault, open the **Admin Console** and select your Organization.
    *   The Organization ID is in your browser's URL, between `/organizations/` and `/vault`.

##### **B. Getting Raindrop.io API Tokens**

You will need a separate API token for each Raindrop.io account you want to back up.

1.  Log in to your Raindrop.io account.
2.  Go to **Settings** and navigate to the **Integrations** tab.
3.  Under **For Developers** click on **+ Create new app**.
4.  Give the app a name (e.g., "Local Backup Script") and click **Create**.
5.  On the next screen, click the **Create test token** button.
6.  **Copy the generated token.** This is the value for `RAINDROP_PERSONAL_API_TOKEN` or `RAINDROP_WORK_API_TOKEN`. Repeat for your other account if necessary.

##### **C. Getting a Google App Password for Email**

To send email notifications using a Gmail account, you must use an App Password.

1.  Ensure **2-Step Verification** is enabled on your Google Account. App Passwords cannot be created without it.
2.  Go directly to the App Passwords page: [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3.  Type any name you want to identify it with (e.g., Email notifications for my project).
4.  **Copy the 16-character password** shown in the yellow box (without spaces). This is the value for `EMAIL_PASSWORD`.

##### **D. Configuring Other Essential Paths and Passwords**

Finally, fill in the remaining blank variables in the `.env` file. These are crucial for the script to locate files and encrypt your backups.

*   `BITWARDEN_PERSONAL_PASSWORD` / `BITWARDEN_WORK_PASSWORD`: **Choose a new password** for the `encrypted_json` Bitwarden export files. This is *not* your master password; it's a separate password used to secure the backup file itself.
*   `KDBX_PERSONAL_PASSWORD` / `KDBX_WORK_PASSWORD`: **Choose a new password** for the final `.kdbx` KeePass database files. This password will be required to open them in KeePassXC.
*   `FFS_PATH`: Provide the full, absolute path to the FreeFileSync executable on your system (e.g., `C:/Program Files/FreeFileSync/FreeFileSync.exe`).
*   `RAINDROP_BACKUP_DESTINATION`: The full path to the local directory where you want the initial backup for Raindrop to be saved. This is a source folder for your FreeFileSync jobs.
*   `BW_SERIALS_TO_KEEP`: Controls the number of recent Bitwarden backup files (.json) to retain. A value of 7 will keep the seven newest backups and delete any older ones. Set to 0 to disable cleanup.
*   `BACKUPS_TO_KEEP`: Defines the number of recent backup files to keep for Raindrop. A value of 7 will keep the seven newest backup archives and delete any older ones. Set to 0 to disable cleanup.


### 3. Run the One-Time Setup

This script prepares the entire project. It creates the Python virtual environment (`venv`), installs all required packages from `requirements.txt`, and downloads the necessary command-line tools.

**Run this only once** for a new setup. Open Command Prompt and run:

`setup.bat`

---

## Usage and Maintenance

Once the project is set up, these are the commands you will use for daily operation, testing, and upkeep.

### Running the Main Automation

This is the primary command for your scheduled tasks. It runs the full backup and sync process and generates the log and email report.

`run.bat`

For continuous automation, it is highly recommended to use **Windows Task Scheduler** to run this script on a regular basis (e.g., daily or weekly).

### Updating Dependencies

Over time, your dependencies will have new versions. To update everything (Python packages, Bitwarden CLI) to the latest versions, manually run the `update.bat` script.

`update.bat`

**IMPORTANT:** After running an update, you should always run your test suite immediately to ensure the new versions haven't introduced any breaking changes.

### Testing the Suite

This project includes a professional test suite using `pytest` to ensure its core logic is always working correctly. To run the entire test suite, simply execute the launcher script:

`tests.bat`

This will activate the `venv`, run the tests, and give you a clear `PASSED` or `FAILED` status for each core feature.

#### Advanced Usage: Running Specific Tasks & Dry Runs

For debugging or performing a one-off action, you can run the engine directly. First, activate the virtual environment:

`.\venv\Scripts\activate`

Then, you can run a custom sequence of tasks:

`python src\master_automation.py run-tasks export-personal convert-kdbx`

You can also perform a **stateful dry run**, which simulates a full run in a temporary directory that is automatically deleted at the end. This is the safest way to test your configuration.

`python src\master_automation.py run-tasks run-all --dry-run`


---

## Key Features

*   **One-Command Setup:** Run a single script to install all tools and dependencies automatically.
*   **Automate Everything:** A central script manages the entire backup process—from exporting and converting to syncing and reporting.
*   **Flexible Error Handling:** A simple flag in the `.env` file lets you choose whether the script should stop on the first error or continue processing other tasks.
*   **Professional Test Suite:** Includes a `pytest`-based framework to verify the core functionality of the application.
*   **Stateful "Dry Run" Mode:** Simulate a complete, realistic automation run in a temporary "sandbox" to safely test your entire workflow without touching your real data.
*   **Flexible Vault Backups:** Exports Bitwarden vaults to both raw JSON and the universally compatible KDBX format, ready for KeePassXC.
*   **Comprehensive Health Dashboard:** Keeps the automation's status constantly updated in two distinct formats: a human-readable Markdown file and a machine-readable JSON file.
*   **Smart Retention Policy:** Automatically rotates backup archives, keeping a configurable number of the most recent versions.
*   **Guaranteed Complete Logs:** Get automatic email alerts summarizing the run's success or failure. The **complete** execution log and all relevant status files are attached.

---

## How It Works: The Supervisor & Engine Model

The suite is designed for maximum reliability using a "Supervisor and Engine" model. This ensures that logging is always complete and that each task runs in a properly isolated environment.

1.  **The Supervisor (`run.bat`):** This script orchestrates the entire workflow in three distinct phases.
    *   **Phase 1 (Execution):** It launches the Python engine to run all backup and sync tasks, capturing all output to a timestamped log file.
    *   **Phase 2 (Log Generation):** It runs the engine in a special `--generate-log-only` mode to append the "future" log of the email report to the main log file, making it 100% complete on disk.
    *   **Phase 3 (Final Reporting):** It runs the engine a final time to read the completed log and send the email notification.

2.  **The Engine (`src/master_automation.py`):** This is the brain of the operation. It's responsible for executing the individual tasks.
    *   **Robust Task Execution:** It launches each helper script (e.g., `bw_export.py`) in its own isolated process. To guarantee the correct virtual environment is used, it explicitly calls the `venv\Scripts\activate.bat` script for each subprocess. This "brute-force" method is the most reliable way to ensure the correct Python packages are used on Windows.

---

## Project Structure

This tree shows the clean, organized layout of the project, with application code neatly separated into the `src` directory.


```
ctrl_s_master/
│
├── 📜 README.md                          # This file: Project overview, setup, and usage.
├── 🔑 .env.example                       # Template for all configuration variables.
├── 🔑 .env                               # (Gitignored) Your actual secrets and settings.
├── 📦 requirements.txt                   # Python dependencies for the virtual environment.
├── 🙈 .gitignore                         # Specifies which files and folders Git should ignore.
│
├── ⚙️ setup.bat                      # One-time script to build the entire environment from scratch.
├── 🔄 update.bat                         # Manually run this to update all dependencies to their latest versions.
├── ▶️ run.bat                 # The Supervisor: The main entry point to run the entire suite.
├── 🧪 tests.bat                      # A simple launcher for the automated test suite.
│
├── 🆘 _USER_emergency_kit.md             # (Gitignored) Your emergency kit.
│
├── 📁 src/                               # Contains all core application source code.
│   ├── 🐍 master_automation.py           # The Engine: Executes all logic as directed by the Supervisor.
│   └── 📁 _tools/                        # Contains all helper scripts and configurations.
│       ├── common_utils.py               # Shared functions like backup rotation.
│       └── ... (and other tools)
│
├── 📁 _tests/                            # (Hidden) Contains the automated test suite.
│   ├── temp/                             # (Gitignored) A temporary sandbox for test file generation.
│   └── test_automation_suite.py          # The pytest file with all test cases.
│
├── 📊 status.json                        # (Gitignored) Machine-readable history of the last 10 runs.
├── 📄 status_dashboard.md                # (Gitignored) Human-readable dashboard of the run history.
│
├── 📁 vaults/                            # (Gitignored) Local archive for password manager exports.
├── 📁 2fa/                               # (Gitignored) Sync destination for critical 2FA vaults.
├── 📁 backups/                           # (Gitignored) Sync destination for all other backups.
├── 📁 _logs/                             # (Gitignored) Contains logs from all script runs.
│
└── 📁 venv/                              # (Hidden & Gitignored) The Python virtual environment.
```


---

### Author  

This project was created and is maintained by myself - [gravi-ctrl](https://github.com/gravi-ctrl).