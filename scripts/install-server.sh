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
#
# For a machine without a desktop, install only `oppxs` (the server as a
# command) and a systemd user service, on any Linux distribution:
#
#   curl -fsSL …/install-server.sh | sh -s -- --headless
set -eu

HEADLESS=0
for arg in "$@"; do
    case "$arg" in
        --headless) HEADLESS=1 ;;
        *) echo "error: unknown option $arg" >&2; exit 1 ;;
    esac
done

REPO="chophilip21/openPhalanx"
VERSION="${OPPX_VERSION:-latest}"

say() { printf '  %s\n' "$*"; }
ok() { printf '  \342\234\223 %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "the server runs on Linux only"
if [ "$HEADLESS" = 0 ]; then
    [ "$(uname -m)" = x86_64 ] || die "the server app is built for x86-64 only (oppxs also runs on arm64: --headless)"
    command -v apt-get >/dev/null 2>&1 || die "this installer needs apt (Debian/Ubuntu); other distros: use the AppImage from https://github.com/$REPO/releases, or --headless"
fi

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
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

# Downloads a release file into $tmp ($file) and checks it against SHA256SUMS.
fetch() {
    file="$tmp/$(basename "$1")"
    say "Downloading $(basename "$1")"
    curl -fsSL "$1" -o "$file"
    want="$(curl -fsSL "$(dirname "$1")/SHA256SUMS" | grep " \*\{0,1\}$(basename "$1")\$" | cut -d' ' -f1)"
    [ -n "$want" ] && [ "$(sha256sum "$file" | cut -d' ' -f1)" = "$want" ] || die "checksum mismatch for $(basename "$1")"
}

if [ "$HEADLESS" = 1 ]; then
    case "$(uname -m)" in
        x86_64) target=x86_64-unknown-linux-musl ;;
        aarch64|arm64) target=aarch64-unknown-linux-musl ;;
        *) die "no oppxs build for $(uname -m)" ;;
    esac
    url="$(curl -fsSL "$api" | grep -o "\"browser_download_url\": *\"[^\"]*oppxs-$target\.tar\.gz\"" | head -1 | cut -d'"' -f4)"
    [ -n "$url" ] || die "no oppxs build for $target in release $VERSION"
    fetch "$url"
    bin="${OPPX_INSTALL_DIR:-$HOME/.local/bin}"
    mkdir -p "$bin"
    tar -xzf "$file" -C "$tmp" oppxs
    install -m 755 "$tmp/oppxs" "$bin/oppxs"
    ok "Installed $bin/oppxs ($("$bin/oppxs" --version))"
    case ":$PATH:" in *":$bin:"*) ;; *) say "! $bin isn't on your PATH; add it to your shell profile." ;; esac

    unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/oppxs.service"
    mkdir -p "$(dirname "$unit")"
    {
        echo "[Unit]"
        echo "Description=OpenPhalanx server (oppxs)"
        echo "After=network-online.target docker.service"
        echo "Wants=network-online.target"
        echo
        echo "[Service]"
        echo "ExecStart=$bin/oppxs run"
        echo "Restart=on-failure"
        echo "RestartSec=5"
        echo "TimeoutStopSec=60"
        echo
        echo "[Install]"
        echo "WantedBy=default.target"
    } > "$unit"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Service file written: $unit"
    say "Start the server now and at login:   systemctl --user enable --now oppxs"
    say "Also at boot, without logging in:    loginctl enable-linger \$USER"
    say "Then: oppxs models, oppxs download <model>, oppxs use <model>, oppxs start, oppxs pair"
    say "Open TCP port 9090 to your laptops' network; never expose 9091 or 9094."
    exit 0
fi

url="$(curl -fsSL "$api" | grep -o '"browser_download_url": *"[^"]*_amd64\.deb"' | head -1 | cut -d'"' -f4)"
[ -n "$url" ] || die "no .deb found in release $VERSION"
fetch "$url"
sudo apt-get install -y "$file"
ok "Installed. Start Openphalanx from your app menu (or run: openphalanx)."
say "The same server is also a command, for scripts and SSH: oppxs --help"
say "Open TCP port 9090 to your laptops' network; never expose 9091."
