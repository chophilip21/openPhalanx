#!/usr/bin/env bash
# Interactive Aider wired to the in-container SGLang server, for manual testing:
#   docker exec -it -u $(id -u):$(id -g) -e HOME=/tmp -w /workspace openphalanx-backend aider-local
exec env \
  OPENAI_API_BASE="http://127.0.0.1:${SGLANG_PORT}/v1" \
  OPENAI_API_KEY="sk-local" \
  /opt/aider/bin/aider \
  --model "openai/${SERVED_MODEL_NAME}" \
  --edit-format "${AIDER_EDIT_FORMAT}" \
  --yes-always \
  --no-auto-commits \
  --no-dirty-commits \
  --no-check-update \
  --no-show-release-notes \
  --no-show-model-warnings \
  --no-analytics \
  "$@"
