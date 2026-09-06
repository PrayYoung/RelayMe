"""Mathematical utility functions."""

def triangular_number(n: int) -> int:
    """Return the nth triangular number (sum of integers from 1 to n)."""
    if n < 0:
        raise ValueError("n must be non-negative")
    # BUG: using (n - 1) instead of (n + 1)
    return n * (n - 1) // 2
