#!/usr/bin/env python3
"""Generate the OpenCode surface (.opencode/ + AGENTS.md) from the Claude Code
surface (.claude/). One-way: `.claude/` is the source of truth, `.opencode/` is
derived and never hand-edited.

Why this exists
---------------
Grimnir ships two harnesses off one prompt corpus. Claude Code reads
`.claude/agents`, `.claude/commands` and `CLAUDE.md`. OpenCode V2 reads
`.opencode/agents`, `.opencode/commands` and `AGENTS.md` -- and, usefully, ALREADY
reads `.claude/skills` and `.agents/skills` natively, so skills are never copied.
Only agents, commands and the orchestrator brief need deriving.

The mapping is not a rename. Claude's agent frontmatter uses `tools:` (a hard
allowlist) and bare model aliases; OpenCode V2 rejects `tools` outright and wants
ordered `permissions` rules plus a `provider/model` string. That translation lives
here so it is applied identically to all 21 agents instead of by hand.

Idempotent and fail-clean: re-running produces byte-identical output, and any
parse failure aborts before a single file is written, so a malformed agent
definition can never leave a half-generated `.opencode/` behind.

Usage
-----
    python3 oc/sync-opencode.py            # generate
    python3 oc/sync-opencode.py --check    # verify up to date, write nothing
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE_DIR = REPO / ".claude"
OPENCODE_DIR = REPO / ".opencode"

# The single model every Grimnir agent runs on. Claude Code's per-agent tiering
# (`opus` for the artemis testers, `sonnet` elsewhere) has no OpenCode Zen
# equivalent, so the tiers collapse. Override with --model or $GRIMNIR_MODEL.
DEFAULT_MODEL = "deepseek/deepseek-flash"
MODEL = os.environ.get("GRIMNIR_MODEL", DEFAULT_MODEL)

# Claude's `tools: Read, Grep, Glob, Bash` is a hard allowlist with no Write/Edit.
# OpenCode expresses that as: local discovery allowed, mutation denied. Every
# agent here writes through `bash scripts/*.sh` rather than a write tool, so
# denying `edit` reproduces Claude's behaviour instead of widening it.
#
# `shell` is deliberately NOT allowed here. Claude's allow/deny lists live in
# .claude/settings.json and are translated into the global `permissions` block in
# opencode.json; anything unlisted falls through to OpenCode's `ask`, which is
# what Claude does too. Allowing `shell: *` per agent would silently defeat that
# allowlist and auto-run tools the operator never approved.
TOOL_PERMISSIONS = [
    {"action": "read", "resource": "*", "effect": "allow"},
    {"action": "glob", "resource": "*", "effect": "allow"},
    {"action": "grep", "resource": "*", "effect": "allow"},
    {"action": "edit", "resource": "*", "effect": "deny"},
    # Leaf agents. No agent delegates to another in Claude Code, so blocking
    # nested dispatch here prevents runaway fan-out that Claude could not do.
    {"action": "subagent", "resource": "*", "effect": "deny"},
]

# Nested specialists get a namespace in both harnesses. Flattening would collide:
# recon-agent and report-generator each exist under both artemis/ and specter/.
NAMESPACES = ("artemis", "specter")

# Per-agentist commands are intentionally NOT all ported. Claude Code's 42 become
# 23: keep every root command (the orchestrator surface CLAUDE.md actually refers
# to) plus the nested ones carrying tool invocations, a routing table, or a
# methodology absent from the matching SKILL.md. Thin "delegate to <agent>" stubs
# and ladders already documented in a skill are dropped -- their content is
# reachable through the skill or the agent directly. The originals remain in
# .claude/commands, so Claude Code keeps all 42.
NESTED_COMMANDS = {
    "artemis": ["assess", "full-audit"],
    "hunter": ["full-audit", "vuln-scan", "enumerate"],
    "specter": ["full-audit", "test-rest-api", "test-bola"],
}

AGENTS_MD_HEADER = """<!-- GENERATED FILE -- do not edit.
Source: CLAUDE.md   Regenerate: python3 oc/sync-opencode.py
OpenCode reads AGENTS.md; it does not fall back to CLAUDE.md. The brief below is
CLAUDE.md with harness-specific paths and delegation wording adjusted. Edit
CLAUDE.md, then re-run the sync. -->

## OpenCode Runtime

`.claude/` is the source of truth. `.opencode/` and this file are generated from it by
`oc/sync-opencode.py` -- never hand-edit them; re-run the script instead.

- **Delegation**: launch a specialist with the `subagent` tool, passing the agent ID
  (`huginn`, `specter`, `artemis`, `hunter`, `nimbus`, `nomad`, `report-compiler`).
  Nested testers are `artemis/<id>` and `specter/<id>`. Run specialists in parallel by
  issuing several `subagent` calls; a command needs `subagent: true` to run detached.
- **Agent specs** live in `.opencode/agents/` (derived from `.claude/agents/`).
- **Skills** are read natively from `.claude/skills/` -- unchanged, not copied.
- **Commands** live in `.opencode/commands/`. Specialist commands are namespaced:
  `/artemis/assess`, `/hunter/vuln-scan`, `/specter/test-bola`, and so on.
- Permissions, the MCP server and the nested skill roots are declared in
  `opencode.json` at the repo root.

---

"""


def fail(msg: str) -> "None":
    print(f"sync-opencode: {msg}", file=sys.stderr)
    raise SystemExit(1)


def split_frontmatter(text: str, source: str) -> "tuple[dict[str, str], str]":
    """Return (frontmatter dict, body).

    Parses the flat YAML subset Claude Code agent files actually use: `key: value`
    scalars plus folded (`>`) and literal (`|`) block scalars. Anything outside that
    subset aborts rather than being silently dropped, so an unsupported construct
    surfaces here instead of quietly truncating an agent's description.
    """
    if not text.startswith("---"):
        fail(f"{source}: missing frontmatter block")
    end = text.find("\n---", 3)
    if end == -1:
        fail(f"{source}: unterminated frontmatter block")
    raw, body = text[3:end], text[end + 4 :].lstrip("\n")

    meta: dict[str, str] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1].isspace():
            fail(f"{source}: unexpected indent at {line!r}")
        key, sep, value = line.partition(":")
        if not sep:
            fail(f"{source}: unparseable frontmatter line {line!r}")
        key, value = key.strip(), value.strip()

        if value in (">", "|", ">-", "|-"):
            block: list[str] = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i].strip())
                i += 1
            # `>` folds to a single space-joined line; `|` preserves newlines.
            meta[key] = " ".join(b for b in block if b) if value.startswith(">") else "\n".join(block)
            continue

        meta[key] = value
        i += 1
    return meta, body


def yaml_str(value: str) -> str:
    """Quote a scalar for YAML frontmatter when it is not plainly safe."""
    if value and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_ ./-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_flow_list(rules: "list[dict[str, str]]") -> str:
    """Render permission rules as YAML flow mappings.

    Key order is fixed (action, resource, effect) so output stays byte-stable
    across runs and `--check` can diff it.
    """
    order = ("action", "resource", "effect")
    rendered = []
    for rule in rules:
        missing = [k for k in order if k not in rule]
        if missing:
            fail(f"permission rule missing {missing}: {rule}")
        rendered.append("{" + ", ".join(f"{k}: {yaml_str(rule[k])}" for k in order) + "}")
    return "[" + ", ".join(rendered) + "]"


def render_agent(meta: dict[str, str], body: str) -> str:
    description = meta.get("description")
    if not description:
        fail(f"agent {meta.get('name', '?')}: no description")

    # Claude Code's `skills:` preloads named skills into the agent. OpenCode has no
    # per-agent skill preload -- skills are advertised by description and loaded on
    # request -- so the list is restated as an instruction in the body instead.
    preload = [s.strip() for s in meta.get("skills", "").split(",") if s.strip()]
    if preload:
        listed = ", ".join(f"`{s}`" for s in preload)
        body = (
            f"## Skills\n\nLoad these before testing: {listed}.\n\n"
            "Call the `skill` tool with each exact ID.\n\n" + body
        )

    return (
        "---\n"
        f"description: {yaml_str(description)}\n"
        "mode: subagent\n"
        f"model: {MODEL}\n"
        f"permissions: {yaml_flow_list(TOOL_PERMISSIONS)}\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def render_orchestrator_agent() -> str:
    """The primary agent. OpenCode's default primary (`build`) is a coding agent
    that would start editing files; CLAUDE.md forbids exactly that."""
    # `shell` is inherited from the global allowlist in opencode.json rather than
    # allowed outright, so the operator still gets prompted for an unapproved tool.
    permissions = [{"action": "subagent", "resource": "*", "effect": "allow"}]
    return (
        "---\n"
        "description: GRIMNIR engagement orchestrator. Classifies targets, scopes "
        "engagements, delegates to specialist agents, tracks progress, compiles "
        "reports. Does not perform testing itself.\n"
        "mode: primary\n"
        f"model: {MODEL}\n"
        f"permissions: {yaml_flow_list(permissions)}\n"
        "---\n\n"
        "You are the GRIMNIR orchestrator. Your full brief is in `AGENTS.md` at the "
        "repo root, which is loaded as project instruction -- read it and follow it.\n\n"
        "You do not perform testing yourself. You classify targets, scope the "
        "engagement, delegate each layer to the right specialist via the `subagent` "
        "tool, track progress, and compile the unified report.\n"
    )


def command_body(src: Path) -> str:
    """Command bodies are prompt templates. None of the Grimnir commands carry
    frontmatter, so the file is passed through verbatim; if one ever gains a
    frontmatter block it is stripped so the metadata never leaks into the prompt."""
    text = src.read_text()
    if not text.startswith("---"):
        return text.rstrip() + "\n"
    _, body = split_frontmatter(text, str(src))
    return body.rstrip() + "\n"


def collect_agents() -> "dict[str, str]":
    out: dict[str, str] = {"grimnir": render_orchestrator_agent()}
    sources: list[Path] = sorted(CLAUDE_DIR.glob("agents/*.md"))
    for ns in NAMESPACES:
        sources += sorted((REPO / ns / ".claude" / "agents").glob("*.md"))
    for src in sources:
        meta, body = split_frontmatter(src.read_text(), str(src))
        parts = src.relative_to(REPO).parts
        if parts[0] == ".claude":
            agent_id = src.stem
        else:
            agent_id = f"{parts[0]}/{src.stem}"
        out[agent_id] = render_agent(meta, body)
    return out


def collect_commands() -> "dict[str, str]":
    out: dict[str, str] = {}
    for src in sorted(CLAUDE_DIR.glob("commands/*.md")):
        out[src.stem] = command_body(src)
    for ns, names in NESTED_COMMANDS.items():
        base = REPO / ns / ".claude" / "commands"
        for name in names:
            src = base / f"{name}.md"
            if not src.exists():
                fail(f"whitelisted command missing: {src}")
            out[f"{ns}/{name}"] = command_body(src)
    return out


def render_agents_md() -> str:
    text = (REPO / "CLAUDE.md").read_text()
    # Paths that moved between harnesses. `.claude/skills/` is deliberately left
    # alone: OpenCode reads it natively, so those references stay correct.
    text = text.replace(".claude/agents/", ".opencode/agents/")
    text = text.replace(".claude/commands/", ".opencode/commands/")
    return AGENTS_MD_HEADER + text


def write_tree(root: Path, files: "dict[str, str]") -> "list[str]":
    written: list[str] = []
    if root.exists():
        shutil.rmtree(root)
    for rel, content in sorted(files.items()):
        dest = root / f"{rel}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        written.append(str(dest.relative_to(REPO)))
    return written


def main() -> int:
    global MODEL

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify up to date, write nothing")
    ap.add_argument(
        "--model",
        default=MODEL,
        help=f"provider/model for every agent (default {MODEL}, or $GRIMNIR_MODEL)",
    )
    args = ap.parse_args()
    MODEL = args.model

    agents, commands = collect_agents(), collect_commands()
    agents_md = render_agents_md()

    if args.check:
        drift = []
        for rel, content in sorted(agents.items()):
            dest = OPENCODE_DIR / "agents" / f"{rel}.md"
            if not dest.exists() or dest.read_text() != content:
                drift.append(str(dest.relative_to(REPO)))
        for rel, content in sorted(commands.items()):
            dest = OPENCODE_DIR / "commands" / f"{rel}.md"
            if not dest.exists() or dest.read_text() != content:
                drift.append(str(dest.relative_to(REPO)))
        target = REPO / "AGENTS.md"
        if not target.exists() or target.read_text() != agents_md:
            drift.append("AGENTS.md")
        if drift:
            print("sync-opencode: out of date, re-run without --check:", file=sys.stderr)
            for item in drift:
                print(f"  {item}", file=sys.stderr)
            return 1
        print(f"sync-opencode: up to date ({len(agents)} agents, {len(commands)} commands)")
        return 0

    written = write_tree(OPENCODE_DIR / "agents", agents)
    written += write_tree(OPENCODE_DIR / "commands", commands)
    (REPO / "AGENTS.md").write_text(agents_md)
    written.append("AGENTS.md")

    print(f"sync-opencode: wrote {len(written)} files")
    print(f"  agents   : {len(agents)} ({len(agents) - 1} derived + 1 orchestrator)")
    print(f"  commands : {len(commands)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())