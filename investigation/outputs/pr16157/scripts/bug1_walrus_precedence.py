"""Minimal reproducer for the walrus-precedence bug in
qiskit/transpiler/preset_passmanagers/generate_preset_pass_manager.py:218.

Run:  python3 /tmp/bug_walrus_precedence.py

The bug has no Qiskit dependency — it's pure Python operator precedence.
"""
import os

# Exact line from Qiskit main (generate_preset_pass_manager.py:218):
#
#     if seed := os.getenv("QISKIT_TRANSPILER_SEED", None) is not None:
#         seed_transpiler = int(seed)
#
# Intended:  assign the env var's value to `seed`, branch on whether it's set.
# Actual:    assign `True`/`False` to `seed`, because `:=` binds looser than `is not`.

def buggy_parse():
    """What the current code does."""
    seed_transpiler = None
    if seed := os.getenv("QISKIT_TRANSPILER_SEED", None) is not None:
        seed_transpiler = int(seed)
    return seed, seed_transpiler


def correct_parse():
    """Proposed fix: parenthesize the walrus assignment."""
    seed_transpiler = None
    if (seed := os.getenv("QISKIT_TRANSPILER_SEED", None)) is not None:
        seed_transpiler = int(seed)
    return seed, seed_transpiler


for env_value in ["42", "12345", None]:
    if env_value is None:
        os.environ.pop("QISKIT_TRANSPILER_SEED", None)
    else:
        os.environ["QISKIT_TRANSPILER_SEED"] = env_value

    b_seed, b_transpiler = buggy_parse()
    c_seed, c_transpiler = correct_parse()

    label = f"QISKIT_TRANSPILER_SEED={env_value!r}"
    print(f"{label}")
    print(f"  buggy:   seed={b_seed!r:>10}  seed_transpiler={b_transpiler!r}")
    print(f"  correct: seed={c_seed!r:>10}  seed_transpiler={c_transpiler!r}")
    print()
