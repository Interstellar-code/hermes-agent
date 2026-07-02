# Review Lens: Dependencies

Primary focus for this review: **dependency risk**. Weight your findings toward
the concerns below, but never ignore an unrelated BLOCKER (including a security
one) you happen to see.

## What to scrutinize

- **Package health** — unmaintained / abandoned / single-maintainer packages,
  very low adoption, packages added for trivial functionality, deep transitive
  trees pulled in for a small need.
- **Licenses** — license incompatible with the project's, copyleft pulled into a
  permissive codebase, missing/unclear license, license changes across versions.
- **CVEs / known vulns** — dependencies with known advisories, transitive
  vulnerabilities, versions behind a security fix, no path to patch.
- **Pinning & reproducibility** — unpinned or loosely-ranged versions, missing
  lockfile or lockfile not updated, floating tags, mismatch between manifest and
  lockfile, non-reproducible installs.
- **Supply-chain hygiene** — typosquat-prone or newly-published names, installs
  from untrusted sources/registries, post-install scripts, missing integrity
  hashes, unverified provenance, needless duplication of an already-present dep.
- **Footprint** — large/native dependencies added without justification, version
  conflicts/duplicates, dependencies that should be dev-only landing in runtime.

## Severity guidance (apply the shared rubric)

- **BLOCKER** — a known-exploitable CVE on a shipped dependency with no
  mitigation, a license that legally bars shipping, or a supply-chain red flag
  (e.g. likely-malicious / typosquat package).
- **HIGH** — a real vulnerability or license/pinning problem that should block
  release until resolved.
- **MED** — health/hygiene risk (unpinned version, unmaintained package, large
  unjustified footprint) that needs attention but isn't yet urgent.
- **LOW / NIT** — minor footprint or duplication concerns with limited impact.

When unsure between two levels, pick the higher and note the uncertainty in the
Finding's evidence. Anchor each claim to the manifest/lockfile entry
(`file:line`) and the specific version. Prefer fewer, well-evidenced findings
over speculation.
