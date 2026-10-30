# Changelog

## 0.4.0

- Replace the KB engine with the Seed KB v08 engine. Each record kind has a closed field set with size limits, every record has a dated log and an automatic history of replaced values, `task` records hold the current step, next action and stop, and routes are formed by record topics. **Not backward compatible:** a 0.3.x KB is read only after a one-time migration.
- Two knowledge tools replace eight: `kb_read` (orientation, known records, routes, search, lists, changes and write checks; large results page instead of failing) and `kb_write` (one call commits, verifies and echoes what was stored; IDs are allocated by the tool; optional preview and confirm; `expect_revision` is checked per target for replacing operations; a `request_id` makes a resend safe). `kb_core`, `kb_context`, `kb_search`, `kb_get`, `kb_validate`, `kb_draft`, `kb_preview` and `kb_apply` are removed; `kb_status` and `kb_setup` remain.
- Upgrade path: `kb_status` and the session-start hook report a 0.3.x KB as `v07_kb_requires_migration`. With consent, `kb_setup` mode `migrate` (or `fist-kb admin migrate-v07`) copies it into a new sibling folder after checking every old field, selects the new folder and leaves the old KB unchanged. A null 0.3.x purpose migrates as empty text.
- The session-start hook adds the orientation (core, current task, recent writes, index and pinned records) within 6,000 bytes, or its first page with a continue token.
- Sources are registered and revised with `kb_write`; preparation, deep validation and guest reads stay on the CLI. The guest reader is interface 2: any `kb_read` request, with record fingerprints. Checkpoint comparison and request/transaction files are removed.
- CLI: `read` and `write` (JSON text, `@file` or `-` for stdin), `admin validate|verify-tools|migrate-v07|recover|export`, `prepare` and `guest`; `init` accepts `--purpose` and `--owner`. Every read and write checks the installed package files against the package manifest, and writes are refused on a mismatch.
- The project-memory block template now names `kb_read`. Projects with an older block keep its wording until setup updates that block with consent.

## 0.3.0

- Order search, context, brief and evidence results by match quality: a whole word (or its plural or word form) before the start of a longer word before text inside a word, and records matching more of the query words first. `guard` now lists "guard clauses" before "safeguards"; a hash prefix and `plan` -> `planning` still match.
- Give the full route bonus only when every query word names the route. One route word in a longer query ("malformed tool input" with a `kb-tools` route) no longer lifts the whole route; its records stay in the results, ordered by their content.
- Matches are unchanged: the same records are found as in 0.2.3, only their order and scores change. A test pins the 0.2.3 match sets.

## 0.2.3

- Add a read-only session-start hook for Claude Code and Codex (Windows): at session start, resume, `/clear` and after compaction it adds the selected project's complete core to context, within 6,000 bytes, or a marked summary when core is larger. Nothing is added on other turns or for projects without a KB selection. Codex runs it after the user trusts it.
- Replace "consult kb_context before every request" with consultation at task start, on topic changes and before knowledge-dependent decisions. Reload core after context loss, a KB switch or another writer's revision change, and after your own write only when it edited core content.
- Run engine and API child processes with `-P`, so a module such as `json.py` in the caller's working directory can no longer replace a standard-library import.
- Add `fist-kb session-context`. Projects that installed the 0.2.2 project-memory block keep its every-request wording until setup updates that block with consent.

## 0.2.2

- Add complete, bounded, revision-aware core retrieval through MCP.
- Make setup load the working skill and relevant knowledge even for existing bindings; offer a scoped, optional persistent project-memory entry.
- Explain and require acknowledgement of separate-store evidence boundaries.
- Make fixed-mode status and setup consistent with the fixed KB.
- Preserve bounded CLI cleanup diagnostics and completed-write outcomes at the API/MCP boundary.
- Add runtime verification to status and a discoverable CLI evidence/guest reference.

## 0.2.1

- Clarify the public introduction and getting-started documentation.
- Require KB consultation for every request and complete core retention/recovery.
- Explain when to read, search, save and maintain corrections.
- Add verified examples for record creation, correction and topic-route updates.
- Repackage the shared plugin and Python wheel; the KB engine is unchanged.

## 0.2.0

- Start the plugin before a KB exists; guided setup selects or creates it.
- Remember one explicit selection per project in a small local file.
- Add a Windows folder picker and private uv/runtime preparation.
- Preserve direct CLI and fixed-MCP bindings, reviewed writes and existing data.
- Refuse silent adoption of evolved Seed KBs.
- Keep one source tree and one shared plugin package; remove expanded per-client copies and the redundant source archive.

## 0.1.0

Initial public package candidate: standalone Python CLI/API, local MCP, thin client wrappers, typed records and reviewed transactions. Provenance is retained in PROVENANCE.json.
