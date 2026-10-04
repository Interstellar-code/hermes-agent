# projects plugin

Dashboard REST API over the per-profile `projects.db` (`$HERMES_HOME/projects.db`, created on first write).
Mounted at `/api/plugins/projects`. Auth: the global dashboard auth middleware (session token / OAuth gate); there is no per-route auth.

Every route takes `?profile=<name|current>` to target another profile's `projects.db` (404 unknown profile, 400 bad name).

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | List projects (`include_archived=false`), `active_id`, enrichment |
| POST | `/` | Create |
| GET/PATCH/DELETE | `/{id_or_slug}` | Get / patch / hard-delete |
| POST | `/{id}/folders`, `/{id}/folders/primary`; DELETE `/{id}/folders`; GET `/{id}/folders` | Folder management |
| POST | `/{id}/archive`, `/{id}/restore` | Archive / restore (returns list + project) |
| POST | `/{id}/active` | Set active project |
| GET | `/{id}/activity?limit&cursor` | Newest-first task + session activity, cursor-paged |
| POST/DELETE/GET | `/session`, `/session/{sid}`, `/{id}/sessions` | Session <-> project binding |

## Semantics

- **Archive** hides the project and clears the active pointer if it was active (same transaction).
- **Set active** on an archived project returns 409.
- **Delete** only works on archived projects (atomic `DELETE ... WHERE archived = 1`); otherwise 409. Clears the active pointer.
- **Enrichment** adds `task_count`, `open_task_count`, `task_status_counts`, `session_count`, `last_*_activity_at`, `bound_board`. Board and session aggregates are cached for up to 5 s and invalidated when any source DB changes. If a source fails, its fields are `null` and `enrichment_errors` lists the causes.
- Field lengths are capped (name 200, path 4096, description 10k); `/` and `~` are rejected as folder paths.
