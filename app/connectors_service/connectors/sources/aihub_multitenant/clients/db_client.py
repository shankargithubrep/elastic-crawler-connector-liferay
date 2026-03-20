#
# Liferay AI Hub — Multi-Tenant Connector
# Generic async database client via SQLAlchemy (sync engine + asyncio.to_thread)
#
# Supports any database dialect that SQLAlchemy supports:
#   PostgreSQL:  postgresql+psycopg2://user:pass@host/db
#   MySQL:       mysql+pymysql://user:pass@host/db
#   MSSQL:       mssql+pytds://user:pass@host/db
#   SQLite:      sqlite:///path/to/file.db
#   Oracle:      oracle+cx_oracle://user:pass@host/db
#

import asyncio
import hashlib
import json
from functools import cached_property

from connectors_sdk.logger import logger
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


FETCH_SIZE = 500   # rows per fetchmany() batch


def _row_id(row_dict, index):
    """Generate a stable document ID from row contents + position."""
    payload = json.dumps(row_dict, default=str, sort_keys=True) + str(index)
    return "db-" + hashlib.md5(payload.encode()).hexdigest()


def _serialize_row(row):
    """Convert a SQLAlchemy Row to a plain dict with JSON-safe values."""
    result = {}
    for key, value in row._mapping.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, (bytes, bytearray)):
            result[key] = value.decode(errors="replace")
        else:
            result[key] = value
    return result


class DbClient:
    """Executes a user-supplied SQL query and streams results row by row.

    Uses a synchronous SQLAlchemy engine inside asyncio.to_thread() so that
    any DBAPI driver can be used without requiring async-specific packages.
    """

    def __init__(self, connection_string, sql_query):
        self.connection_string = connection_string
        self.sql_query = sql_query.strip()
        self._logger = logger

    def set_logger(self, logger_):
        self._logger = logger_

    @cached_property
    def _engine(self):
        return create_engine(
            self.connection_string,
            pool_pre_ping=True,
            # Keep pool small — this is a one-shot sync job process
            pool_size=1,
            max_overflow=0,
        )

    def _ping_sync(self):
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    async def ping(self):
        """Verify database connectivity (runs in thread pool)."""
        await asyncio.to_thread(self._ping_sync)

    def _stream_rows_sync(self):
        """Generator that yields serialized row dicts from the query."""
        with self._engine.connect() as conn:
            result = conn.execute(text(self.sql_query))
            index = 0
            while True:
                batch = result.fetchmany(FETCH_SIZE)
                if not batch:
                    break
                for row in batch:
                    row_dict = _serialize_row(row)
                    row_dict["id"] = _row_id(row_dict, index)
                    yield row_dict
                    index += 1

    async def query(self):
        """Async generator: yields one doc dict per result row.

        Runs the blocking fetchmany() loop in a thread pool and feeds
        rows back into the async context via an asyncio.Queue.
        """
        queue = asyncio.Queue(maxsize=FETCH_SIZE * 2)
        _SENTINEL = object()

        async def _producer():
            loop = asyncio.get_event_loop()
            try:
                rows = await loop.run_in_executor(
                    None, lambda: list(self._stream_rows_sync())
                )
                for row in rows:
                    await queue.put(row)
            except SQLAlchemyError as e:
                self._logger.error(f"Database query failed: {e}")
            finally:
                await queue.put(_SENTINEL)

        producer_task = asyncio.create_task(_producer())

        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

    def close(self):
        """Dispose of the connection pool."""
        if "_engine" in self.__dict__:
            self._engine.dispose()
