#!/bin/bash
# @DESCRIPTION: Installs or updates Neovim from the official GitHub release tarball (/opt/nvim)
# @FREQUENCY: On Demand (triggered by `setup.sh`, re-run to upgrade or pin a new version)
# @CAPABILITIES:
#   • Bypasses the outdated apt package by installing the official prebuilt Neovim release.
#   • Resolves the latest or a pinned version tag via the GitHub releases API.
#   • Verifies the download against GitHub's SHA-256 asset digest and aborts if it is missing or mismatched.
#   • Installs into a self-contained /opt/nvim directory and symlinks it to /usr/local/bin/nvim.
#   • Replaces any previous install on re-run, making upgrades and version pins repeatable.
#   • Cleans up temporary files automatically and fails fast on any error (set -euo pipefail).

set -euo pipefail

VERSION="${1:-latest}"            # or pin e.g. v0.11.4
ASSET="nvim-linux-x86_64.tar.gz"  # nvim-linux-arm64.tar.gz on ARM
API="https://api.github.com/repos/neovim/neovim/releases"

if [[ "$VERSION" == "latest" ]]; then URL="$API/latest"; else URL="$API/tags/$VERSION"; fi

release=$(curl -fsSL "$URL")
digest=$(jq -r --arg a "$ASSET" '.assets[] | select(.name==$a) | .digest' <<<"$release")
dl=$(jq -r --arg a "$ASSET" '.assets[] | select(.name==$a) | .browser_download_url' <<<"$release")

[[ "$digest" == sha256:* ]] || { echo "No sha256 digest found for $ASSET" >&2; exit 1; }
expected="${digest#sha256:}"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/$ASSET" "$dl"
echo "$expected  $tmp/$ASSET" | sha256sum -c -

sudo rm -rf /opt/nvim
sudo mkdir -p /opt/nvim
sudo tar -xzf "$tmp/$ASSET" -C /opt/nvim --strip-components=1
sudo ln -sf /opt/nvim/bin/nvim /usr/local/bin/nvim

nvim --version | head -1
