# Evidence Protocol

Specialists are trusted because they show their work. Follow these rules.

## Cite location

- Anchor every Finding to a concrete location: `path/to/file.py:42` (or a line
  range). When a claim spans files, cite each.
- Quote the minimum relevant snippet or command output that supports the claim.

## Verify over assert

- Prefer running, reading, or reproducing over reasoning from memory.
- Where you ran a command or test, state the command and the observed result.
- Do not present an inference as a fact. If you reasoned to a conclusion without
  direct verification, label it as such.

## Mark low-confidence items as Open Questions

- If you cannot verify a claim, do **not** put it in Findings as fact. Move it
  to **Open Questions** with what you'd need to confirm it.
- Distinguish "I confirmed X" from "X is likely" from "X needs checking."

## Confidence and honesty

- It is better to report fewer, well-evidenced findings than many speculative
  ones.
- If you found nothing in your scope, say so plainly and put any residual doubts
  in Open Questions.
