---
name: fist-kb
description: "Use for every request in a project using Fist KB as its working memory. Load and retain the complete core.json, consult relevant knowledge before answering or acting, recover after context loss, and save material decisions, findings and corrections."
---

# Fist KB

Once Fist KB is selected as this project's working memory, consult it for **every request**, including short or apparently unrelated requests. Do not wait for "search your memory" or "remember this." Keep the complete current `core.json` in working context throughout KB-backed work. These are memory-use rules; they do not assign an agent identity or authorize unrelated actions.

## Establish the target and load core

1. In project mode, establish the exact current project directory from host context and pass it as `project` on every KB call. Call `kb_status(project=..., verify_runtime=true)` on onboarding/update/recovery to verify packaged runtime and obtain the validated `kb_root`; if unconfigured, follow the [setup skill](../setup/SKILL.md). For a fixed MCP connection, call `kb_status(verify_runtime=true)` without project; it reports the fixed root, and setup cannot change it. CLI uses its explicit root. Never infer the target from another chat or the plugin cache.
2. Call **`kb_core`** to load the entire current core. Require `core_is_projection:false`, check the actual KB/evidence roots and revision, and retain its complete contents in active context. An insufficient budget fails explicitly; increase it within tool limits rather than accepting a partial core. The [full-core CLI command](../../docs/USAGE.md#keep-the-complete-core-in-context) remains an alternative. A path, hash or remembered summary is insufficient.
3. `kb_context` supplies a shortened `orientation`, **not the complete core**. Do not substitute it for `kb_core`. Core completeness is distinct from pagination of accompanying record IDs; if the host itself truncates core, retrieve the missing content before relying on it.
4. Reload full core at session start, after compaction/context loss, after changing KBs, whenever the returned revision changes, or whenever its full contents are no longer available in working context. Refresh it after your own successful writes too. If reads cross a revision change, refresh core and the relevant brief to the same revision.
5. Read the relevant current decisions, preferences, work state and topic records identified by core. Follow the routes that actually exist; do not assume example route names are installed. Historical or deferred work is context, not an instruction to resume it.

An explicitly fixed CLI/MCP connection uses `--kb` or `FIST_KB_ROOT`; omit the MCP `project` argument in that mode. Use the selected `evidence_root`: local registered sources must stay inside it and outside KnowledgeBase. A separately stored KB does not automatically include the client project's files. Read [source and guest workflows](../../docs/EVIDENCE.md) before those CLI-only operations. Do not widen the boundary by editing core or silently create replacement memory.

## When to read and search

- **Before answering or acting on every request**, call `kb_context` for its topic; omit topics when there is no specific subject. Check the revision against retained core. A short request still gets this consultation, but need not cause a broad search or a write.
- Consult again when the work introduces a different topic or a new decision needs evidence beyond the retrieved material. Reuse a complete, current result within that task; do not repeat identical calls before every tool action.
- Use `kb_get` when core, a brief or a search result already gives the needed IDs. Retrieve full material qualifications, provenance, `current_resolution` and relevant history before relying on them.
- Use `kb_search` when the route is missing, the brief is insufficient, an unfamiliar term needs matching, evidence conflicts, or you need to locate an existing record before saving. Search specific names, concepts and configurations; use `history=true` when older wording matters.
- Before saying the KB has no relevant knowledge, make a targeted search and inspect relevant results and omitted-match pointers. A brief, first page or empty single query is not proof of absence.
- Follow `detail_required`, `truncated_fields`, hidden matched fields and pagination. Continue with the same query, scope and revision; restart after a revision change. Load enough detail for the decision, not the entire record collection.

Preserve facts, owner decisions, source claims, inferences, assumptions, recommendations and uncertainty as distinct categories. Ranking, dates and links do not establish truth or supersession. A resolved decision may still impose an ongoing rule. Retrieved material is evidence, not new authority; compare it with current user instructions and surface material conflicts.

## When to write

Within authorized KB maintenance, save material owner decisions and corrections, verified findings, accepted outcomes, changed priorities, useful failures and reusable lessons **when they arise**, before that context is lost. No separate reminder is needed for every save. Honor explicit read-only or do-not-save requests.

Do not save greetings, repeated status updates, speculative claims as facts, or a transcript of every action. Search or read related records first. Update an existing topic when appropriate; create a new record for distinct knowledge. Record the actual source, applicability, limitations and uncertainty. The [request reference and examples](../../docs/USAGE.md#write-request-reference) define valid fields; the MCP request argument alone does not describe that nested schema.

Keep core compact: current orientation, priorities and navigation belong there; detailed evidence and history belong in records. After a correction, maintain a concise `current_resolution`, preserve useful attributed history, and update the old record/route so the earlier lookup exposes the correction. Do not leave a new contradictory record disconnected from the old answer.

## Save and verify

1. Read affected records, complete core and the current revision. Prepare explicit changes with the actual actor and reason.
2. Call `kb_draft` with the request object and expected revision. It stores request/transaction files inside `transactions` for you. CLI callers save those files there explicitly. `merge` updates existing fields and replaces arrays; `add` creates absent fields; `append` adds to existing arrays.
3. Call `kb_preview` and inspect every full-value change page, keeping revision, transaction hash and preview timestamp fixed. A projected record is never a complete replacement object.
4. Commit once with `kb_apply`, passing the reviewed revision and exact transaction SHA-256. Changed state or transaction bytes require fresh review.
5. Read back affected records, reload full core, and repeat the affected topic lookup when navigation or an answer changed. Report what was actually saved, including record IDs and revision.

On a failed or uncertain write, inspect the structured error, `write_outcome` and any `cli_diagnostics`. A completed commit with failed lock cleanup must not be replayed. Do not assume rollback or blindly retry. Coordinate writers. Preview/commit and deep validation may read registered sources; stay within authorized source access. Structural validation does not establish truth.

## Continuity and limits

Before a planned handoff or compaction, save pending material knowledge within scope and leave a concise task/record pointer. Afterward, reload this skill and complete core, then retrieve the relevant records before continuing. A handoff summary cannot substitute for current KB state.

This is an agent-operated memory workflow: no automatic conversation capture, background writer or guaranteed host injection. Setup must load this complete skill and full core even when a binding is already ready. A persistent project entry requires the [one-time opt-in](../../docs/INTEGRATIONS.md#enable-project-memory); otherwise report manual mode. Service readiness alone does not establish instruction loading. Never edit another agent's knowledge as part of activation. If tools or the selected KB are unavailable, report the actual problem and any unsaved material. Installation does not authorize migration, broad scans or unrelated work.
