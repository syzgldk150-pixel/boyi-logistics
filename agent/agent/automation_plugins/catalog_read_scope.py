"""One read transaction per catalog request, without cross-request business caches."""
from contextlib import contextmanager
from contextvars import ContextVar

_READ_TRANSACTION = ContextVar("plugin_catalog_read_transaction", default=None)


@contextmanager
def catalog_read_scope(repository):
    existing = _READ_TRANSACTION.get()
    if existing is not None and existing[0] is repository:
        yield existing[1]
        return
    with repository.unit_of_work() as transaction:
        token = _READ_TRANSACTION.set((repository, transaction, {}, {}))
        try:
            yield transaction
        finally:
            _READ_TRANSACTION.reset(token)


def catalog_version_cache(repository):
    """Immutable package reads reused only within this display transaction."""
    existing = _READ_TRANSACTION.get()
    return existing[2] if existing is not None and existing[0] is repository else None


def catalog_row_cache(repository):
    existing = _READ_TRANSACTION.get()
    return existing[3] if existing is not None and existing[0] is repository else None


@contextmanager
def catalog_read_transaction(repository):
    existing = _READ_TRANSACTION.get()
    if existing is not None and existing[0] is repository:
        yield existing[1]
    else:
        with repository.unit_of_work() as transaction:
            yield transaction
