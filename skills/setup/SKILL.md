---
name: setup
description: Set up Fist KB, choose or recover its data folder, load the working-memory contract, and optionally enable persistent project memory.
---

# Set up Fist KB

Keep setup short, but distinguish a connected service from a loaded working-memory workflow.

1. Establish the exact current project directory from host context. Ask if ambiguous. Never use a plugin/cache/runtime directory as project data.
2. Call `kb_status(project=..., verify_runtime=true)`. Inspect the runtime verification. If already ready, keep that selection and **continue to step 5**; do not stop at service readiness. A fixed connection uses `kb_status(verify_runtime=true)` without project and cannot be changed through `kb_setup`.
3. If unconfigured, ask whether to create or use an existing KB unless already specified. Recommend the actual project directory as the parent of `KnowledgeBase` when project files will be registered as evidence.
4. Warn before opening the folder picker, then call `kb_setup` with the project and mode, omitting folder unless supplied. Cancellation stops without changes. Use `replace=true` only for an explicitly requested selection change. If setup reports `storage_scope_confirmation_required`, explain its exact evidence root: files outside the selected KB's parent cannot be registered or prepared. Offer the project folder or a separate store; pass `allow_separate_store=true` only after that boundary is accepted. Never silently retry an uncertain create.
5. **Load the complete [working skill](../fist-kb/SKILL.md)** through the host, this package-relative reference, or the current `operating_skill_path` returned by status. A description or remembered summary is insufficient. Then use the complete core: this project's session-start hook output at the current revision, or `kb_core` with `core_is_projection:false`. Load relevant decisions/current work through `kb_context` and necessary `kb_get` calls. Report missing/truncated reads; do not claim orientation from brief context alone.
6. In project mode, offer the [one-time project-memory opt-in](../../docs/INTEGRATIONS.md#enable-project-memory) unless that choice was already made. Show the exact instruction-file target and small delimited block before requesting consent. On acceptance, preserve existing instructions and add/update only that block using the host's ordinary file tools. Do not add a second memory system over conflicting existing instructions. If declined or file access is unavailable, keep explicit manual-use mode. Fixed connections also stay in manual mode here: do not install the project-mode block, which depends on a project selection file.
7. Read back any changed entry and its effective location; check override/loading limits. Confirm the selected KB, evidence boundary, loaded operating skill/core revision and mode. Say **service connected** for tool success, **working-memory workflow loaded in this session** after step 5, and **persistent entry installed** only after its verified write. Fresh-session automatic loading is a separate check; never report it as proven by file creation. In new sessions the plugin's session-start hook adds the selected core at start and after compaction once the runtime is prepared; Codex runs it only after the user reviews and trusts it. Report the hook as working only after a fresh session shows its output.

An existing entry that matches the current template needs no repeated consent or rewrite; for an older block, show the changed text and update it only with consent. On later setup/recovery, load the working skill, verify runtime as relevant, load full core as above, and retrieve current records.

No global active KB exists. Project-mode calls always carry the current project. Setup does not migrate knowledge or change agent identity. First launch may need internet to prepare the private Windows runtime. If startup fails, report the actual failure rather than inventing a replacement memory location.
