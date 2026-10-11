def average(values):
    """Return the mean of a list of numbers."""
    total = 0
    for v in values:
        total += v
    return total / len(values)


def top_n(scores, n):
    """Return the n highest scores, highest first."""
    return sorted(scores)[:n]
