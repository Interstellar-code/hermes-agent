<p align="center">
  <img src="assets/banner.png" alt="Hermes Agent" width="100%">
</p>

# Hermes Agent ☤ — Interstellar fork
<p align="center">
  <a href="https://hermes-switchui.zi0n.space/">Hermes SwitchUI</a> |
  <a href="https://hermes-switchui.zi0n.space/docs/welcome/">SwitchUI Docs</a> |
  <a href="https://github.com/NousResearch/hermes-agent">Upstream Hermes</a>
</p>
<p align="center">
  <a href="https://hermes-switchui.zi0n.space/"><img src="https://img.shields.io/badge/SwitchUI-Live%20Site-18A558?style=for-the-badge" alt="Hermes SwitchUI website"></a>
  <a href="https://github.com/Interstellar-code/hermes-agent"><img src="https://img.shields.io/badge/Fork-Interstellar--code-7B61FF?style=for-the-badge&logo=github" alt="Interstellar-code Hermes Agent fork"></a>
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Core%20Docs-Upstream-FFD700?style=for-the-badge" alt="Upstream Hermes documentation"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
</p>

This is [Interstellar-code's](https://github.com/Interstellar-code) maintained fork of
[Hermes Agent](https://github.com/NousResearch/hermes-agent), the open-source AI agent
built by [Nous Research](https://nousresearch.com). It tracks the upstream runtime while
adding the self-hosted [Hermes SwitchUI](https://hermes-switchui.zi0n.space/), multi-agent
coordination, visual workflows, projects, personas, and fork-specific operational plugins.

---

## Hermes Agent + SwitchUI

[Hermes SwitchUI](https://hermes-switchui.zi0n.space/) is the self-hosted browser
workspace for this fork. It is a separate frontend repository, not a replacement for
the Hermes runtime.

| Layer | Responsibility |
| --- | --- |
| **Hermes Agent fork** | Models, conversations, tools, memory, sessions, gateway, scheduling, and persistence |
| **Hermes SwitchUI** | Browser chat and workspace, files, terminal, projects, boards, workflows, operations, and plugin views |
| **Plugins** | Interstellar capabilities such as A2A fleet coordination, visual workflows, projects, personas, and Matrix Memory |

- **Website:** [hermes-switchui.zi0n.space](https://hermes-switchui.zi0n.space/)
- **Documentation:** [hermes-switchui.zi0n.space/docs](https://hermes-switchui.zi0n.space/docs/welcome/)
- **Source:** [Interstellar-code/hermes-switchui](https://github.com/Interstellar-code/hermes-switchui)

---

## Quick Install

### SwitchUI + this Hermes fork (recommended)

The SwitchUI installer installs the required Interstellar Hermes fork first, then sets
up the browser workspace. Supported by the installer on Linux, macOS, and WSL2.

```bash
curl -fsSL https://raw.githubusercontent.com/Interstellar-code/hermes-switchui/main/install.sh | bash
```

### Hermes Agent fork only

#### Linux, macOS, WSL2, Termux

```bash
curl -fsSL https://raw.githubusercontent.com/Interstellar-code/hermes-agent/main/scripts/install.sh | bash
```

#### Windows (native, PowerShell)

Run this in PowerShell:

```powershell
iex (irm https://raw.githubusercontent.com/Interstellar-code/hermes-agent/main/scripts/install.ps1)
```

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes              # start chatting!
```

---

## Getting Started

```bash
hermes              # Interactive CLI — start a conversation
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes config get   # Print individual config values
hermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)
hermes setup        # Run the full setup wizard
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

📖 **[SwitchUI documentation](https://hermes-switchui.zi0n.space/docs/welcome/)** ·
**[Upstream Hermes documentation](https://hermes-agent.nousresearch.com/docs/)**

---

## Interstellar extensions

These are additions maintained in this fork. They build on Hermes' plugin and service
surfaces so the upstream core can continue to move forward without turning every
extension into a permanent model-tool cost.

### Supported extensions

| Extension | What it adds |
| --- | --- |
| [**Hermes SwitchUI bridge**](plugins/hermes-switch-ui/README.md) | Authenticated frontend registration, heartbeat/status, settings sync, agent tools, and the backend API used by the separate SwitchUI workspace. |
| [**Projects**](plugins/projects/) | Named multi-folder workspaces, CLI and REST management, project activity, and explicit per-session project binding. |
| [**A2A Fleet**](plugins/a2a_fleet/README.md) | Hermes-to-Hermes peering plus managed Claude Code, OpenCode, Codex, and Antigravity executor deployment. Executor capabilities depend on the installed CLI and its authentication. |
| [**Workflow Engine**](plugins/workflow-engine/README.md) | YAML-defined DAG workflows with branching, parallel nodes, approval gates, cron polling, events, agent tools, and Kanban dispatch. The visual editor lives in SwitchUI. |
| [**Personas**](plugins/personas/README.md) | A canonical library of 20 personas, runtime list/get/apply tools, trusted `persona_ref` overlays, and a read API. SwitchUI wizard migration and write-side promotion remain follow-up work. |
| **Kanban extensions** | Board templates, scheduled tasks and board creation, project-linked tasks, dispatcher workflows, and restored dashboard APIs on top of Hermes Kanban. |

### Optional and transitional

| Extension | Status |
| --- | --- |
| [**Matrix Memory**](plugins/memory/matrix-memory/) | Optional Mnemosyne-backed vector, knowledge-graph, temporal, and wiki-bridge memory provider. Requires its pinned engine dependency and deliberate per-profile data-path configuration. |
| [**MCP Lazy**](plugins/mcp_lazy/README.md) | Fork-local compatibility layer that defers MCP schemas and promotes them on demand. Savings are workload-dependent; prefer upstream `tool_search` when it works for your provider configuration. |

`karpathy-self-improve` and `matrix_coder` have been retired and are no longer shipped.

---

## Documentation

### Interstellar fork and SwitchUI

| Resource | What's covered |
| --- | --- |
| [SwitchUI website](https://hermes-switchui.zi0n.space/) | Product overview, architecture, and the self-hosted browser workspace |
| [SwitchUI documentation](https://hermes-switchui.zi0n.space/docs/welcome/) | Installation, chat, files, terminal, projects, boards, workflows, operations, plugins, and troubleshooting |
| [Fork issues](https://github.com/Interstellar-code/hermes-agent/issues) | Bugs and feature requests specific to this repository |
| [Plugin READMEs](plugins/) | Implementation, configuration, limitations, and operational guidance for fork extensions |

---

## Contributing

We welcome contributions to this fork. Search the
[fork issues](https://github.com/Interstellar-code/hermes-agent/issues) and
[pull requests](https://github.com/Interstellar-code/hermes-agent/pulls) first.

Quick start for contributors:

```bash
curl -fsSL https://raw.githubusercontent.com/Interstellar-code/hermes-agent/main/scripts/install.sh | bash
cd "${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

---

## Community

- 🌐 [Hermes SwitchUI website](https://hermes-switchui.zi0n.space/)
- 📚 [SwitchUI documentation](https://hermes-switchui.zi0n.space/docs/welcome/)
- 🐛 [Interstellar fork issues](https://github.com/Interstellar-code/hermes-agent/issues)
- 💬 [Nous Research Discord](https://discord.gg/NousResearch) — upstream/community discussion

---

## License

MIT — see [LICENSE](LICENSE).

Based on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by
[Nous Research](https://nousresearch.com). This fork and its extensions are maintained
by [Interstellar-code](https://github.com/Interstellar-code).
