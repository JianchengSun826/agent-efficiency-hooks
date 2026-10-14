# Agent efficiency hooks

## Goal

Reduce repeated reads and unchanged failing retries without changing ordinary user prompts. Share a deterministic Python standard-library engine between Codex, Claude Code, and custom harnesses. No LLM calls, automatic code cleanup, or automatic session restart.

## Packaging

One CLI receives JSON on stdin and selects an explicit adapter: codex, claude, or harness. Native adapters normalize events and translate decisions. Harness callers send normalized before/after events and consume allow/warn/deny decisions. Hook registration is platform-specific; the engine is shared.

## Read detection

Support structured local file reads with path and range, plus narrowly parsed single cat/head/tail/sed commands. Unknown shell syntax, pipelines, substitutions, remote tools, and dynamic commands pass through. Never execute commands to discover their meaning. Record a successful completed read, not just an attempted call. Key records by platform, session, agent identity where provided, canonical cwd/path, range, and content fingerprint. Limit fingerprint cost; skip oversized or inaccessible files. Expire records and reset on a supported compaction/session boundary so old context is not assumed available.

Repeated unchanged reads generate one short warning after the configured threshold. Denial is opt-in and has a bounded cooldown/escape; defaults must not trap the agent if prior content is no longer available.

## Retry detection

Track explicitly configured deterministic verification commands only. Normalize tool failures and command exit status separately. Do not count successful commands, incomplete PTY sessions, polling, interrupts, hook denials, network operations, or unclassified output as failures. Compare command, cwd, configured relevant input fingerprints, and error signature. Clear streak after relevant changes or success. Emit one warning after two matching failures; optional denial applies before a subsequent identical attempt. No automatic rewrite or execution of alternatives.

Inputs must be configured: guessing dependencies or hashing the whole repository on every call would be inaccurate and expensive. Without dependable input coverage, warnings only.

## State and overhead

Use a local SQLite store outside repositories, transactional updates, bounded retention, and hashed identifiers. Do not persist raw command output or file contents. Silence normal events; fail open on malformed events or state failures. Keep diagnostics outside model context. Avoid repository-wide scans. Provide observe, warn (default), and enforce modes plus reset and statistics commands. Metrics distinguish events, warnings and denied calls from actual token savings, which require host usage measurements.

## Installation

Generate snippets with a quoted absolute Python/script path. Installer merges only this package's uniquely identified registrations, preserves existing hooks, saves a backup, and supports uninstall and project/user scope. Preview generated config before applying it.

Codex: register PreToolUse and PostToolUse plus supported lifecycle resets in .codex/hooks.json; use current documented schemas and host trust flow. Claude Code: register PreToolUse, PostToolUse, PostToolUseFailure and lifecycle resets in .claude/settings.json or settings.local.json. Configuration files cannot be reused verbatim between platforms.

Harness: call engine before tool dispatch, honor deny without executing, then report final success/failure after completion. Supply stable session/agent/call IDs, cwd, normalized read details or verification command, input fingerprints, and result status/error signature. A harness with no interception interface needs changes to its tool dispatcher or a wrapper; merely copying config cannot integrate it.

## Verification

Fixture tests for each native adapter; behavioral tests for unchanged/changed reads, failed read exclusion, success/reset, changed inputs, transient failures, unsupported shell forms, compaction, concurrent state updates and configuration merge/uninstall. Local simulated end-to-end tests are distinct from live host validation. Benchmark hook overhead and observed redundancy before claiming savings.

## Hermes Agent target

Support NousResearch Hermes Agent through a named adapter rather than requiring changes to its core. Hermes shell hooks register under the active profile config.yaml hooks block; pre_tool_call handles interception and post_tool_call observes completion. Warnings are queued for pre_llm_call at the next eligible turn. Normalize the shell envelope's tool_input and extra fields and use stable session identities rather than rotating task IDs. Gateway-only ~/.hermes/hooks handlers are not the tool interception target. Default to skipping Hermes read_file when its built-in dedup handles repeated reads. Terminal monitoring requires confirmed local execution and an explicit absolute workdir. Hermes provider credentials do not imply Codex CLI or Claude Code hooks are loaded. Native hooks are needed separately when Hermes delegates work to those runtimes.

## First release

Implement shared engine, Codex and Claude adapters, generic harness contract, installer, documentation and tests. Default warn mode. Start with structured reads and conservative shell patterns; retry monitoring requires explicit command/input configuration. Windows locking/path behavior is not included until separately validated.
