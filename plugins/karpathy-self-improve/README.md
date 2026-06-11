# karpathy-self-improve

Hermes plugin: autonomous self-improvement loop — collects metrics, proposes diffs, evaluates them, and promotes or reverts via git ratchet.

## Configuration

### DB location

The plugin stores all data in a single SQLite database file.

**Default location**: `~/.hermes/karpathy-self-improve.db` (or wherever `get_default_hermes_root()` resolves — shared across all profiles as a central controller DB).

### Precedence (highest to lowest)

| Priority | Source | Description |
|----------|--------|-------------|
| 1 | `KARPATHY_DB_PATH` env var | Set at runtime; tests rely on this |
| 2 | `config.yaml` key | `plugins.karpathy_self_improve.db_path` |
| 3 | Default | `~/.hermes/karpathy-self-improve.db` |

### Setting via config.yaml

Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  karpathy_self_improve:
    db_path: /path/to/your/karpathy-self-improve.db
```

Supports `~` expansion and environment variable expansion (e.g. `$HERMES_HOME/ksi.db`). Relative paths are resolved against the hermes root directory.

### Setting via CLI

```bash
# Persist a custom path to config.yaml and initialize the DB:
hermes karpathy init --db-path /path/to/karpathy-self-improve.db

# Just inspect/create the current DB without changing config:
hermes karpathy init
```

### Setting via env var

```bash
export KARPATHY_DB_PATH=/tmp/ksi-dev.db
hermes karpathy status
```

## CLI

```
hermes karpathy init [--db-path PATH]   # Initialize DB; optionally write path to config.yaml
hermes karpathy collect                  # Collect metrics snapshots
hermes karpathy status                   # Show DB info + active experiments + baselines
hermes karpathy propose --profile PROF   # Run proposer once
hermes karpathy daemon [--interval N]    # Run the self-improvement loop continuously
```
