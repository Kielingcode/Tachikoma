"""Record types and wire serialization for the order pipeline."""

import zlib


class CustomerRecord:
    """Flat wire record for Customer."""

    __slots__ = ('name', 'tier')

    def __init__(self, name, tier):
        self.name = name
        self.tier = tier


class InvoiceRecord:
    """Flat wire record for Invoice."""

    __slots__ = ('invoice_id', 'order_id', 'total_cents')

    def __init__(self, invoice_id, order_id, total_cents):
        self.invoice_id = invoice_id
        self.order_id = order_id
        self.total_cents = total_cents


class OrderRecord:
    """Flat wire record for Order."""

    __slots__ = ('order_id', 'amount_cents')

    def __init__(self, order_id, amount_cents):
        self.order_id = order_id
        self.amount_cents = amount_cents


class ShipmentRecord:
    """Flat wire record for Shipment."""

    __slots__ = ('shipment_id', 'order_id', 'carrier')

    def __init__(self, shipment_id, order_id, carrier):
        self.shipment_id = shipment_id
        self.order_id = order_id
        self.carrier = carrier


FIELD_ORDER = {
    'Customer': ('name', 'tier'),
    'Invoice': ('invoice_id', 'order_id', 'total_cents'),
    'Order': ('order_id', 'amount_cents'),
    'Shipment': ('shipment_id', 'order_id', 'carrier'),
}

SCHEMA_VERSIONS = {
    'Customer': 3713718242,
    'Invoice': 1214547386,
    'Order': 347000405,
    'Shipment': 3590958900,
}

_RECORD_TYPES = {
    'Customer': CustomerRecord,
    'Invoice': InvoiceRecord,
    'Order': OrderRecord,
    'Shipment': ShipmentRecord,
}


def _verify_registry():
    # guard against partial edits to the tables above
    for kind, field_names in FIELD_ORDER.items():
        expected = zlib.crc32(','.join(field_names).encode())
        if expected != SCHEMA_VERSIONS[kind]:
            raise RuntimeError(
                f'record table corrupt for {kind!r} '
                f'(checksum {expected} != {SCHEMA_VERSIONS[kind]})'
            )


_verify_registry()


def to_record(obj):
    kind = type(obj).__name__
    if kind not in FIELD_ORDER:
        raise TypeError(f'no record type for {kind!r}')
    return _RECORD_TYPES[kind](*[getattr(obj, f) for f in FIELD_ORDER[kind]])


def pack(rec):
    kind = type(rec).__name__[: -len('Record')]
    payload = {'kind': kind, 'v': SCHEMA_VERSIONS[kind]}
    for f in FIELD_ORDER[kind]:
        payload[f] = getattr(rec, f)
    return payload


def unpack(payload):
    kind = payload['kind']
    if payload.get('v') != SCHEMA_VERSIONS[kind]:
        raise ValueError(
            f'record schema mismatch for {kind!r}: '
            f'payload v={payload.get("v")}, expected {SCHEMA_VERSIONS[kind]}'
        )
    extra = set(payload) - {'kind', 'v', *FIELD_ORDER[kind]}
    if extra:
        raise ValueError(f'unexpected keys in {kind!r} payload: {sorted(extra)}')
    return _RECORD_TYPES[kind](*[payload[f] for f in FIELD_ORDER[kind]])
