# After Install — workflow-engine

1. Restart the Hermes dashboard: `hermes dashboard restart`
2. This plugin is **API-only** in the Hermes dashboard — its manifest sets
   `tab.hidden: true`, so **no Workflows entry appears in the dashboard sidebar**.
   The workflows UI lives in the separate **hermes-switchui** app. Enabling the
   plugin only gives you the backend API below.
3. Verify the health endpoint (on the dashboard, port 9119 — the gateway's 8642
   returns 404; the dashboard needs its session auth token, a bare curl gets 401):
   ```bash
   curl http://127.0.0.1:9119/api/plugins/workflow-engine/health
   # → {"ok":true,"version":"0.1.0"}
   ```
4. Bundled workflows are seeded into the profile DB (`$HERMES_HOME/switchui-workflows.db`)
   on first engine use; the engine initialises lazily on first tool/API call.

No environment variables are required for Phase 1.

---

## Background scheduler (daemon) — Phase 4

The workflow cron poller and scheduled-run tick run in a standalone daemon
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

Both units set `HERMES_HOME` (default `~/.hermes`; edit for another profile) and log to
`$HERMES_HOME/logs/workflow-daemon.log` / `workflow-daemon-error.log`. launchd does not
expand `~` — replace `/Users/YOU` in the plist. The daemon takes a single-instance lock at
`$HERMES_HOME/workflow-daemon.pid`; launchd restarts it only after a crash.

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
