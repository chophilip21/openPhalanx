#!/usr/bin/env bash
# Launches the SGLang inference engine on the loopback interface only; the
# agent server is the sole externally exposed endpoint.
#
# If SGLang exits, the whole container stops (SIGTERM to supervisord, PID 1)
# instead of being restarted in a loop, so the GUI sees the failure and can
# show the log that explains it.
set -uo pipefail

args=(
  --model-path "${MODEL_PATH}"
  --served-model-name "${SERVED_MODEL_NAME}"
  --host 127.0.0.1
  --port "${SGLANG_PORT}"
  --mem-fraction-static "${MEM_FRACTION_STATIC}"
  --enable-metrics
  # The GUI polls these every 2 s; keep them out of the log it displays.
  --uvicorn-access-log-exclude-prefixes /metrics /v1/models /health
)
if [[ -n "${CONTEXT_LENGTH:-}" ]]; then
  args+=(--context-length "${CONTEXT_LENGTH}")
fi

# SGLANG_EXTRA_ARGS allows ad-hoc flags (e.g. "--quantization awq_marlin").
# shellcheck disable=SC2086
python3 -m sglang.launch_server "${args[@]}" ${SGLANG_EXTRA_ARGS:-} &
child=$!
trap 'kill -TERM "$child" 2>/dev/null' TERM INT
wait "$child"
code=$?
echo "SGLang exited with code ${code}; stopping the container." >&2
kill -TERM 1
exit "$code"
