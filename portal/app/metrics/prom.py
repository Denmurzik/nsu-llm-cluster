"""A reader of the Prometheus text format, enough for llama-server's `/metrics`.

llama-server (`--metrics`) answers lines such as `llamacpp:tokens_predicted_total 12345` with `# HELP` and `# TYPE`
comments between them. The `llamacpp:` prefix is dropped, labels are ignored (llama-server has none), and a value that
is not a finite number (NaN, Inf) is skipped.
"""

import math

PREFIX = "llamacpp:"


def parse(text: str) -> dict[str, float]:
    """{"tokens_predicted_total": 12345.0, ...}; a line that does not parse is skipped."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        value = rest.split(" ", 1)[0] if rest else ""
        name = name.partition("{")[0]
        if not name.startswith(PREFIX):
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if math.isfinite(number):
            out[name[len(PREFIX):]] = number
    return out
