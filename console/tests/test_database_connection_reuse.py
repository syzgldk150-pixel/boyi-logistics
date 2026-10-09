"""Console reuses committed MySQL connections instead of reconnecting per query."""

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from console import database
from console.database import DocumentRepository


class _Connection:
    def __init__(self, number, *, commit_error=None, ping_error=None):
        self.number = number
        self.open = True
        self.commits = 0
        self.rollbacks = 0
        self.pings = 0
        self.commit_error = commit_error
        self.ping_error = ping_error

    def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def ping(self, reconnect=True):
        assert reconnect is False
        self.pings += 1
        if self.ping_error:
            raise self.ping_error

    def close(self):
        self.open = False


class _MySQL:
    cursors = SimpleNamespace(DictCursor=object)

    def __init__(self):
        self.connections = []
        self.next_options = {}

    def connect(self, **_kwargs):
        connection = _Connection(len(self.connections) + 1, **self.next_options)
        self.next_options = {}
        self.connections.append(connection)
        return connection


def _repository():
    settings = SimpleNamespace(
        mysql_host="db.invalid", mysql_port=3306, mysql_user="u", mysql_password="p",
        mysql_database="agent_db", mysql_connect_timeout_seconds=5, mysql_ssl_ca="",
    )
    repository = DocumentRepository(settings)
    repository._mysql = _MySQL()
    return repository


class ConnectionReuseTests(unittest.TestCase):
    def test_committed_connection_is_reused(self):
        repository = _repository()
        with repository.connect() as first:
            pass
        with repository.connect() as second:
            pass
        self.assertIs(first, second)
        self.assertEqual(1, len(repository._mysql.connections))
        self.assertEqual(2, first.commits)
        self.assertTrue(first.open)

    def test_failed_use_rolls_back_and_is_never_reused(self):
        repository = _repository()
        with self.assertRaises(RuntimeError):
            with repository.connect() as failed:
                raise RuntimeError("query failed")
        self.assertEqual((1, False), (failed.rollbacks, failed.open))
        with repository.connect() as fresh:
            pass
        self.assertIsNot(failed, fresh)

    def test_commit_failure_discards_connection(self):
        repository = _repository()
        repository._mysql.next_options = {"commit_error": RuntimeError("commit failed")}
        with self.assertRaises(RuntimeError):
            with repository.connect() as failed:
                pass
        self.assertFalse(failed.open)
        self.assertEqual(0, len(repository._idle_connections))

    def test_idle_connection_is_pinged_and_replaced_when_dead(self):
        repository = _repository()
        clock = [100.0]
        with patch.object(database.time, "monotonic", side_effect=lambda: clock[0]):
            with repository.connect() as first:
                pass
            clock[0] += database._IDLE_PING_AFTER_SECONDS - 1
            with repository.connect() as warm:
                pass
            self.assertIs(first, warm)
            self.assertEqual(0, first.pings)

            clock[0] += database._IDLE_PING_AFTER_SECONDS
            with repository.connect() as checked:
                pass
            self.assertIs(first, checked)
            self.assertEqual(1, first.pings)

            first.ping_error = ConnectionError("server closed connection")
            clock[0] += database._IDLE_PING_AFTER_SECONDS
            with repository.connect() as replacement:
                pass
        self.assertIsNot(first, replacement)
        self.assertFalse(first.open)

    def test_old_connection_is_recycled(self):
        repository = _repository()
        clock = [100.0]
        with patch.object(database.time, "monotonic", side_effect=lambda: clock[0]):
            with repository.connect() as first:
                pass
            clock[0] += database._CONNECTION_MAX_AGE_SECONDS
            with repository.connect() as second:
                pass
        self.assertIsNot(first, second)
        self.assertFalse(first.open)

    def test_idle_pool_is_bounded(self):
        repository = _repository()
        limit = database._IDLE_CONNECTION_LIMIT
        contexts = [repository.connect() for _ in range(limit + 2)]
        connections = [context.__enter__() for context in contexts]
        for context in contexts:
            context.__exit__(None, None, None)
        self.assertEqual(limit, len(repository._idle_connections))
        self.assertEqual(2, sum(not connection.open for connection in connections))


if __name__ == "__main__":
    unittest.main()
