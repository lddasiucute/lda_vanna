"""PostgreSQL implementation of SqlRunner interface."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import pandas as pd

from vanna.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from vanna.core.tool import ToolContext


class PostgresRunner(SqlRunner):
    """PostgreSQL implementation of the SqlRunner interface.

    Connections are pooled and every query runs on a dedicated worker thread, so
    a slow query never blocks the asyncio event loop that serves the streaming
    chat endpoint.
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = 5432,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        min_connections: int = 1,
        max_connections: int = 10,
        statement_timeout_ms: Optional[int] = 30000,
        **kwargs,
    ):
        """Initialize with PostgreSQL connection parameters.

        You can either provide a connection_string OR individual parameters (host, database, etc.).
        If connection_string is provided, it takes precedence.

        Args:
            connection_string: PostgreSQL connection string (e.g., "postgresql://user:password@host:port/database")
            host: Database host address
            port: Database port (default: 5432)
            database: Database name
            user: Database user
            password: Database password
            min_connections: Connections opened eagerly when the pool is created
            max_connections: Upper bound on pooled connections and on concurrent queries
            statement_timeout_ms: Server-side statement timeout; None disables it
            **kwargs: Additional psycopg2 connection parameters (sslmode, connect_timeout, etc.)
        """
        try:
            import psycopg2
            import psycopg2.extras
            import psycopg2.pool

            self.psycopg2 = psycopg2
        except Exception as e:
            raise ImportError(
                "psycopg2 package is required. Install with: pip install 'vanna[postgres]'"
            ) from e

        if connection_string:
            self.connection_string = connection_string
            self.connection_params = None
        elif host and database and user:
            self.connection_string = None
            self.connection_params = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
                **kwargs,
            }
        else:
            raise ValueError(
                "Either provide connection_string OR (host, database, and user) parameters"
            )

        self.min_connections = max(1, int(min_connections))
        self.max_connections = max(self.min_connections, int(max_connections))
        self.statement_timeout_ms = statement_timeout_ms

        self._pool = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._init_lock = threading.Lock()

    # Internal helpers

    def _get_pool(self):
        """Create the connection pool on first use (thread-safe)."""
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    if self.connection_string:
                        kwargs: Dict[str, Any] = {"dsn": self.connection_string}
                    else:
                        kwargs = dict(self.connection_params or {})
                    self._pool = self.psycopg2.pool.ThreadedConnectionPool(
                        self.min_connections, self.max_connections, **kwargs
                    )
        return self._pool

    def _get_executor(self) -> ThreadPoolExecutor:
        """Bound query concurrency to the pool size so getconn never starves."""
        if self._executor is None:
            with self._init_lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self.max_connections,
                        thread_name_prefix="postgres-runner",
                    )
        return self._executor

    def _prepare_connection(self, conn) -> None:
        """Apply per-connection settings once, right after it is opened."""
        if getattr(conn, "_vanna_prepared", False):
            return
        conn.autocommit = True
        if self.statement_timeout_ms:
            with conn.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
        conn._vanna_prepared = True

    def _execute(self, sql: str) -> pd.DataFrame:
        """Run one statement on a pooled connection. Called from a worker thread."""
        pool = self._get_pool()
        conn = pool.getconn()
        discard = False
        try:
            self._prepare_connection(conn)
            with conn.cursor(cursor_factory=self.psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(sql)

                # Determine if this is a SELECT query or modification query
                query_type = sql.strip().upper().split()[0]

                if query_type == "SELECT":
                    rows = cursor.fetchall()
                    if not rows:
                        return pd.DataFrame()
                    return pd.DataFrame([dict(row) for row in rows])

                # For non-SELECT queries (INSERT, UPDATE, DELETE, etc.)
                return pd.DataFrame({"rows_affected": [cursor.rowcount]})
        except Exception:
            # A connection left in a bad state must not go back into the pool.
            discard = True
            raise
        finally:
            pool.putconn(conn, close=discard)

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against PostgreSQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            psycopg2.Error: If query execution fails
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._get_executor(), self._execute, args.sql)

    def close(self) -> None:
        """Close the pool and the query executor."""
        with self._init_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None
            if self._executor is not None:
                self._executor.shutdown(wait=False)
                self._executor = None
