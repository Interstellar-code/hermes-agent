# Domain Pack: Frontend

This pack ADDS stack-specific context for frontend / UI work. It does NOT
override the active role's contract, output format, or severity rubric. Apply
it alongside your role persona — treat it as an extra checklist lens.

## Stack context this pack adds

- **Components & state** — component boundaries, prop drilling, shared vs.
  local state, derived state vs. stored state, unnecessary re-renders, stale
  closures in hooks/callbacks.
- **UI/UX conventions** — consistent spacing/sizing tokens, accessible color
  contrast, keyboard navigation, focus management, meaningful ARIA labels, no
  ARIA that conflicts with native semantics.
- **Accessibility (a11y)** — interactive elements reachable and operable by
  keyboard; images have alt text; form inputs have associated labels; error
  messages are announced; color is never the sole differentiator.
- **Browser & runtime** — feature detection vs. browser sniffing, CSP
  compatibility, memory leaks (event listeners not removed, timers not
  cleared), layout/paint thrashing, large bundle chunks blocking TTI.
- **Bundling & assets** — tree-shaking friendliness, lazy/dynamic imports for
  route-level splits, image optimization, cache-busting hashes, avoiding
  accidental inclusion of dev-only code in production bundles.
- **Testing idioms** — prefer queries by role/label/text over implementation
  details; mock at the boundary (network, time), not deep internals; snapshot
  tests should be intentional, not a catch-all.

## Common pitfalls to flag

- Prop mutation instead of deriving new state.
- `useEffect` with missing or incorrect dependency arrays.
- Inline styles or `!important` hacks that bypass the design system.
- Uncontrolled → controlled component transitions without explicit handling.
- Click handlers on non-interactive elements (missing keyboard/ARIA).
- Secrets or environment variables exposed to the client bundle.
- Direct DOM manipulation bypassing the framework's reconciler.
