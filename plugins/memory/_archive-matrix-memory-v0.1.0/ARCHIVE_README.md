# matrix-memory v0.1.0 — ARCHIVED 2026-06-19

This is the **retired** custom implementation of the `matrix-memory`
provider (custom FTS5 + markdown wiki, ~1,361 LOC). It was replaced by
**v0.2.0**, a thin contract layer wrapping the **Mnemosyne** engine.

- Live v0.2 plugin:  `plugins/memory/matrix-memory/`
- Engine submodule:  `plugins/memory/_matrix-memory-mnemosyne/` (fork `main` @ `aa09b29`)
- Fork repo:         https://github.com/Interstellar-code/mnemosyne
- Design doc (v0.1): `~/hermes/research/Plans/matrix-memory-plugin.md`
- Fork spec  (v0.2): `~/hermes/research/Plans/matrix-memory-mnemosyne-fork.md`

## Why kept (not deleted)

Per fork spec §13.6 rec (c): retain the v0.1.0 validation recipe and the
design-doc history. The contract (3-tier model, Tier 1 passthrough,
dry_run + confirm_token safety, one-way wiki bridge, discipline skill)
survives in v0.2; only the custom engine was replaced by Mnemosyne's
(vector + KG + temporal + sync).

## Discovery

This dir is `_`-prefixed so Hermes' bundled memory-provider discovery
(`plugins/memory/__init__.py` skips names starting with `_` or `.`) does
**not** surface it as a selectable provider. It is inert history.

## Restore (emergency rollback)

```bash
# from the hermes-agent checkout
git mv plugins/memory/matrix-memory plugins/memory/_matrix-memory-v0.2-disabled
git mv plugins/memory/_archive-matrix-memory-v0.1.0 plugins/memory/matrix-memory
# remove the ARCHIVE_README.md if it interferes, then restart the gateway
```
The config key `memory.provider: matrix-memory` is unchanged across both
versions, so a rename swap is sufficient to roll back.
