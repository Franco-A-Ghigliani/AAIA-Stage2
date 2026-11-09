"""Acceptance check for CancelAllocation. Self-contained: no repo test helpers."""

from collections import defaultdict
from datetime import date

import pytest
from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.domain import commands, events
from allocation.service_layer import handlers, unit_of_work

LATER = date(2030, 1, 1)


class FakeRepository(repository.AbstractRepository):
    def __init__(self):
        super().__init__()
        self._products = set()

    def _add(self, product):
        self._products.add(product)

    def _get(self, sku):
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref):
        return next((p for p in self._products for b in p.batches if b.reference == batchref), None)


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository()

    def _commit(self):
        pass

    def rollback(self):
        pass


class FakeNotifications(notifications.AbstractNotifications):
    def __init__(self):
        self.sent = defaultdict(list)

    def send(self, destination, message):
        self.sent[destination].append(message)


def make_bus():
    return bootstrap.bootstrap(
        start_orm=False,
        uow=FakeUnitOfWork(),
        notifications=FakeNotifications(),
        publish=lambda channel, event: None,
    )


def product(bus, sku):
    return bus.uow.products.get(sku)


def available(bus, sku):
    return [b.available_quantity for b in product(bus, sku).batches]


def stocked_bus():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, None))
    bus.handle(commands.CreateBatch("b2", "LAMP", 10, LATER))
    bus.handle(commands.Allocate("o1", "LAMP", 4))
    return bus


def test_cancelling_frees_the_stock_and_does_not_allocate_again():
    bus = stocked_bus()
    bus.handle(commands.CancelAllocation("o1", "LAMP"))
    assert available(bus, "LAMP") == [10, 10]


def test_only_the_named_order_is_cancelled():
    bus = stocked_bus()
    bus.handle(commands.Allocate("o2", "LAMP", 3))
    bus.handle(commands.CancelAllocation("o1", "LAMP"))
    assert available(bus, "LAMP") == [7, 10]


def test_cancelling_increases_the_version_number():
    bus = stocked_bus()
    before = product(bus, "LAMP").version_number
    bus.handle(commands.CancelAllocation("o1", "LAMP"))
    assert product(bus, "LAMP").version_number == before + 1


class RecordingSession:
    """Stands in for the read-model session: any handler name may do the work."""

    def __init__(self):
        self.statements = []
        self.session = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def commit(self):
        pass

    def execute(self, statement, params=None):
        self.statements.append((" ".join(str(statement).lower().split()), params or {}))


def test_event_updates_the_read_model_and_is_not_reallocated():
    used = handlers.EVENT_HANDLERS[events.AllocationCancelled]
    assert handlers.reallocate not in used
    uow = RecordingSession()
    for handler in used:
        handler(events.AllocationCancelled("o1", "LAMP", 4), uow)
    assert any(
        "delete from allocations_view" in sql
        and params.get("orderid") == "o1"
        and params.get("sku") == "LAMP"
        for sql, params in uow.statements
    )


def test_the_event_is_recorded_with_the_quantity():
    uow = FakeUnitOfWork()
    handlers.add_batch(commands.CreateBatch("b1", "RUG", 10, None), uow)
    handlers.allocate(commands.Allocate("o1", "RUG", 4), uow)
    list(uow.collect_new_events())
    cancel = handlers.COMMAND_HANDLERS[commands.CancelAllocation]
    cancel(commands.CancelAllocation("o1", "RUG"), uow)
    assert list(uow.collect_new_events()) == [events.AllocationCancelled("o1", "RUG", 4)]


def test_unknown_allocation_is_a_clear_error_and_changes_nothing():
    bus = stocked_bus()
    version = product(bus, "LAMP").version_number
    with pytest.raises(handlers.AllocationNotFound, match="o9"):
        bus.handle(commands.CancelAllocation("o9", "LAMP"))
    assert available(bus, "LAMP") == [6, 10]
    assert product(bus, "LAMP").version_number == version


def test_unknown_sku_uses_the_existing_error():
    with pytest.raises(handlers.InvalidSku):
        stocked_bus().handle(commands.CancelAllocation("o1", "MISSING"))


def test_regression_allocation_still_uses_the_next_batch():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "SOFA", 10, None))
    bus.handle(commands.CreateBatch("b2", "SOFA", 10, LATER))
    bus.handle(commands.Allocate("o1", "SOFA", 10))
    bus.handle(commands.Allocate("o2", "SOFA", 10))
    assert available(bus, "SOFA") == [0, 0]


def test_regression_lowering_a_batch_still_reallocates_its_lines():
    bus = make_bus()
    bus.handle(commands.CreateBatch("b1", "DESK", 10, None))
    bus.handle(commands.CreateBatch("b2", "DESK", 10, LATER))
    bus.handle(commands.Allocate("o1", "DESK", 10))
    bus.handle(commands.ChangeBatchQuantity("b1", 5))
    assert available(bus, "DESK") == [5, 0]
