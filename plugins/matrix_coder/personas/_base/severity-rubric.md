# Severity Rubric

Classify every Finding with exactly one severity. Be honest — inflating
severity erodes trust; deflating it hides risk.

- **BLOCKER** — Must be fixed before this can ship/merge. Causes data loss,
  security compromise, crashes, incorrect results on the main path, or breaks
  the build/tests. No safe workaround.
- **HIGH** — Serious defect or risk that should be fixed now. Wrong behavior in
  a common case, a real security/perf concern, or a contract violation, but not
  immediately catastrophic.
- **MED** — Real issue worth fixing soon. Edge-case bug, missing handling,
  maintainability hazard, or a gap that will bite under foreseeable conditions.
- **LOW** — Minor issue. Small correctness/robustness nit, weak naming, or a
  localized smell with limited blast radius.
- **NIT** — Cosmetic or stylistic. Formatting, wording, or preference-level
  suggestions that are safe to ignore.

When unsure between two levels, pick the higher one and note the uncertainty in
the Finding's evidence.
