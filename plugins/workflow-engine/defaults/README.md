# defaults/

Default workflow YAML files bundled with the workflow-engine plugin.
On first enable, these are copied into `~/.hermes/switchui/workflows/`.

## Bundled workflows

| File | Name | Description |
|------|------|-------------|
| `githubawesome-monitor.yaml` | githubawesome-monitor | Polls the GithubAwesome RSS feed, diffs new posts against the local tracker JSON, and dispatches each new post into `tool-catalog-write`. |
| `tool-catalog-write.yaml` | tool-catalog-write | Catalogs a single forwarded URL into the local tool-catalog via `catalog.py`. |

## Notes

- No hardcoded paths. The `TOOL_CATALOG_ROOT` environment variable controls the catalog location.
- Add custom workflow YAMLs to `~/.hermes/switchui/workflows/` — they are discovered automatically.
