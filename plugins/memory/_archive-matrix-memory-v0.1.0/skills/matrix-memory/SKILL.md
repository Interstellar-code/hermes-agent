---
name: matrix-memory
description: "Operate Matrix Memory safely with setup and recall workflows."
version: 1.0.0
author: Hermes Agent
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [memory, matrix-memory, recall, setup, retrieval]
    category: memory
    related_skills: []
---

# Matrix Memory Skill

Use this skill when Hermes is configured to use the bundled Matrix Memory provider and the task is to set it up, inspect its recall surface, or operate its memory workflow safely. This skill covers configuration, expected tool usage, and validation steps. It does not implement or modify provider code.

## When to Use

- The active memory provider is Matrix Memory.
- A user asks how to configure Matrix Memory for Hermes.
- A task needs Matrix Memory recall or fact-storage workflows.
- You need to explain or validate the provider's expected usage without editing provider internals.

## Prerequisites

- The Matrix Memory plugin is present in the Hermes checkout.
- Hermes is configured to use the Matrix Memory memory provider.
- Any Matrix Memory provider-specific credentials or endpoint settings required by the plugin are already available in `config.yaml` or `~/.hermes/.env`.
- The relevant Matrix Memory tools are enabled by the active provider configuration.

## How to Run

1. Confirm Hermes is using the Matrix Memory provider.
2. Inspect the active provider configuration before making claims about setup.
3. Use the provider's exposed memory tools rather than ad-hoc shell commands.
4. Validate behavior with a minimal read/write or recall flow when the task requires proof.

## Quick Reference

| Task | Action |
|------|--------|
| Confirm provider | Check `memory.provider` configuration |
| Review plugin notes | Read `README.md` in this skill folder first |
| Inspect recall | Use the Matrix Memory search/profile/context tools exposed by the provider |
| Store a fact | Use the provider's explicit memory-write tool |
| Validate setup | Run the smallest safe recall test and report the result |

## Procedure

1. **Confirm scope**
   - Stay inside documentation and operational guidance unless the user explicitly asks for provider-code changes.
   - Do not invent Matrix Memory capabilities that are not exposed by the installed provider.

2. **Verify configuration**
   - Check that Hermes is configured to use Matrix Memory as the active memory provider.
   - Identify any required environment variables or config fields from the plugin documentation before answering setup questions.

3. **Use provider tools directly**
   - For recall, prefer the provider's search, profile, or context tools.
   - For persistence, use the provider's explicit conclude/store/write tool if one exists.
   - Keep tool usage minimal and targeted to the user's request.

4. **Validate with evidence**
   - If the user asks whether Matrix Memory works, perform one small verification path: store a simple fact or query known context, then confirm the result.
   - Report what was validated and what remains unverified.

5. **Document gaps honestly**
   - If the provider is not configured, say exactly what is missing.
   - If the provider is configured but the tool surface is unavailable in the current session, explain that as the blocker.

## Pitfalls

- Do not assume Matrix Memory is active just because the plugin exists in the repo.
- Do not describe guessed environment variables, endpoints, or tool names as facts.
- Do not bypass provider tools with direct database or filesystem edits.
- Do not claim persistence or retrieval succeeded without fresh evidence.

## Verification

- Confirm the active provider selection before setup guidance.
- Confirm any cited config keys or environment requirements from local plugin docs.
- When validating runtime behavior, use a minimal provider-tool interaction and report the exact outcome.
