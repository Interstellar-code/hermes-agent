# defaults/

Default workflow YAML files bundled with the workflow-engine plugin.
On first enable, these are copied into `~/.hermes/switchui/workflows/`.

## Bundled workflows

There are currently **27** bundled workflow YAML files in this directory.

| File | Workflow name | Purpose |
|------|---------------|---------|
| `archon-adversarial-dev.yaml` | `archon-adversarial-dev` | Build an app from scratch via GAN-style Planner/Generator/Evaluator loop |
| `archon-architect.yaml` | `archon-architect` | Architectural sweep, complexity reduction, codebase health |
| `archon-assist.yaml` | `archon-assist` | Fallback when no other workflow matches the request |
| `archon-comprehensive-pr-review.yaml` | `archon-comprehensive-pr-review` | Comprehensive PR review with automatic fixes |
| `archon-create-issue.yaml` | `archon-create-issue` | File a bug as a GitHub issue with automated reproduction |
| `archon-feature-development.yaml` | `archon-feature-development` | Implement a feature from an existing plan/planning issue |
| `archon-fix-github-issue.yaml` | `archon-fix-github-issue` | Investigate, fix, and open a PR for a GitHub issue |
| `archon-idea-to-pr.yaml` | `archon-idea-to-pr` | Raw idea → autonomous PRD → plan → implement → PR |
| `archon-interactive-prd.yaml` | `archon-interactive-prd` | Create a PRD through guided conversation |
| `archon-issue-review-full.yaml` | `archon-issue-review-full` | Full fix + review pipeline for a GitHub issue |
| `archon-piv-loop.yaml` | `archon-piv-loop` | Guided Plan-Implement-Validate, human-in-the-loop |
| `archon-plan-to-pr.yaml` | `archon-plan-to-pr` | Execute an existing plan file end-to-end |
| `archon-ralph-dag.yaml` | `archon-ralph-dag` | Run a Ralph implementation loop |
| `archon-refactor-safely.yaml` | `archon-refactor-safely` | Refactor with continuous validation and behavior preservation |
| `archon-remotion-generate.yaml` | `archon-remotion-generate` | Generate/modify a Remotion video composition with AI |
| `archon-resolve-conflicts.yaml` | `archon-resolve-conflicts` | Fix merge conflicts against a base branch |
| `archon-smart-pr-review.yaml` | `archon-smart-pr-review` | Multi-angle PR review with auto-fix for CRITICAL/HIGH |
| `archon-test-loop-dag.yaml` | `archon-test-loop-dag` | Test-loop DAG (explicit invocation only) |
| `archon-validate-pr.yaml` | `archon-validate-pr` | Validate PR against main (bug present) and feature branch (fixed) |
| `archon-workflow-builder.yaml` | `archon-workflow-builder` | Create a new custom workflow for a project |
| `gateway-health-check.yaml` | `Gateway Health Check` | Check the Hermes gateway for errors, report to Telegram |
| `githubawesome-monitor.yaml` | `githubawesome-monitor` | Poll GithubAwesome RSS, dispatch new posts to `tool-catalog-write` |
| `pr-review-5agents.yaml` | `PR review (5-agent fan-out)` | Reusable subgraph: five specialist reviewers in parallel |
| `repo-issue-fixer.yaml` | `repo-issue-fixer` | Review open repo issues, analyze, attempt fixes |
| `repo-review.yaml` | `repo-review` | Review a repo for bugs, code smells, and issues |
| `switch-smoke-test.yaml` | `switch-smoke-test` | Minimal end-to-end smoke test for the workflow plugin |
| `tool-catalog-write.yaml` | `tool-catalog-write` | Catalog a single forwarded URL into the local tool-catalog |

To regenerate this table after adding/removing files, list the directory and re-read the `name`/`description` of each YAML rather than copying an old hardcoded count:

```bash
ls plugins/workflow-engine/defaults/*.yaml
```

## Notes

- No hardcoded paths. `TOOL_CATALOG_ROOT` controls the catalog location for tool-catalog workflows.
- Add custom workflow YAMLs to `~/.hermes/switchui/workflows/` — they are discovered automatically.
- Keep this README in sync with the actual directory contents; stale counts are worse than no count.
