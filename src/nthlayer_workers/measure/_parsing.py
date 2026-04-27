"""Shared parsing utilities for model responses."""
from __future__ import annotations


def strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences from model response text."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text
