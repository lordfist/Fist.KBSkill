# Fist KB

**Simple, local memory for long-running agents.**

Fist KB helps agents remember the decisions, facts and context that matter across sessions. It focuses on a simple job: keeping useful knowledge organised and making it easy to find when it is needed. Knowledge lives in readable local files, with a small set of tools for saving, finding and updating it.

## Install (Windows)

Requires Git and Claude Code or the Codex CLI on Windows. First launch needs internet access to prepare the private Python runtime and dependencies through uv.

### Claude Code

Run these commands inside Claude Code:

```text
/plugin marketplace add lordfist/Fist.KBSkill
/plugin install fist-kb@fist-kb-local
```

Choose the installation scope when prompted.

### Codex

Run these commands in a terminal:

```text
codex plugin marketplace add lordfist/Fist.KBSkill
codex plugin add fist-kb@fist-kb-local
```

### Choose your KB folder

Start a new chat in your project, reloading the client if prompted, and ask:

> Set up Fist KB for this project.

Choose a new or existing KB and select its folder. Setup validates the selection, loads the memory workflow and remembers the folder. You can also enable project memory so future sessions receive the startup/recovery instructions.

For project-file evidence, choose your project folder as the parent of `KnowledgeBase`. For an existing KB, choose `KnowledgeBase` itself or its parent. See the [integration guide](docs/INTEGRATIONS.md) for other storage choices and local-checkout installation.

The session-start hook loads the selected KB's core at startup and after compaction; Codex asks you to review and trust the hook first.

## What it does

- **Keeps useful context:** decisions, findings, corrections and the evidence behind them.
- **Finds the right detail:** topic-based briefs, keyword search and individual record lookups.
- **Preserves distinctions:** facts, claims and recommendations retain their sources and uncertainty.
- **Makes changes reviewable:** updates are drafted and previewed before committing, with checks for conflicting changes.
- **Fits your workflow:** Claude Code and Codex plugins, plus a Python API and command-line interface.

No model dependency or vector database is required. Agents explicitly choose what to record; conversations are not automatically captured.

## Your knowledge, your files

Each project remembers its chosen KB in a small `.fist-kb.json` file. Different projects can use different knowledge bases, and the data stays separate from plugin installations and caches.

Records are stored locally as JSON and JSONL. They can be inspected, backed up and kept alongside your project. Keep private knowledge out of public repositories.

## CLI and development

From a source checkout, with Python 3.11+ and [uv](https://docs.astral.sh/uv/) available:

```text
uv run --locked fist-kb init /absolute/existing/project
uv run --locked fist-kb --kb /absolute/existing/project/KnowledgeBase context --brief --compact
```

Run tests or build release packages:

```text
uv run --locked python -m unittest discover -s tests -v
uv run --locked python scripts/build_release.py
```

The guided desktop launcher and folder picker currently support Windows. See [validation](VALIDATION.md) for the scope of testing.

[Usage and data format](docs/USAGE.md) · [Client integration](docs/INTEGRATIONS.md)

## Licence

Fist KB is [MIT licensed](LICENSE).
