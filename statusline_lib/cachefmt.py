"""Shared cache-count and cache-hit formatting across harness adapters."""

from .base import CACHE_READ, CACHE_WRITE, RESET, color_high_good, fmt


def format_cache_read(tokens):
    """Format cache-read tokens with the shared read identity color."""
    return f"{CACHE_READ}{fmt(tokens)}{RESET}"


def format_cache_write(tokens):
    """Format cache-write or cache-miss tokens with the shared secondary color."""
    return f"{CACHE_WRITE}{fmt(tokens)}{RESET}"


def format_cache_counts(read, write_or_miss):
    """Format the two-token cache core shared by Claude and Qwen."""
    return f"{format_cache_read(read)} / {format_cache_write(write_or_miss)}"


def cache_hit_percent(read, total_input):
    """Return cache-read tokens as a percentage of all input tokens."""
    return read * 100.0 / total_input if total_input > 0 else 0.0


def format_cache_hit(read, total_input, suffix=""):
    """Format cache hit percentage with the shared high-is-good color ramp."""
    return f"{color_high_good(cache_hit_percent(read, total_input), 90, 75)}{suffix}"
