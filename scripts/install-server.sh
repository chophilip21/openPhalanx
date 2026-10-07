#!/bin/sh
# OpenPhalanx server installer (Debian/Ubuntu, x86-64, NVIDIA GPU).
#
#   curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install-server.sh | sh
#
# Checks the prerequisites the app can't install for you (NVIDIA driver,
# Docker, NVIDIA Container Toolkit), then installs the Openphalanx app from
# the release's .deb. The app builds the backend image on first start (Docker
# downloads the official SGLang image, about 16 GB, once).
# OPPX_VERSION=1.2.3 installs a specific release instead of the latest.
set -eu

REPO="chophilip21/openPhalanx"
VERSION="${OPPX_VERSION:-latest}"

say() { printf '  %s\n' "$*"; }
ok() { printf '  \342\234\223 %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "the server runs on Linux only"
[ "$(uname -m)" = x86_64 ] || die "the server app is built for x86-64 only"
command -v apt-get >/dev/null 2>&1 || die "this installer needs apt (Debian/Ubuntu); other distros: use the AppImage from https://github.com/$REPO/releases"

missing=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    ok "NVIDIA driver: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    say "✗ NVIDIA driver: nvidia-smi doesn't work. Install the driver for your GPU first."; missing=1
fi
if command -v docker >/dev/null 2>&1; then
    ok "Docker: $(docker --version)"
    if docker info 2>/dev/null | grep -qi 'nvidia'; then
        ok "NVIDIA Container Toolkit is registered with Docker"
    else
        say "✗ NVIDIA Container Toolkit: install it, then run"
        say "    sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
        say "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
        missing=1
    fi
    if ! docker info >/dev/null 2>&1; then
        say "! Your user can't use Docker yet: sudo usermod -aG docker \$USER, then log out and back in."
    fi
else
    say "✗ Docker: install Docker Engine (https://docs.docker.com/engine/install/)"; missing=1
fi
[ "$missing" = 0 ] || die "fix the items above, then run this installer again"

if [ "$VERSION" = latest ]; then
    api="https://api.github.com/repos/$REPO/releases/latest"
else
    api="https://api.github.com/repos/$REPO/releases/tags/v${VERSION#v}"
fi
url="$(curl -fsSL "$api" | grep -o '"browser_download_url": *"[^"]*_amd64\.deb"' | head -1 | cut -d'"' -f4)"
[ -n "$url" ] || die "no .deb found in release $VERSION"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM
deb="$tmp/$(basename "$url")"
say "Downloading $(basename "$url")"
curl -fsSL "$url" -o "$deb"
sums="$(dirname "$url")/SHA256SUMS"
want="$(curl -fsSL "$sums" | grep " \*\{0,1\}$(basename "$url")\$" | cut -d' ' -f1)"
[ -n "$want" ] && [ "$(sha256sum "$deb" | cut -d' ' -f1)" = "$want" ] || die "checksum mismatch for $(basename "$url")"
sudo apt-get install -y "$deb"
ok "Installed. Start Openphalanx from your app menu (or run: openphalanx)."
say "Open TCP port 9090 to your laptops' network; never expose 9091."
