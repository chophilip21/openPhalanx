"""OpenPhalanx chat: a terminal frontend over Aider's editing engine.

`oppx` embeds this package and runs it (through `run.py`) with the Python
interpreter of the installed Aider, so `aider`, `prompt_toolkit` and `rich` are
importable. It lets Aider's own `main()` build the coder (repo map, edit
formats, model settings) from the arguments oppx passes, but swaps in our
InputOutput class first, then drives `coder.run_one()` from our own prompt.
Every line on screen, every prompt and every confirmation goes through here.

Keys and commands follow Claude Code, so people can switch without relearning.

Environment from oppx: OPENAI_API_BASE / OPENAI_API_KEY (the loopback proxy),
OPPX_SERVER, OPPX_CONTEXT, OPPX_MODEL_ID (what the server runs), OPPX_WEB
("1"/"0"), OPPX_VERSION, OPPX_BIN, OPPX_INITIAL (a first prompt) and
OPPX_PRINT ("1": answer once and exit).

Modules, lowest layer first (each imports only from the ones above it):

    config     environment from oppx, colors, names
    term       console output, the turn spinner, Esc-to-interrupt
    render     streamed answers (reasoning and edit blocks hidden), diffs
    context    ContextManager: fit every request into the context window
    oppx_io    Aider's InputOutput restyled: prompt, keys, confirmations
    routing    the one-token ask/edit check
    cache      keep requests prefix-cache friendly (stable repo map and prompts)
    commands   slash commands, memory, and message translation
    app        patches into Aider, the turn loop, main()
"""
