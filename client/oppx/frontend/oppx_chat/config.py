"""Settings passed in by oppx through the environment, plus colors and names."""

import os


TESTED_AIDER = "0.86"  # internals this file relies on; other versions get a warning

ACCENT = "#5EEAD4"
MUTED = "#8A95AB"
GREEN = "#34D399"
RED = "#F87171"
YELLOW = "#FBBF24"
BULLET = "⏺"
ELBOW = "⎿"

SERVER = os.environ.get("OPPX_SERVER", "server")
CONTEXT = int(os.environ.get("OPPX_CONTEXT", "32768"))
WEB = os.environ.get("OPPX_WEB", "1") == "1"
VERSION = os.environ.get("OPPX_VERSION", "")
OPPX_BIN = os.environ.get("OPPX_BIN", "oppx")
PRINT_MODE = os.environ.get("OPPX_PRINT") == "1"
# The server's model reasons before answering (from /v1/info); see routing.ask_choice.
REASONING = os.environ.get("OPPX_REASONING") == "1"
MODEL_LABEL = "openphalanx-coder"  # the name the gateway serves any model under
# What the server actually runs (a Hugging Face id or a path); shown to the user.
MODEL_NAME = os.environ.get("OPPX_MODEL_ID", "").rstrip("/").rsplit("/", 1)[-1] or MODEL_LABEL
# "agent": the model reads the repository on demand through tools (agent.py).
# "aider": Aider's own loop (whole files and a repo map in every request).
ENGINE = os.environ.get("OPPX_ENGINE", "agent")
MEMORY_FILE = "OPENPHALANX.md"  # our CLAUDE.md
EXTRA_MEMORY = ("AGENTS.md",)  # read too when present
