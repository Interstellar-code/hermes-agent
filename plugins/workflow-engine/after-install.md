# After Install — workflow-engine

1. Restart the Hermes dashboard: `hermes dashboard restart`
2. Open the dashboard — a **Workflows** entry should appear in the sidebar.
3. Verify the health endpoint:
   ```bash
   curl http://127.0.0.1:8642/api/plugins/workflow-engine/health
   # → {"ok":true,"version":"0.1.0"}
   ```
4. Place workflow YAML files in `~/.hermes/workflows/` (created automatically
   on first run in Phase 2a).

No environment variables are required for Phase 1.

---

## Background scheduler (daemon) — Phase 4

The workflow cron poller and kanban dispatcher run in a standalone daemon
process (`hermes workflow daemon`). Choose one install method:

### Linux (systemd user unit)

```bash
cp plugins/workflow-engine/systemd/hermes-workflow-dispatcher.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-workflow-dispatcher.service
systemctl --user status hermes-workflow-dispatcher.service
```

### macOS (launchd)

```bash
cp plugins/workflow-engine/launchd/ai.hermes.workflow-dispatcher.plist \
   ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/ai.hermes.workflow-dispatcher.plist
launchctl list | grep hermes-workflow
```

The plist logs to `/tmp/hermes-workflow-dispatcher.log` by default. Edit the
`StandardOutPath` / `StandardErrorPath` keys to redirect to `~/.hermes/logs/`
(note: launchd does not expand `~` in plist values — use the full absolute path).

### Foreground / dev mode (no supervisor)

```bash
hermes workflow daemon --interval 30
```

**IMPORTANT**: Without a supervisor (systemd or launchd), the daemon does
**not** auto-restart if it crashes. Foreground mode is sufficient for
development; use a supervisor for any production or always-on deployment.

### Config keys

Add to your hermes config (`~/.hermes/config.yaml`) to tune auth and rate
limits for the agent tools:

```yaml
workflow:
  allowed_roots: ["~", "${HERMES_HOME}"]  # working_path must resolve under one of these
  run_rate_per_session: 5                 # max workflow_run calls per minute per session
  approve_any: false                      # if true, any session can approve any run (dev only)
```
