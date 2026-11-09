"""Reusable fixtures for behavior-focused tests."""

import json

SYSTEM = {"role": "system", "content": "trusted instructions"}


TASK = {"role": "user", "content": "Add Order.refund. Scope: only src/. Accept: refund test."}


class MappedRepository:
    def symbol_outline(self):
        return [
            "README.md",
            "src/billing/refunds.py: def issue",
            "src/orders/model.py: class Order(pay, refund)",
            "src/unrelated.py: def other",
        ]

    def read_text(self, path):
        return f"content of {path}\n" + "x = 1\n" * 5000


def steps(count, size=400):
    history = []
    for number in range(count):
        history.append(
            {
                "role": "assistant",
                "content": json.dumps({"tool": "read_file", "args": {"path": f"f{number}.py"}}),
            }
        )
        result = {"status": "ok", "output": "y" * size}
        history.append({"role": "user", "content": json.dumps({"result": result, "n": number})})
    return history
