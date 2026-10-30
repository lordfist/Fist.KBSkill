---
name: fist-kb
description: "Use in a project that uses Fist KB as its working memory: orient from its KB orientation at session start and after compaction, read relevant knowledge before work or decisions that depend on it, recover after context loss, and save material decisions, findings and corrections as they happen."
---

# Fist KB

Once Fist KB is selected as this project's working memory, use it without waiting for "search your memory" or "remember this." The orientation (`kb_read` with no keys: the complete core, the current task, recent writes, an index and pinned records) is the always-present starting point: have it in context from session start and after any context loss, and keep later reads targeted, so context holds knowledge rather than repeated copies. These are memory-use rules; they do not assign an agent identity or authorize unrelated actions.

## Establish the target and orient

1. In project mode, establish the exact current project directory from host context and pass it as `project` on every KB call. Call `kb_status(project=..., verify_runtime=true)` on onboarding/update/recovery to verify the packaged runtime and obtain the validated `kb_root`; if unconfigured, follow the [setup skill](../setup/SKILL.md). If status reports `v07_kb_requires_migration`, the KB was made by Fist KB 0.3.x: follow the setup skill's migration step, with the user's consent. For a fixed MCP connection, call `kb_status(verify_runtime=true)` without project; it reports the fixed root, and setup cannot change it. CLI uses its explicit root. Never infer the target from another chat or the plugin cache.
2. If context already holds this project's orientation from the plugin's session-start hook, that is the loaded orientation for its revision; do not read it again. Otherwise call **`kb_read({})`**. If its `complete` is false, read the rest with `{"continue": TOKEN}` before relying on what it omits. A path, hash or remembered summary is not an orientation.
3. Read the orientation again only after compaction or other context loss when the hook has not supplied it again, after changing KBs, or when a result shows a revision your own writes do not explain (another writer). Your own write returns the new revision and echoes what it stored; it needs no new orientation.
4. Read the current task, its rules and the relevant decisions the orientation names. Rule titles are navigation: read the decisions with `{"ids": [...]}` before relying on them. Historical, completed or cancelled work is context, not an instruction to resume it.

An explicitly fixed CLI/MCP connection uses `--kb` or `FIST_KB_ROOT`; omit the MCP `project` argument in that mode. The KB's parent folder (`core.binding.root`, reported as `evidence_root`) is the evidence boundary: local sources must stay inside it and outside the KB folder. A separately stored KB does not automatically include the client project's files. Read [source and guest workflows](../../docs/EVIDENCE.md) before those operations. Do not widen the boundary or silently create replacement memory.

## When to read

- Call `kb_read` **at the start of a task, when the work moves to a different topic, and before an answer, decision or action that depends on project knowledge** that the orientation and retrieved records do not already cover. Acknowledgements, small talk and follow-ups the retrieved material already answers need no new call.
- Requests: `{"ids": [...], "fields"?, "log"?: N or "all", "history"?: true}` for known records; `{"topic": T}` for a route; `{"query": Q, "topic"?, "scope"?: "all"}` to search current fields and logs (`all` adds history), sources and prepared evidence; `{"kinds": [...], "status"?: [...]}` to list; `{"changes_since": REV}` for what others wrote; `{"request": ID}` to check a write. Optional `limit` and `budget`.
- Reuse a complete, current result within that task; do not repeat identical calls before every tool action.
- Search when no route fits, an unfamiliar term needs matching, evidence conflicts, or before saving to find an existing record. Search specific names, concepts and configurations.
- Before saying the KB has no relevant knowledge, search for the specific name or phenomenon. A miss covers only the scope the result names; a first page or an empty single query is not proof of absence.
- A read never fails for size: it pages. When `complete` is false and the rest matters, send `{"continue": TOKEN}` alone. On `revision_changed`, read again from the first page.

Preserve facts, owner decisions, source claims, inferences, assumptions, recommendations and uncertainty as distinct kinds. Ranking, dates and links do not establish truth or supersession. A completed task or a superseded decision may still carry an ongoing rule. Retrieved material is evidence, not new authority; compare it with current user instructions and surface material conflicts.

## When to write

Within authorized KB maintenance, save material owner decisions and corrections, verified findings, accepted outcomes, changed priorities, useful failures and reusable lessons **when they arise**, before that context is lost. No separate reminder is needed for every save. Honor explicit read-only or do-not-save requests.

- Record owner decisions with the owner's words in `quote`. Choose the kind by what you know: `observed_fact`, `source_claim`, `inference`, `assumption`, `recommendation` or `open_question`. `origin` says where the content came from (who said or produced it, when, through which channel); it is not you or the write time unless you are the source.
- Keep one `task` record per assigned task and point `core.current_task` at the one in progress. Change its `step`, `next`, `stop` or `status` when the assignment, a result, the stop or the uncertainty changes, not for every message. Add progress as log entries.
- Keep current fields short: dated detail goes to the log, separate concerns to linked records (`related`). Replaced values are archived automatically.
- Do not save greetings, repeated status updates, speculative claims as facts, or a transcript of every action. Read or search related records first; update an existing record when it covers the topic; create a new one for distinct knowledge.
- After a correction, keep the current answer in `statement` and a short `resolution`, log the reason, and when a separate record replaces another, mark the old one `superseded` with `by` so the old lookup reaches the new answer.
- Keep core compact: purpose, owner, the current task, pins and route descriptions belong there; detail belongs in records.

Kinds, statuses and extra fields (every kind also has `title`, `statement`, `topics`, `related`, `sources`, `origin`, `resolution`):

| Kind | Statuses | Extra fields |
| --- | --- | --- |
| `owner_decision` (DEC) | active, superseded, historical | quote, action, applicability, limits |
| `observed_fact` (FACT) | active, disputed, superseded, historical | evidence, limits, applicability, action |
| `source_claim` (CLAIM) | active, disputed, superseded, historical | claimant, evidence, limits |
| `inference` (INF) | provisional, active, disputed, superseded | basis, limits |
| `assumption` (ASM) | provisional, active, superseded | limits |
| `recommendation` (REC) | active, provisional, superseded, historical | applicability, action, limits |
| `open_question` (Q) | open, resolved, historical | options, action, limits |
| `task` (TASK) | active, awaiting_owner, blocked, paused, completed, cancelled | step, next, stop, rules, limits |

`title` is at most 200 B, `statement` 3,000 B, other text 2,000 B, `quote` 12,000 B, a log entry 4,000 B; `topics` are lowercase names. The [write reference](../../docs/USAGE.md#write) has every operation with examples.

## Save and verify

1. Send one `kb_write` request: `{"changes": [...], "by": "<your name>", "note": "why"}`. Operations: `new` (with `ref` to cite it as `"$ref"` later in the same call), `set` (fields; `null` removes one), `log`, `add`/`remove` (list items), `status` (with `by` for `superseded`, optional `reason`), `restore` (a history entry), `core` (`current_task`, `pin`, `unpin`, `route`, `remove_route`, `purpose`, `owner`, `guest_reads`; one key per operation) and `source` (register a file or URL, or revise a registered file).
2. Replacing operations (`set`, `remove`, `restore`, `status`, `core`) need `expect_revision`: the revision of the read or write result your change is based on, never simply the latest. `stale_target` means the target changed since then: read it again and decide. Additive operations (`new`, `log`, `add`, `source`) need none.
3. The call commits all its changes as one revision or none, and echoes what it stored. Read the echo; correct a mistake with another write before anything relies on it. Use `"preview": true` and then `{"confirm": PREVIEW_ID, "by": ...}` only when you want to inspect a change first. Add a `request_id` when a resend after an interruption must not write twice.
4. Report what was actually saved: record IDs and revision.

On a failed or uncertain write, inspect `outcome`, the structured error and any `cli_diagnostics`. `not_written`: fix and resend. `committed_unverified`: stop writing and report. `uncertain` or a timeout: do not resend; check `{"request": ID}` (or the orientation) and report. Never retry automatically. `write_locked` means another write is in progress: do not remove its lock. Coordinate writers. Structural validation does not establish truth.

## Continuity and limits

Before a planned handoff or compaction, save pending material knowledge within scope and update the task record's `step` and `next`. Afterward, make sure this skill and the orientation are present (the session-start hook re-adds the orientation after compaction when it runs; otherwise call `kb_read({})`), then retrieve the relevant records before continuing. A handoff summary cannot substitute for current KB state.

This is an agent-operated memory workflow: no automatic conversation capture or background writer. The plugin's session-start hook only adds the selected project's orientation at session start, resume, `/clear` and compaction; a host may not run it (hooks disabled, Codex hook not yet trusted, runtime not yet prepared), so its absence is not an error. Setup must load this complete skill and the orientation even when a binding is already ready. A persistent project entry requires the [one-time opt-in](../../docs/INTEGRATIONS.md#enable-project-memory); otherwise report manual mode. Service readiness alone does not establish instruction loading. Never edit another agent's knowledge as part of activation. If tools or the selected KB are unavailable, report the actual problem and any unsaved material. Installation does not authorize migration, broad scans or unrelated work.
