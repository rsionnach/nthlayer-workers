"""Policy condition evaluator.

Parses and evaluates condition strings against a context dictionary.
Supports a simple DSL for time, SLO, and service-based conditions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from nthlayer_workers.observe.gate.conditions import (
    is_business_hours,
    is_freeze_period,
    is_peak_traffic,
    is_weekday,
)


@dataclass
class PolicyContext:
    """Context for policy evaluation with service and SLO data."""

    budget_remaining: float = 100.0
    budget_consumed: float = 0.0
    burn_rate: float = 1.0
    tier: str = "standard"
    environment: str = "prod"
    service: str = ""
    team: str = ""
    downstream_count: int = 0
    high_criticality_downstream: int = 0
    now: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for evaluation. Delegates to get_current_context."""
        from nthlayer_workers.observe.gate.conditions import get_current_context

        ctx = get_current_context(
            budget_remaining=self.budget_remaining,
            budget_consumed=self.budget_consumed,
            burn_rate=self.burn_rate,
            tier=self.tier,
            environment=self.environment,
            downstream_count=self.downstream_count,
            high_criticality_downstream=self.high_criticality_downstream,
            now=self.now,
        )
        ctx["service"] = self.service
        ctx["team"] = self.team
        return ctx


@dataclass
class EvaluationResult:
    """Result of condition evaluation."""

    condition: str
    result: bool
    matched_rule: str | None = None
    context_snapshot: dict[str, Any] = field(default_factory=dict)


class ConditionEvaluator:
    """Evaluates policy conditions against a context.

    Supports comparisons, boolean operators (AND/OR/NOT), parentheses,
    and built-in functions (business_hours, freeze_period, etc.).
    """

    OPERATORS: dict[str, Any] = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
    }

    FUNCTIONS: dict[str, Callable[..., bool]] = {
        "business_hours": is_business_hours,
        "weekday": is_weekday,
        "freeze_period": is_freeze_period,
        "peak_traffic": is_peak_traffic,
    }

    def __init__(self, context: PolicyContext | dict[str, Any] | None = None):
        self._policy_context: PolicyContext | None = None
        if context is None:
            self.context: dict[str, Any] = {}
        elif isinstance(context, PolicyContext):
            self.context = context.to_dict()
            self._policy_context = context
        else:
            self.context = context

    def evaluate(self, condition: str) -> bool:
        """Evaluate a condition string. Empty condition = always true."""
        if not condition or not condition.strip():
            return True

        condition = " ".join(condition.split())

        try:
            return self._evaluate_expression(condition)
        except Exception:
            return False  # fail safe

    def _evaluate_expression(self, expr: str) -> bool:
        """Evaluate a boolean expression with AND/OR/NOT."""
        expr = expr.strip()

        while "(" in expr:
            match = re.search(r"(?<!\w)\(([^()]+)\)", expr)
            if match:
                inner = match.group(1)
                result = self._evaluate_expression(inner)
                expr = expr[: match.start()] + str(result) + expr[match.end() :]
            else:
                break

        if expr.upper().startswith("NOT "):
            return not self._evaluate_expression(expr[4:])

        if " OR " in expr.upper():
            parts = re.split(r"\s+OR\s+", expr, flags=re.IGNORECASE)
            return any(self._evaluate_expression(p) for p in parts)

        if " AND " in expr.upper():
            parts = re.split(r"\s+AND\s+", expr, flags=re.IGNORECASE)
            return all(self._evaluate_expression(p) for p in parts)

        if expr.lower() == "true":
            return True
        if expr.lower() == "false":
            return False

        func_match = re.match(r"(\w+)\((.*)\)", expr)
        if func_match:
            return self._evaluate_function(func_match.group(1), func_match.group(2))

        return self._evaluate_comparison(expr)

    def _evaluate_comparison(self, expr: str) -> bool:
        """Evaluate a comparison like 'hour >= 9'."""
        for op, func in self.OPERATORS.items():
            if op in expr:
                parts = expr.split(op, 1)
                if len(parts) == 2:
                    left = self._resolve_value(parts[0].strip())
                    right = self._resolve_value(parts[1].strip())
                    return func(left, right)

        value = self._resolve_value(expr)
        return bool(value)

    def _resolve_value(self, token: str) -> Any:
        """Resolve a token to its value."""
        token = token.strip()

        if (token.startswith("'") and token.endswith("'")) or (
            token.startswith('"') and token.endswith('"')
        ):
            return token[1:-1]

        try:
            if "." in token:
                return float(token)
            return int(token)
        except ValueError:
            pass

        if token.lower() == "true":
            return True
        if token.lower() == "false":
            return False

        return self.context.get(token, False)

    def _evaluate_function(self, name: str, args_str: str) -> bool:
        """Evaluate a function call."""
        name = name.lower()

        if name not in self.FUNCTIONS:
            return False

        func = self.FUNCTIONS[name]

        args = []
        if args_str.strip():
            raw_args = re.split(r",\s*(?=(?:[^']*'[^']*')*[^']*$)", args_str)
            for arg in raw_args:
                args.append(self._resolve_value(arg.strip()))

        now = None
        if self._policy_context:
            now = self._policy_context.now

        if name == "freeze_period" and len(args) >= 2:
            return func(args[0], args[1], now=now)
        elif name in ("business_hours", "weekday", "peak_traffic"):
            return func(now=now)

        return False

    def evaluate_all(
        self,
        conditions: list[dict[str, Any]],
    ) -> tuple[bool, dict[str, Any] | None]:
        """Evaluate multiple conditions and return the most restrictive match."""
        matched = None

        for cond in conditions:
            when_clause = cond.get("when", "")
            if self.evaluate(when_clause):
                if matched is None:
                    matched = cond
                else:
                    curr_block = cond.get("blocking", 0)
                    prev_block = matched.get("blocking", 0)
                    if curr_block > prev_block:
                        matched = cond

        return (matched is not None, matched)
