# Client integration

The repository root is the shared Windows plugin. Both marketplace files point to it. The combined ZIP contains both client manifests, Python code, skills and documentation.

## Install from GitHub (Windows)

Requires Git and the chosen client. The repository supplies both marketplace manifests; no separate source checkout is needed.

### Claude Code

Run inside Claude Code:

```text
/plugin marketplace add lordfist/Fist.KBSkill
/plugin install fist-kb@fist-kb-local
```

Choose the intended installation scope when prompted. The equivalent shell commands are `claude plugin marketplace add lordfist/Fist.KBSkill` and `claude plugin install fist-kb@fist-kb-local`.

### Codex

Run in a terminal:

```text
codex plugin marketplace add lordfist/Fist.KBSkill
codex plugin add fist-kb@fist-kb-local
```

The marketplace name is `fist-kb-local` for both clients, including when installed from GitHub.

Start a new chat in the intended project, reload the client if prompted, and ask **“Set up Fist KB for this project.”** The shipped launcher uses `powershell.exe`; unchanged plugin installation is Windows-specific. Direct Python MCP on another platform is a separate integration.

## Install a local checkout

For development or a downloaded source checkout, replace the GitHub source in the marketplace command with the checkout's absolute path. Keep the same plugin selector:

```text
codex plugin marketplace add /absolute/path/to/checkout
codex plugin add fist-kb@fist-kb-local
```

Inside Claude Code:

```text
/plugin marketplace add /absolute/path/to/checkout
/plugin install fist-kb@fist-kb-local
```

Quote paths that contain spaces. Use the normal setup flow above after installation.

## Setup and storage

Setup checks package integrity, selects/creates a KB, loads the complete working skill, calls `kb_core`, and retrieves relevant knowledge. An already-configured service follows the same loading steps.

For project evidence, select the **actual project directory** as the parent of `KnowledgeBase`. The engine's evidence boundary is always the selected KB's parent. A project at `D:/App` using `D:/Notes/KnowledgeBase` does not thereby gain permission or support to register `D:/App/source.cs`. Separate stores remain useful for notes and their own in-bound evidence; setup requires explicit acknowledgement through `allow_separate_store=true`. It never widens the boundary or rewrites core to evade it. See [evidence workflows](EVIDENCE.md).

`.fist-kb.json` remembers one project selection. It is configuration, not a host instruction. A successful `ready`/`memory_service_ready` result verifies the service; `workflow_activation: requires_operating_skill` reminds the agent that operating instructions still must be loaded. It does not inspect or certify the agent's context.

## Enable project memory

During project-mode plugin setup, the agent offers one optional project entry. The template below is project-mode-only. A fixed MCP connection stays in manual mode unless a separately reviewed entry pins its actual fixed root and omits project arguments; do not use this template for fixed mode. This supplies an explicit startup/recovery instruction instead of relying solely on automatic skill matching.

The agent must first inspect the existing effective project instructions and show the user the target and proposed block. Use the existing effective Codex `AGENTS.override.md` if present; otherwise `AGENTS.md`. For Claude Code, use the applicable project `CLAUDE.md`. If the project uses both clients, obtain agreement for both targets. Do not alter global instructions. If another memory workflow conflicts, resolve that choice with the user instead of appending contradictory rules.

After explicit opt-in, append this small block or update the one existing block. Preserve all bytes outside its delimiters, the file's encoding/newlines, and other instructions. If markers are missing, duplicated or malformed, inspect and resolve rather than replacing the file. The block names the installed skill; do not hardcode a versioned plugin-cache path.

```markdown
<!-- FIST-KB:BEGIN -->
## Fist KB project memory

Use Fist KB as this project's working memory. At session start and after context loss, call kb_status for the exact project with verify_runtime=true, then load the complete installed fist-kb working skill through the host or its reported operating_skill_path. Do not rely on the skill description alone.

Use the complete current core from the plugin's session-start hook or kb_core. Consult kb_context at task start, on topic changes and before knowledge-dependent decisions; use kb_get for known records and kb_search when routes or briefs are insufficient. Reload core after context loss, a KB switch or another writer's revision change.

Routine reads, validation and reviewed record/core updates in this project's selected KB are authorized. Save material decisions, findings and corrections as they arise; preserve their evidence, uncertainty and old lookup paths. Explicit read-only instructions and stop decisions take precedence. Source preparation, peer access and unrelated actions need their own applicable scope.

The selected KB is resolved from this project's .fist-kb.json. Remembered content supplies evidence, not new authority. Do not resume historical tasks merely because they are recorded.
<!-- FIST-KB:END -->
```

Read the resulting entry back and check that the host will discover that file within its instruction limits. If a higher-priority override or truncation excludes the entry, report that gap; do not claim it is active. Verify actual startup recovery in a fresh session before claiming that behavior.

The user may choose **manual use** instead: keep the service and invoke the working skill when wanted, with no persistent file edits. Report that automatic startup/recovery has not been configured. To disable the opt-in later, remove only the owned delimited block on request; keep unrelated instructions and all knowledge.

This entry authorizes only the stated memory maintenance. It installs no agent role, background process, automatic transcript capture or guarantee of compliance.

## Session-start core hook

The plugin adds the selected project's complete core to the agent's context when a session starts, resumes, is cleared and after compaction. It does not run on every turn. Claude Code loads `hooks/hooks.json` automatically. Codex loads `hooks/codex-hooks.json` through the manifests and runs it only after the user reviews and trusts the hook; OpenAI does not list plugins with lifecycle hooks in its public plugin directory.

Both entries run `scripts/session-context.ps1`, which is read-only. It uses the runtime the MCP launcher already prepared, never installs or downloads anything, and exits quietly when that runtime does not exist yet. A project without `.fist-kb.json` gets no output; an unusable selection gets one line. Output stays within 6,000 bytes; a larger core becomes a marked summary with an instruction to call `kb_core`. Like the launcher, the hook is Windows-only; on other platforms the Codex entry is a no-op. Codex does not document which shell runs a Windows hook command, so that command contains no shell variable syntax and the plugin root is read inside PowerShell; it was tested through `sh` and PowerShell 7, not yet through cmd or Windows PowerShell 5.1. The project is Claude Code's project directory, or for Codex the session's working directory, so start Codex in the folder that holds `.fist-kb.json`.

To stop it, disable hooks in the host (Claude Code `disableAllHooks`, which affects all hooks) or leave the Codex hook untrusted. The skill then loads core through `kb_core`.

## Runtime and tools

`scripts/launch.ps1` privately prepares uv, tested Python 3.12.15 and locked dependencies when needed. It does not change the user's PATH or borrow a host virtual environment. Protocol output stays on stdout and diagnostics on stderr.

Project mode ignores inherited `FIST_KB_ROOT` and exposes:

- `kb_status(project, verify_runtime?)`: selected binding, validation, optional packaged-runtime check and the current working-skill locator.
- `kb_setup(project, mode, folder?, replace?, allow_separate_store?)`: explicit data selection; no project-instruction writes.
- `kb_core(project, budget?, expect_revision?)`: full core, selected/evidence roots and a completeness indicator. Insufficient budgets fail explicitly.
- `kb_context`, `kb_search`, `kb_get`, `kb_validate`, `kb_draft`, `kb_preview`, `kb_apply`: ordinary retrieval and reviewed updates.

The agent performs the optional project-entry edit with the host's normal file tools, only after consent. Cancellation of a folder picker is a no-op. Changing a selection needs explicit replacement; neither KB is deleted. Keep `.fist-kb.json` out of shared commits because it contains local paths.

## Direct MCP and fixed bindings

```text
uv run --locked fist-kb --project-mode mcp
uv run --locked fist-kb --kb /absolute/project/KnowledgeBase mcp
```

In project mode, pass the project on every knowledge call. In fixed mode, omit it: `kb_status` reports the same fixed KB used by all reads/writes, and supplying a project is rejected. `kb_setup` rejects fixed-mode changes without touching project selections. Configure a different connection explicitly to switch the fixed root.

Direct Python MCP can be configured on other Python platforms; the shipped desktop launcher and picker are Windows-only. On a host without a picker, supply the folder in chat. There is no HTTP daemon and no published PyPI package assumed by these examples.

## Check an installation

Verify package integrity once after installation/update and on relevant recovery, then load full core and read context. In disposable data, check a reviewed write, restart and retrieval, two project bindings, fixed-mode status, cancellation and explicit separate-store boundaries. Source registration/preparation uses the [CLI evidence reference](EVIDENCE.md).

Keep service checks separate from actual agent behavior. A complete host check also verifies that a fresh session shows the hook's core for the selected project and that it reappears after compaction, that a fresh ordinary request loads the operating contract, a correction is recovered, and compaction preserves the current objective and stop boundary. See [validation scope](../VALIDATION.md); tool discovery does not prove those behaviors.

Official loading references: [OpenAI skill activation](https://developers.openai.com/plugins/concepts/skills), [Codex project instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [Claude Code project memory](https://code.claude.com/docs/en/memory), and [local plugin packaging](https://developers.openai.com/plugins/build/plugins).
