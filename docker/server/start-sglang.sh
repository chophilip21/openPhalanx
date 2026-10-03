#!/usr/bin/env bash
# Launches the SGLang inference engine on the loopback interface only; the
# agent server is the sole externally exposed endpoint.
set -euo pipefail

args=(
  --model-path "${MODEL_PATH}"
  --served-model-name "${SERVED_MODEL_NAME}"
  --host 127.0.0.1
  --port "${SGLANG_PORT}"
  --mem-fraction-static "${MEM_FRACTION_STATIC}"
  --enable-metrics
)
if [[ -n "${CONTEXT_LENGTH:-}" ]]; then
  args+=(--context-length "${CONTEXT_LENGTH}")
fi

# SGLANG_EXTRA_ARGS allows ad-hoc flags (e.g. "--quantization awq_marlin").
# shellcheck disable=SC2086
exec python3 -m sglang.launch_server "${args[@]}" ${SGLANG_EXTRA_ARGS:-}
