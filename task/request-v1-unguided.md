Bug: an order line must not be allocated twice.

When the same order line (same order id, SKU and quantity) is allocated twice, the second
Allocate command must change nothing. Today, if the batch that already holds the line has
no stock left, the line is allocated again to the next batch, so one order consumes stock
from two batches. If there is no other batch, an out-of-stock event is recorded for an order
that is already allocated.

Expected behaviour: Product.allocate returns the reference of the batch that already holds
the line, allocates nothing, and records no new event.

Scope: you may only edit files under src/allocation/. The fix belongs in
src/allocation/domain/model.py. Do not change tests.
