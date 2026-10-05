#!/usr/bin/env bash
# Build the backend image and publish it to GitHub Container Registry.
#
#   scripts/publish-image.sh            # tag = workspace version (Cargo.toml)
#   scripts/publish-image.sh 0.1.1      # explicit tag
#   OPPX_REPUSH=1 scripts/publish-image.sh   # overwrite an already published tag
#
# Needs `docker login ghcr.io` with a token that has the write:packages scope:
#   gh auth token | docker login ghcr.io -u <github-user> --password-stdin
# (run `gh auth refresh -s write:packages` first if the token lacks the scope).
# New GHCR packages start private; make it public in the package settings so
# the app can pull it without credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

version="${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)}"
image="ghcr.io/chophilip21/openphalanx-backend:${version}"

# A published tag is what every app of that version pulls: never swap it
# silently. Re-pushing on purpose needs OPPX_REPUSH=1.
if docker manifest inspect "$image" >/dev/null 2>&1 && [ "${OPPX_REPUSH:-}" != 1 ]; then
    echo "$image is already published; refusing to overwrite it (OPPX_REPUSH=1 to force)." >&2
    exit 1
fi

docker build -f docker/Dockerfile.server -t "$image" docker/
docker push "$image"
echo "Published $image ($(docker inspect --format '{{index .RepoDigests 0}}' "$image"))"
