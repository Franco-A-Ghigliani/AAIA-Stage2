Feature: cancel the allocation of an order line.

Add a command `CancelAllocation` with the fields `orderid` and `sku`. Handling it removes that
order's line for that SKU from the batch holding it, so the stock becomes available again.

Rules:
- The stock must stay free: a cancelled line must NOT be allocated again to any batch.
- Record a new event `AllocationCancelled` with the fields `orderid`, `sku` and `qty`. The
  allocations read model must drop the row for that order and SKU when the event is handled.
- Each cancellation must increase the product's `version_number`, as allocation does.
- If the SKU does not exist, raise the existing `InvalidSku`.
- If the order has no allocated line for that SKU, raise a new exception `AllocationNotFound`,
  defined next to `InvalidSku`, with a clear message. Nothing may change in that case.
- Existing commands, events and callers must keep working as they do today.

Scope: you may only edit files under src/allocation/. Do not change tests, configuration or
dependencies. Find the relevant modules yourself, run the unit_tests check, and fix what fails
before you give your final summary.
