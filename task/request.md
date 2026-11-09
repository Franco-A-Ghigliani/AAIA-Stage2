Bug: an order line must not be allocated twice.

When the same order line (same order id, SKU and quantity) is allocated twice, the second
Allocate command must change nothing. Today, if the batch that already holds the line has
no stock left, the line is allocated again to the next batch, so one order consumes stock
from two batches. If there is no other batch, an out-of-stock event is recorded for an order
that is already allocated.

Expected behaviour: Product.allocate returns the reference of the batch that already holds
the line, allocates nothing, and records no new event.

How to fix it: in src/allocation/domain/model.py, method Product.allocate, ADD a check as
the first statements of the method, before the existing `try:` line. The check loops over
self.batches; if `line` is already in a batch's `_allocations` set, it returns that batch's
reference immediately. Do not modify or remove any existing line of the method.

Scope: you may only edit files under src/allocation/. Do not change tests. After the edit,
run the unit_tests check, then give your final summary.
