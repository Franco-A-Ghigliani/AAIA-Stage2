"""Acceptance check for the duplicate-allocation bug. Self-contained: no repo test helpers."""

from collections import defaultdict
from datetime import date

from allocation import bootstrap
from allocation.adapters import notifications, repository
from allocation.domain import commands
from allocation.service_layer import unit_of_work

LATER = date(2030, 1, 1)  # a shipment batch; in-stock batches have no ETA


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
    notifs = FakeNotifications()
    published = []
    bus = bootstrap.bootstrap(
        start_orm=False,
        uow=FakeUnitOfWork(),
        notifications=notifs,
        publish=lambda channel, event: published.append(event),
    )
    return bus, notifs, published


def available(bus, sku):
    return [b.available_quantity for b in bus.uow.products.get(sku).batches]


def test_repeated_order_line_is_not_allocated_to_a_second_batch():
    bus, _, _ = make_bus()
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, None))
    bus.handle(commands.CreateBatch("b2", "LAMP", 10, LATER))
    bus.handle(commands.Allocate("o1", "LAMP", 10))
    bus.handle(commands.Allocate("o1", "LAMP", 10))
    assert available(bus, "LAMP") == [0, 10]


def test_repeated_order_line_sends_no_out_of_stock_notification():
    bus, notifs, _ = make_bus()
    bus.handle(commands.CreateBatch("b1", "RUG", 10, None))
    bus.handle(commands.Allocate("o1", "RUG", 10))
    bus.handle(commands.Allocate("o1", "RUG", 10))
    assert notifs.sent == {}


def test_repeated_order_line_publishes_one_allocated_event():
    bus, _, published = make_bus()
    bus.handle(commands.CreateBatch("b1", "DESK", 100, None))
    bus.handle(commands.Allocate("o1", "DESK", 10))
    bus.handle(commands.Allocate("o1", "DESK", 10))
    assert len(published) == 1
    assert available(bus, "DESK") == [90]


def test_a_different_order_still_uses_the_next_batch():
    bus, _, _ = make_bus()
    bus.handle(commands.CreateBatch("b1", "SOFA", 10, None))
    bus.handle(commands.CreateBatch("b2", "SOFA", 10, LATER))
    bus.handle(commands.Allocate("o1", "SOFA", 10))
    bus.handle(commands.Allocate("o2", "SOFA", 10))
    assert available(bus, "SOFA") == [0, 0]
