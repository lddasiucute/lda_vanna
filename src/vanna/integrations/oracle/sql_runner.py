"""Oracle implementation of SqlRunner interface."""

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import pandas as pd

from vanna.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from vanna.core.tool import ToolContext

# A PL/SQL block ends with `END;` and the semicolon is part of the syntax.
# Plain SQL, by contrast, must not carry a trailing semicolon over the wire.
_PLSQL_START = re.compile(
    r"^\s*(DECLARE|BEGIN"
    r"|CREATE\s+(OR\s+REPLACE\s+)?"
    r"(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE))\b",
    re.IGNORECASE,
)


class OracleRunner(SqlRunner):
    """Oracle implementation of the SqlRunner interface.

    Connections are pooled and every query runs on a dedicated worker thread, so
    a slow query never blocks the asyncio event loop that serves the streaming
    chat endpoint.
    """

    def __init__(
        self,
        user: str,
        password: str,
        dsn: str,
        min_connections: int = 1,
        max_connections: int = 10,
        call_timeout_ms: Optional[int] = 30000,
        current_schema: Optional[str] = None,
        **kwargs,
    ):
        """Initialize with Oracle connection parameters.

        Args:
            user: Oracle database user name
            password: Oracle database user password
            dsn: Oracle database host - format: host:port/sid
            min_connections: Connections opened eagerly when the pool is created
            max_connections: Upper bound on pooled connections and on concurrent queries
            call_timeout_ms: Per-round-trip timeout; Oracle has no server-side
                `statement_timeout`, so this is enforced client-side. None disables it.
            current_schema: Schema to resolve unqualified table names against,
                the Oracle equivalent of PostgreSQL's `search_path`
            **kwargs: Additional oracledb pool parameters
        """
        try:
            import oracledb

            self.oracledb = oracledb
        except ImportError as e:
            raise ImportError(
                "oracledb package is required. Install with: pip install 'vanna[oracle]'"
            ) from e

        self.user = user
        self.password = password
        self.dsn = dsn
        self.kwargs = kwargs

        self.min_connections = max(1, int(min_connections))
        self.max_connections = max(self.min_connections, int(max_connections))
        self.call_timeout_ms = call_timeout_ms
        self.current_schema = current_schema

        self._pool = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._init_lock = threading.Lock()

    # Internal helpers

    def _session_callback(self, connection, requested_tag) -> None:
        """Per-connection setup, run once when the pool opens a new session.

        Untagged acquires only invoke this for freshly created connections, so
        the settings below are paid for once per connection, not once per query.
        """
        connection.autocommit = True
        if self.call_timeout_ms:
            connection.call_timeout = int(self.call_timeout_ms)
        if self.current_schema:
            connection.current_schema = self.current_schema

    def _get_pool(self):
        """Create the connection pool on first use (thread-safe)."""
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    self._pool = self.oracledb.create_pool(
                        user=self.user,
                        password=self.password,
                        dsn=self.dsn,
                        min=self.min_connections,
                        max=self.max_connections,
                        increment=1,
                        getmode=self.oracledb.POOL_GETMODE_WAIT,
                        session_callback=self._session_callback,
                        **self.kwargs,
                    )
        return self._pool

    def _get_executor(self) -> ThreadPoolExecutor:
        """Bound query concurrency to the pool size so acquire never starves."""
        if self._executor is None:
            with self._init_lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self.max_connections,
                        thread_name_prefix="oracle-runner",
                    )
        return self._executor

    def _execute(self, sql: str) -> pd.DataFrame:
        """Run one statement on a pooled connection. Called from a worker thread."""
        # Oracle rejects the trailing semicolon that a client would send, but
        # stripping it from a PL/SQL block truncates its closing END; and the
        # server answers PLS-00103. Only plain SQL gets the semicolon removed.
        sql = sql.rstrip()
        if sql.endswith(";") and not _PLSQL_START.match(sql):
            sql = sql[:-1]

        pool = self._get_pool()
        conn = pool.acquire()
        discard = False
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql)

                # cursor.description tells us whether the server sent back a
                # result set. Reading it off the cursor also covers WITH, DDL
                # and PL/SQL blocks, which sniffing the first keyword of the
                # statement would get wrong.
                if cursor.description is None:
                    return pd.DataFrame({"rows_affected": [cursor.rowcount]})

                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                if not rows:
                    return pd.DataFrame(columns=columns)
                return pd.DataFrame(rows, columns=columns)
        except Exception:
            # A connection left in a bad state must not go back into the pool.
            discard = True
            raise
        finally:
            if discard:
                pool.drop(conn)
            else:
                pool.release(conn)

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against Oracle database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            oracledb.Error: If query execution fails
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._get_executor(), self._execute, args.sql)

    def close(self) -> None:
        """Close the pool and the query executor."""
        with self._init_lock:
            if self._pool is not None:
                self._pool.close(force=True)
                self._pool = None
            if self._executor is not None:
                self._executor.shutdown(wait=False)
                self._executor = None
