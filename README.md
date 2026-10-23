# Grimnir for OpenCode

The public Grimnir toolkit running on [OpenCode](https://opencode.ai): the orchestrator brief,
the specialist agents, skills, engagement scripts, the confirmation harness and the shared
hunting rules, with the OpenCode surface already generated.

`.claude/` is the source of truth. `.opencode/` and `AGENTS.md` are generated from it, so never
hand-edit them. To regenerate after changing `.claude/` or `CLAUDE.md`:

```bash
python3 oc/sync-opencode.py            # generate
python3 oc/sync-opencode.py --check    # verify up to date
```

Permissions, the optional MCP server and the nested skill roots live in `opencode.json`.
Every agent runs on the model named in `oc/sync-opencode.py` (`DEFAULT_MODEL`; override with
`$GRIMNIR_MODEL`) and `opencode.json`.

```bash
opencode             # interactive session as the GRIMNIR orchestrator
opencode run "task"  # headless run
```

See `SECURITY.md` for the security model. Only test targets you are authorized to test.
