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

Hermes still provides the core learning loop: it creates and improves skills from
experience, persists memory, searches past conversations, delegates to subagents, and
runs across a real terminal, messaging platforms, and remote environments. This fork
keeps those upstream capabilities and adds a browser workspace for operating them.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), OpenRouter, OpenAI, your own endpoint, and [many others](https://hermes-agent.nousresearch.com/docs/integrations/providers). Switch with `hermes model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Six terminal backends — local, Docker, SSH, Singularity, Modal, and Daytona. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## Hermes Agent + SwitchUI

[Hermes SwitchUI](https://hermes-switchui.zi0n.space/) is the self-hosted browser
workspace for this fork. It is a separate frontend repository, not a replacement for
the Hermes runtime.

| Layer | Responsibility |
| --- | --- |
| **Hermes Agent fork** | Models, conversations, tools, memory, sessions, gateway, scheduling, and persistence |
| **Hermes SwitchUI** | Browser chat and workspace, files, terminal, projects, boards, workflows, operations, and plugin views |
| **Plugins** | Optional capabilities such as A2A fleet coordination, specialist coding, personas, and Matrix Memory |

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

> **Heads up:** Native Windows runs Hermes without WSL — CLI, gateway, TUI, and
> tools all work natively. If you prefer WSL2, use the Linux installer above.
> Found a fork-specific bug? [File it here](https://github.com/Interstellar-code/hermes-agent/issues).

Run this in PowerShell:

```powershell
iex (irm https://raw.githubusercontent.com/Interstellar-code/hermes-agent/main/scripts/install.ps1)
```

The installer handles everything: uv, Python 3.11, Node.js, ripgrep, ffmpeg, **and a portable Git Bash** (MinGit, unpacked to `%LOCALAPPDATA%\hermes\git` — no admin required, completely isolated from any system Git install). Hermes uses this bundled Git Bash to run shell commands.

If you already have Git installed, the installer detects it and uses that instead. Otherwise a ~45MB MinGit download is all you need — it won't touch or interfere with any system Git.

> **Android / Termux:** The tested manual path is documented in the [Termux guide](https://hermes-agent.nousresearch.com/docs/getting-started/termux). On Termux, Hermes installs a curated `.[termux]` extra because the full `.[all]` extra currently pulls Android-incompatible voice dependencies.
>
> **Windows:** Native Windows is fully supported — the PowerShell one-liner above installs everything. If you'd rather use WSL2, the Linux command works there too. Native Windows install lives under `%LOCALAPPDATA%\hermes`; WSL2 installs under `~/.hermes` as on Linux.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes              # start chatting!
```

### Troubleshooting

#### Windows Defender or antivirus flags `uv.exe` as malware

If your antivirus (Bitdefender, Windows Defender, etc.) quarantines `uv.exe` from the Hermes `bin` folder (`%LOCALAPPDATA%\hermes\bin\uv.exe`), this is a **false positive**. The file is Astral's `uv` — the Rust Python package manager Hermes bundles to manage its Python environment. ML-based antivirus engines commonly flag unsigned Rust binaries that download and install packages.

**To verify your copy is authentic:**

```powershell
# Install GitHub CLI if needed
winget install --id GitHub.cli

# Login to GitHub
gh auth login

# Run verification
$uv = "$env:LOCALAPPDATA\hermes\bin\uv.exe"
$ver = (& $uv --version).Split(' ')[1]
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = "$env:TEMP\uv.zip"
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
gh attestation verify $zip --repo astral-sh/uv
Expand-Archive $zip "$env:TEMP\uv_x" -Force
(Get-FileHash "$env:TEMP\uv_x\uv.exe").Hash -eq (Get-FileHash $uv).Hash
```

If attestation says "Verification succeeded" and the last line prints `True`, you're good.

**To whitelist Hermes:**
- **Windows Defender:** Run PowerShell as Admin → `Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\hermes\bin"`
- **Bitdefender:** Add an exception in the Bitdefender console (Protection > Antivirus > Settings > Manage Exceptions)
- Whitelist the **folder**, not the file hash — Hermes updates `uv` and the hash changes every version

For more context, see the upstream Astral reports: [astral-sh/uv#13553](https://github.com/astral-sh/uv/issues/13553), [astral-sh/uv#15011](https://github.com/astral-sh/uv/issues/15011), [astral-sh/uv#10079](https://github.com/astral-sh/uv/issues/10079).

---

## Getting Started

```bash
hermes              # Interactive CLI — start a conversation
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)
hermes setup        # Run the full setup wizard (configures everything at once)
hermes claw migrate # Migrate from OpenClaw (if coming from OpenClaw)
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

📖 **[SwitchUI documentation](https://hermes-switchui.zi0n.space/docs/welcome/)** ·
**[Upstream Hermes documentation](https://hermes-agent.nousresearch.com/docs/)**

---

## Skip the API-key collection — Nous Portal

Hermes works with whatever provider you want — that's not changing. But if you'd rather not collect five separate API keys for the model, web search, image generation, TTS, and a cloud browser, **[Nous Portal](https://portal.nousresearch.com)** covers all of them under one subscription:

- **300+ models** — pick any of them with `/model <name>`
- **Tool Gateway** — web search (Firecrawl), image generation (FAL), text-to-speech (OpenAI), cloud browser (Browser Use), all routed through your sub. No extra accounts.

One command from a fresh install:

```bash
hermes setup --portal
```

That logs you in via OAuth, sets Nous as your provider, and turns on the Tool Gateway. Check what's wired up any time with `hermes portal info`. Full details on the [Tool Gateway docs page](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-gateway).

You can still bring your own keys per-tool whenever you want — the gateway is per-backend, not all-or-nothing.

---

## CLI vs Messaging Quick Reference

Hermes has two entry points: start the terminal UI with `hermes`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both interfaces.

| Action                         | CLI                                           | Messaging platforms                                                              |
| ------------------------------ | --------------------------------------------- | -------------------------------------------------------------------------------- |
| Start chatting                 | `hermes`                                      | Run `hermes gateway setup` + `hermes gateway start`, then send the bot a message |
| Start fresh conversation       | `/new` or `/reset`                            | `/new` or `/reset`                                                               |
| Change model                   | `/model [provider:model]`                     | `/model [provider:model]`                                                        |
| Set a personality              | `/personality [name]`                         | `/personality [name]`                                                            |
| Retry or undo the last turn    | `/retry`, `/undo`                             | `/retry`, `/undo`                                                                |
| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]`                                        |
| Browse skills                  | `/skills` or `/<skill-name>`                  | `/<skill-name>`                                                                  |
| Interrupt current work         | `Ctrl+C` or send a new message                | `/stop` or send a new message                                                    |
| Platform-specific status       | `/platforms`                                  | `/status`, `/sethome`                                                            |

For the full command lists, see the [CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli) and the [Messaging Gateway guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

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
| [**Matrix Coder**](plugins/matrix_coder/README.md) | Eight coding specialist roles, review lenses, domain/workflow personas, trusted developer-tier injection, and optional Kanban audit mirroring. Concurrency safety remains an orchestration responsibility. |
| [**Personas**](plugins/personas/README.md) | A canonical library of 20 personas, runtime list/get/apply tools, trusted `persona_ref` overlays, and a read API. SwitchUI wizard migration and write-side promotion remain follow-up work. |
| **Kanban extensions** | Board templates, scheduled tasks and board creation, project-linked tasks, dispatcher workflows, and restored dashboard APIs on top of Hermes Kanban. |

### Optional and transitional

| Extension | Status |
| --- | --- |
| [**Matrix Memory**](plugins/memory/matrix-memory/) | Optional Mnemosyne-backed vector, knowledge-graph, temporal, and wiki-bridge memory provider. Requires its pinned engine dependency and deliberate per-profile data-path configuration. |
| [**MCP Lazy**](plugins/mcp_lazy/README.md) | Fork-local compatibility layer that defers MCP schemas and promotes them on demand. Savings are workload-dependent; prefer upstream `tool_search` when it works for your provider configuration. |

`karpathy-self-improve` is being retired and is intentionally not presented as a
supported extension.

---

## Documentation

### Interstellar fork and SwitchUI

| Resource | What's covered |
| --- | --- |
| [SwitchUI website](https://hermes-switchui.zi0n.space/) | Product overview, architecture, and the self-hosted browser workspace |
| [SwitchUI documentation](https://hermes-switchui.zi0n.space/docs/welcome/) | Installation, chat, files, terminal, projects, boards, workflows, operations, plugins, and troubleshooting |
| [Fork issues](https://github.com/Interstellar-code/hermes-agent/issues) | Bugs and feature requests specific to this repository |
| [Plugin READMEs](plugins/) | Implementation, configuration, limitations, and operational guidance for fork extensions |

### Upstream Hermes core

The upstream documentation remains the authoritative reference for shared Hermes core
behavior:

| Section                                                                                             | What's Covered                                             |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| [Quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart)                 | Install → setup → first conversation in 2 minutes          |
| [CLI Usage](https://hermes-agent.nousresearch.com/docs/user-guide/cli)                              | Commands, keybindings, personalities, sessions             |
| [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)                | Config file, providers, models, all options                |
| [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)                | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)                          | Command approval, DM pairing, container isolation          |
| [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)            | 40+ tools, toolset system, terminal backends               |
| [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)              | Procedural memory, Skills Hub, creating skills             |
| [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)                     | Persistent memory, user profiles, best practices           |
| [MCP Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)               | Connect any MCP server for extended capabilities           |
| [Cron Scheduling](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)              | Scheduled tasks with platform delivery                     |
| [Context Files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)       | Project context that shapes every conversation             |
| [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)             | Project structure, agent loop, key classes                 |
| [Contributing](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing)             | Development setup, PR process, code style                  |
| [CLI Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)                  | All commands and flags                                     |
| [Environment Variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) | Complete env var reference                                 |

---

## Migrating from OpenClaw

If you're coming from OpenClaw, Hermes can automatically import your settings, memories, skills, and API keys.

**During first-time setup:** The setup wizard (`hermes setup`) automatically detects `~/.openclaw` and offers to migrate before configuration begins.

**Anytime after install:**

```bash
hermes claw migrate              # Interactive migration (full preset)
hermes claw migrate --dry-run    # Preview what would be migrated
hermes claw migrate --preset user-data   # Migrate without secrets
hermes claw migrate --overwrite  # Overwrite existing conflicts
```

What gets imported:

- **SOUL.md** — persona file
- **Memories** — MEMORY.md and USER.md entries
- **Skills** — user-created skills → `~/.hermes/skills/openclaw-imports/`
- **Command allowlist** — approval patterns
- **Messaging settings** — platform configs, allowed users, working directory
- **API keys** — allowlisted secrets (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs)
- **TTS assets** — workspace audio files
- **Workspace instructions** — AGENTS.md (with `--workspace-target`)

See `hermes claw migrate --help` for all options, or use the `openclaw-migration` skill for an interactive agent-guided migration with dry-run previews.

---

## Contributing

We welcome contributions to this fork. Search the
[fork issues](https://github.com/Interstellar-code/hermes-agent/issues) and
[pull requests](https://github.com/Interstellar-code/hermes-agent/pulls) first. The
[upstream contributing guide](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing)
still applies to shared architecture and code style; fork-specific contribution rules
live in [AGENTS.md](AGENTS.md).

Quick start for contributors — use the standard installer, then work from the
full git checkout it creates at `$HERMES_HOME/hermes-agent` (usually
`~/.hermes/hermes-agent`). This matches the layout used by `hermes update`, the
managed venv, lazy dependencies, gateway, and docs tooling.

```bash
curl -fsSL https://raw.githubusercontent.com/Interstellar-code/hermes-agent/main/scripts/install.sh | bash
cd "${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

Manual clone fallback (for throwaway clones/CI where you intentionally do not
want the managed install layout):

Create the venv outside the cloned source tree — a venv inside the directory
the agent operates from can be wiped by a relative-path command the agent runs
against its own checkout, destroying the running runtime mid-session.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv ~/.hermes/venvs/hermes-dev --python 3.11
source ~/.hermes/venvs/hermes-dev/bin/activate
git clone https://github.com/Interstellar-code/hermes-agent.git
cd hermes-agent
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

---

## Community

- 🌐 [Hermes SwitchUI website](https://hermes-switchui.zi0n.space/)
- 📚 [SwitchUI documentation](https://hermes-switchui.zi0n.space/docs/welcome/)
- 🐛 [Interstellar fork issues](https://github.com/Interstellar-code/hermes-agent/issues)
- 💬 [Nous Research Discord](https://discord.gg/NousResearch) — upstream/community discussion
- 🧠 [Upstream Hermes Agent](https://github.com/NousResearch/hermes-agent)
- 📦 [Agent Skills open standard](https://agentskills.io)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux desktop-control MCP server for Hermes and other MCP hosts, with AT-SPI accessibility trees, Wayland/X11 input, screenshots, and compositor window targeting.
- 🔌 [HermesClaw](https://github.com/AaronWong1999/hermesclaw) — Community WeChat bridge: Run Hermes Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Based on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by
[Nous Research](https://nousresearch.com). This fork and its extensions are maintained
by [Interstellar-code](https://github.com/Interstellar-code).
