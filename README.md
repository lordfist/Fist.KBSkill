# Fist KB

**Simple, local memory for long-running agents.**

Fist KB helps agents remember the decisions, facts and context that matter across sessions. It focuses on a simple job: keeping useful knowledge organised and making it easy to find when it is needed. Knowledge lives in readable local files, with a small set of tools for saving, finding and updating it.

## What it does

- **Keeps useful context:** decisions, findings, corrections and the evidence behind them.
- **Finds the right detail:** topic-based briefs, keyword search and individual record lookups.
- **Preserves distinctions:** facts, claims and recommendations retain their sources and uncertainty.
- **Makes changes reviewable:** updates are drafted and previewed before committing, with checks for conflicting changes.
- **Fits your workflow:** Claude Code and Codex plugins, plus a Python API and command-line interface.

No model dependency or vector database is required. Agents explicitly choose what to record; conversations are not automatically captured.

## Get started

The Windows plugins provide guided setup:

1. Install **Fist KB** using the [client integration guide](docs/INTEGRATIONS.md).
2. Ask your agent: **“Set up Fist KB for this project.”**
3. Choose a new or existing KB, then select its folder.
4. The agent validates the selection, loads the memory workflow and remembers the folder.
5. Optionally enable project memory: the agent adds a small startup/recovery entry to your project instructions with your consent. Manual use remains available.

In later sessions the plugin's session-start hook adds the selected KB's core to the agent's context, again after compaction, without repeating it on every turn. Codex asks you to trust the hook first.

For project-file evidence, choose the project folder as the parent of `KnowledgeBase`. An external note store has a separate evidence boundary, which setup explains before accepting it. For an existing KB, choose `KnowledgeBase` itself or its parent folder.

The first launch needs internet access to prepare the Python runtime and dependencies through uv. A fresh chat or client reload may be needed after installation.

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
