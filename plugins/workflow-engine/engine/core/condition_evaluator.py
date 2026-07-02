"""
Condition evaluator for DAG workflow ``when:`` expressions.

Ports condition-evaluator.ts exactly. Grammar:

  String equality:  "$nodeId.output == 'VALUE'"  / "$nodeId.output != 'VALUE'"
  Dot notation:     "$nodeId.output.field == 'VALUE'"
  Numeric ops:      "$nodeId.output > '80'"  / ">=" / "<" / "<="
                    (both sides must parse as finite floats; fail-closed otherwise)
  Compound AND/OR:  "$a.output == 'X' && $b.output != 'Y'"
                    "$a.output == 'X' || $b.output == 'Y'"
                    AND has higher precedence than OR. No parentheses.

Returns True = run this node, False = skip it.
Invalid/unparseable expressions default to False (fail-closed = skip the node).
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Dict

from engine.schemas.workflow_run import NodeOutput

logger = logging.getLogger("workflow.condition-evaluator")

# Pattern matching a single condition atom: $nodeId.output[.field] OPERATOR 'value'
_ATOM_PATTERN = re.compile(
    r"^\$([a-zA-Z_][a-zA-Z0-9_-]*)\.output(?:\.([a-zA-Z_][a-zA-Z0-9_]*))?\s*(==|!=|<=|>=|<|>)\s*'([^']*)'$"
)


def _resolve_output_ref(
    node_id: str,
    field: str | None,
    node_outputs: Dict[str, NodeOutput],
) -> str:
    """Resolve $nodeId.output or $nodeId.output.field to a string value."""
    node_output = node_outputs.get(node_id)
    if not node_output:
        logger.warning("condition_output_ref_unknown_node node_id=%s", node_id)
        return ""
    output = node_output.output
    if not output:
        return ""
    if field is None:
        return output
    # Dot notation: parse JSON and access field
    try:
        parsed = json.loads(output)
        if not isinstance(parsed, dict):
            return ""
        value = parsed.get(field)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value).lower() if isinstance(value, bool) else str(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return ""
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "condition_json_parse_failed node_id=%s field=%s output_preview=%.100s",
            node_id, field, output,
        )
        return ""


def _split_outside_quotes(expr: str, sep: str) -> list[str]:
    """Split expr on sep only when not inside single-quoted regions."""
    parts: list[str] = []
    current = ""
    in_quote = False
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'" and not in_quote:
            in_quote = True
            current += ch
            i += 1
        elif ch == "'" and in_quote:
            in_quote = False
            current += ch
            i += 1
        elif not in_quote and expr[i:i + len(sep)] == sep:
            parts.append(current.strip())
            current = ""
            i += len(sep)
        else:
            current += ch
            i += 1
    parts.append(current.strip())
    return parts


def _evaluate_atom(
    atom: str,
    node_outputs: Dict[str, NodeOutput],
) -> tuple[bool, bool]:
    """Evaluate a single atom. Returns (result, parsed)."""
    atom = atom.strip()
    match = _ATOM_PATTERN.match(atom)
    if not match:
        logger.debug("condition_atom_parse_failed atom=%r", atom)
        return False, False

    node_id, field, operator, expected = match.group(1), match.group(2), match.group(3), match.group(4)
    actual = _resolve_output_ref(node_id, field, node_outputs)
    expr = atom  # for logging

    if operator in ("==", "!="):
        result = (actual == expected) if operator == "==" else (actual != expected)
    else:
        # Numeric comparison
        try:
            actual_num = float(actual)
            expected_num = float(expected)
        except (ValueError, TypeError):
            logger.debug(
                "condition_numeric_parse_failed expr=%r actual=%r expected=%r",
                expr, actual, expected,
            )
            return False, False
        if not (math.isfinite(actual_num) and math.isfinite(expected_num)):
            logger.debug(
                "condition_numeric_parse_failed expr=%r actual=%r expected=%r",
                expr, actual, expected,
            )
            return False, False
        if operator == "<":
            result = actual_num < expected_num
        elif operator == ">":
            result = actual_num > expected_num
        elif operator == "<=":
            result = actual_num <= expected_num
        else:  # ">="
            result = actual_num >= expected_num

    logger.debug(
        "condition_evaluated node_id=%s field=%s operator=%s expected=%r actual=%r result=%s",
        node_id, field, operator, expected, actual, result,
    )
    return result, True


def evaluate_condition(
    expr: str,
    node_outputs: Dict[str, NodeOutput],
) -> tuple[bool, bool]:
    """
    Evaluate a condition expression (possibly compound) against upstream node outputs.

    Returns (result, parsed) — result is True to run the node, False to skip;
    parsed is False when the expression could not be parsed (fail-closed: result defaults to False).
    """
    trimmed = expr.strip()

    # Split on || — OR has lower precedence
    or_clauses = _split_outside_quotes(trimmed, "||")

    for or_clause in or_clauses:
        # Split each OR clause on && — AND has higher precedence
        and_atoms = _split_outside_quotes(or_clause, "&&")
        or_clause_result = True

        for atom in and_atoms:
            result, parsed = _evaluate_atom(atom, node_outputs)
            if not parsed:
                return False, False  # fail-closed on any parse error
            if not result:
                or_clause_result = False
                break  # short-circuit AND

        if or_clause_result:
            return True, True  # short-circuit OR

    return False, True
