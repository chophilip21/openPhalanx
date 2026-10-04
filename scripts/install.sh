#!/bin/sh
# OpenPhalanx client installer.
#
#   curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install.sh | sh
#
# Installs the prebuilt `oppx` for this machine into ~/.local/bin (override
# with OPPX_INSTALL_DIR), after checking its SHA-256 against the release's
# SHA256SUMS. Then sets up the coding engine (a private Aider in oppx's data
# folder), so nothing else has to be installed by hand.
# OPPX_VERSION=1.2.3 installs a specific release instead of the latest.
set -eu

REPO="chophilip21/openPhalanx"
INSTALL_DIR="${OPPX_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${OPPX_VERSION:-latest}"

say() { printf '  %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux) os=unknown-linux-musl ;;
    Darwin) os=apple-darwin ;;
    *) die "unsupported OS $(uname -s); build from source: https://github.com/$REPO" ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) arch=aarch64 ;;
    *) die "unsupported CPU $(uname -m); build from source: https://github.com/$REPO" ;;
esac
target="$arch-$os"

if [ "$VERSION" = latest ]; then
    base="https://github.com/$REPO/releases/latest/download"
else
    base="https://github.com/$REPO/releases/download/v${VERSION#v}"
fi

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q "$1" -O "$2"; }
else
    die "curl or wget is required"
fi
if command -v sha256sum >/dev/null 2>&1; then
    sha() { sha256sum "$1" | cut -d' ' -f1; }
else
    sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

asset="oppx-$target.tar.gz"
say "Downloading $asset ($VERSION)"
fetch "$base/$asset" "$tmp/$asset" || die "no oppx build for $target in release $VERSION"
fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS" || die "cannot download SHA256SUMS"
want="$(grep " \*\{0,1\}$asset\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)"
[ -n "$want" ] || die "SHA256SUMS has no entry for $asset"
[ "$(sha "$tmp/$asset")" = "$want" ] || die "checksum mismatch for $asset; nothing was installed"

tar -xzf "$tmp/$asset" -C "$tmp"
mkdir -p "$INSTALL_DIR"
install -m 755 "$tmp/oppx" "$INSTALL_DIR/oppx"
say "Installed $INSTALL_DIR/oppx ($("$INSTALL_DIR/oppx" --version))"

# The coding engine: one minute now instead of on the first run.
"$INSTALL_DIR/oppx" --update || say "The coding engine will be set up on the first run instead."

case ":$PATH:" in
    *":$INSTALL_DIR:"*) ;;
    *) say "Add $INSTALL_DIR to your PATH, e.g.: echo 'export PATH=\"$INSTALL_DIR:\$PATH\"' >> ~/.profile" ;;
esac
say "Next: pair with your server:  oppx pair <server> <code>   (the code is shown in the server app)"
