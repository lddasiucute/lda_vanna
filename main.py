import importlib.util
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _restart_with_project_python() -> None:
    """Use the prepared project environment when the active Python lacks PostgreSQL."""
    if importlib.util.find_spec("psycopg2") is not None:
        return

    project_python = (
        Path(__file__).resolve().parent.parent
        / ".venv-1"
        / "Scripts"
        / "python.exe"
    )
    if __name__ == "__main__" and project_python.exists():
        print(
            f"psycopg2 is missing from {sys.executable}; "
            f"restarting with {project_python}",
            flush=True,
        )
        return_code = subprocess.call(
            [str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]]
        )
        raise SystemExit(return_code)


_restart_with_project_python()

from fastapi import FastAPI

from vanna import Agent, AgentConfig
from vanna.core.llm import LlmRequest, LlmResponse, LlmStreamChunk
from vanna.core.registry import ToolRegistry
from vanna.core.system_prompt import DefaultSystemPromptBuilder
from vanna.core.tool import ToolCall
from vanna.core.user import RequestContext, User, UserResolver
from vanna.integrations.local import LocalFileSystem
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.ollama import OllamaLlmService
from vanna.integrations.oracle import OracleRunner
from vanna.integrations.postgres import PostgresRunner
from vanna.servers.base import ChatHandler
from vanna.servers.fastapi.routes import register_chat_routes
from vanna.tools import RunSqlTool, VisualizeDataTool

# Load local configuration from the .env file next to this script.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    # Keep local setup working even when python-dotenv is not installed.
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))

DB_PATH = Path(__file__).parent / "sales_dashboard.db"
RESULTS_PATH = Path(__file__).parent / "query_results"

# --- SQL dialect ---------------------------------------------------------
# The deterministic SQL below and the model instructions both hard-code a
# dialect, so the backend has to be known before either is built.
#
# The default is oracle. Running without an Oracle instance therefore fails at
# startup rather than silently falling back, which is deliberate: a silent
# fallback to Neon would answer questions from a different database than the
# one the operator configured. Set DATABASE_BACKEND=postgres to use Neon.
DATABASE_BACKEND = os.getenv("DATABASE_BACKEND", "oracle").strip().lower()
if DATABASE_BACKEND not in ("postgres", "oracle"):
    raise RuntimeError("DATABASE_BACKEND must be either 'postgres' or 'oracle'.")

IS_ORACLE = DATABASE_BACKEND == "oracle"


def _limit(n: int) -> str:
    """Row-limiting clause. Oracle has no LIMIT; it uses the SQL standard form."""
    return f"FETCH FIRST {n} ROWS ONLY" if IS_ORACLE else f"LIMIT {n}"


def _as(table: str, alias: str) -> str:
    """Table alias. Oracle rejects AS before one (ORA-00933); Postgres allows both."""
    return f"{table} {alias}" if IS_ORACLE else f"{table} AS {alias}"


_ORACLE_RULES = """
CRITICAL SQL DIALECT — Oracle, not PostgreSQL or MySQL:
- To return only the first N rows write: ORDER BY ... FETCH FIRST N ROWS ONLY.
  The keyword LIMIT does not exist in Oracle and raises ORA-03049. Never write
  LIMIT. Never write OFFSET n LIMIT m.
  WRONG: SELECT code FROM qlsp_backup.units ORDER BY id LIMIT 5
  RIGHT: SELECT code FROM qlsp_backup.units ORDER BY id FETCH FIRST 5 ROWS ONLY
- Never write AS before a table alias (ORA-00933):
  WRONG: FROM qlsp_backup.units AS u     RIGHT: FROM qlsp_backup.units u
  AS before a column alias is correct and expected.
- Concatenate with ||. Use NVL, not COALESCE-only PostgreSQL idioms.
- Do not end the statement with a semicolon.
"""


def _apply_dialect(prompt: str) -> str:
    """Fill the dialect-dependent placeholders in the system prompt."""
    return (
        prompt.replace("__ENGINE__", "Oracle" if IS_ORACLE else "PostgreSQL")
        .replace(
            "__SCHEMA_CATALOG__",
            "all_tab_columns" if IS_ORACLE else "information_schema.columns",
        )
        .replace("__DIALECT_RULES__", _ORACLE_RULES if IS_ORACLE else "")
    )

app = FastAPI()


class MyUserResolver(UserResolver):
    async def resolve_user(self, request_context: RequestContext) -> User:
        return User(
            id="local-user",
            email="you@example.com",
            group_memberships=["read_qlsp"],
        )


class ChartAwareOllamaLlmService(OllamaLlmService):
    """Keep local-model SQL and chart tool use finite and deterministic."""

    _chart_terms = (
        "biểu đồ",
        "bieu do",
        "vẽ",
        "ve ",
        "chart",
        "plot",
        "graph",
        "visualize",
    )
    _result_file_pattern = re.compile(
        r"Results saved to file:\s*([^\s*]+\.csv)", re.IGNORECASE
    )
    _return_rate_terms = (
        "tỷ lệ hoàn hàng",
        "ty le hoan hang",
        "tỷ lệ trả hàng",
        "ty le tra hang",
        "return rate",
    )
    _category_terms = (
        "danh mục",
        "danh muc",
        "category",
        "categories",
    )
    _revenue_terms = ("doanh thu", "revenue")
    _region_terms = ("khu vực", "khu vuc", "region")

    @staticmethod
    def _latest_user(request: LlmRequest) -> tuple[int, str]:
        for index in range(len(request.messages) - 1, -1, -1):
            message = request.messages[index]
            if message.role == "user":
                return index, message.content or ""
        return -1, ""

    def _chart_tool_call(self, request: LlmRequest) -> ToolCall | None:
        user_index, user_text = self._latest_user(request)
        if user_index < 0:
            return None

        normalized_question = user_text.casefold()
        if not any(term in normalized_question for term in self._chart_terms):
            return None

        match = None
        for message in reversed(request.messages[user_index + 1 :]):
            if message.role != "tool":
                continue
            match = self._result_file_pattern.search(message.content or "")
            if match:
                break
        if not match:
            return None

        return ToolCall(
            id=f"chart_{uuid.uuid4().hex[:8]}",
            name="visualize_data",
            arguments={
                "filename": match.group(1),
                "title": user_text.rstrip(" .?!") or "Biểu đồ dữ liệu",
            },
        )

    def _region_revenue_tool_call(self, request: LlmRequest) -> ToolCall | None:
        """Use the canonical realized-revenue query for region questions."""
        user_index, user_text = self._latest_user(request)
        if user_index < 0:
            return None

        normalized_question = user_text.casefold()
        if not (
            any(term in normalized_question for term in self._revenue_terms)
            and any(term in normalized_question for term in self._region_terms)
        ):
            return None

        if any(
            message.role == "tool"
            for message in request.messages[user_index + 1 :]
        ):
            return None

        return ToolCall(
            id=f"region_revenue_{uuid.uuid4().hex[:8]}",
            name="run_sql",
            arguments={
                "sql": """
SELECT
    region,
    ROUND(SUM(revenue), 2) AS revenue
FROM sales_enriched
WHERE status = 'Completed'
GROUP BY region
ORDER BY revenue DESC
""".strip()
            },
        )

    def _database_counts_tool_call(self, request: LlmRequest) -> ToolCall | None:
        """Avoid a Cartesian product when checking the three QLSP table counts."""
        user_index, user_text = self._latest_user(request)
        if user_index < 0 or self._has_tool_result(request):
            return None

        normalized = user_text.casefold()
        user_terms = ("người dùng", "nguoi dung", "user")
        unit_terms = ("đơn vị", "don vi", "unit")
        ticket_terms = ("ticket", "phiếu yêu cầu", "phieu yeu cau")
        if not (
            any(term in normalized for term in user_terms)
            and any(term in normalized for term in unit_terms)
            and any(term in normalized for term in ticket_terms)
        ):
            return None

        return ToolCall(
            id=f"database_counts_{uuid.uuid4().hex[:8]}",
            name="run_sql",
            arguments={
                "sql": """
SELECT
    (SELECT COUNT(*) FROM qlsp_backup.users) AS total_users,
    (SELECT COUNT(*) FROM qlsp_backup.units) AS total_units,
    (SELECT COUNT(*) FROM qlsp_backup.spdv_tickets) AS total_spdv_tickets
""".strip()
            },
        )

    def _known_counts_tool_call(self, request: LlmRequest) -> ToolCall | None:
        """Map common Vietnamese count questions to independent table counts."""
        user_index, user_text = self._latest_user(request)
        if user_index < 0 or self._has_tool_result(request):
            return None

        normalized = user_text.casefold()
        count_cues = (
            "bao nhiêu",
            "bao nhieu",
            "tổng số",
            "tong so",
            "số lượng",
            "so luong",
            "đếm",
            "dem ",
            "count",
        )
        grouped_cues = (
            " theo ",
            " từng ",
            " moi ",
            " mỗi ",
            " by ",
            "top ",
            "liệt kê",
            "liet ke",
        )
        if not any(cue in normalized for cue in count_cues):
            return None
        if any(cue in f" {normalized} " for cue in grouped_cues):
            return None

        specs: list[tuple[str, str]] = []

        def add(alias: str, table: str) -> None:
            if (alias, table) not in specs:
                specs.append((alias, table))

        mappings = (
            (
                "total_plan_applications",
                "plan_applications",
                (
                    "hồ sơ kế hoạch",
                    "ho so ke hoach",
                    "plan application",
                    "plan_applications",
                ),
            ),
            (
                "total_plan_items",
                "plan_items",
                (
                    "hạng mục kế hoạch",
                    "hang muc ke hoach",
                    "plan item",
                    "plan_items",
                ),
            ),
            (
                "total_users",
                "users",
                ("người dùng", "nguoi dung", "users"),
            ),
            (
                "total_units",
                "units",
                ("đơn vị", "don vi", "units"),
            ),
            (
                "total_spdv_categories",
                "spdv_categories",
                (
                    "danh mục spdv",
                    "danh muc spdv",
                    "spdv_categories",
                ),
            ),
            (
                "total_documents",
                "documents",
                ("tài liệu", "tai lieu", "documents"),
            ),
            (
                "total_wiki_pages",
                "wiki_pages",
                ("trang wiki", "wiki page", "wiki_pages"),
            ),
        )
        for alias, table, terms in mappings:
            if any(term in normalized for term in terms):
                add(alias, table)

        has_ticket_events = any(
            term in normalized
            for term in (
                "sự kiện ticket",
                "su kien ticket",
                "ticket event",
                "spdv_ticket_events",
            )
        )
        if has_ticket_events:
            add("total_ticket_events", "spdv_ticket_events")
        elif any(
            term in normalized
            for term in ("ticket spdv", "ticket", "spdv_tickets")
        ):
            add("total_spdv_tickets", "spdv_tickets")

        if not specs:
            return None

        select_items = [
            f"(SELECT COUNT(*) FROM qlsp_backup.{table}) AS {alias}"
            for alias, table in specs
        ]
        return ToolCall(
            id=f"known_counts_{uuid.uuid4().hex[:8]}",
            name="run_sql",
            arguments={"sql": "SELECT\n    " + ",\n    ".join(select_items)},
        )

    def _qlsp_analysis_tool_call(self, request: LlmRequest) -> ToolCall | None:
        """Provide reliable SQL for the main QLSP demonstration questions."""
        user_index, user_text = self._latest_user(request)
        if user_index < 0 or self._has_tool_result(request):
            return None

        normalized = user_text.casefold()

        def contains(*terms: str) -> bool:
            return any(term in normalized for term in terms)

        sql: str | None = None
        if (
            contains("người dùng", "nguoi dung", "users")
            and contains("trạng thái", "trang thai", "status")
        ):
            sql = """
SELECT
    status,
    COUNT(*) AS total_users
FROM qlsp_backup.users
GROUP BY status
ORDER BY total_users DESC, status
""".strip()
        elif (
            contains("ticket")
            and contains("trạng thái", "trang thai", "status")
        ):
            sql = """
SELECT
    trang_thai,
    COUNT(*) AS total_tickets
FROM qlsp_backup.spdv_tickets
GROUP BY trang_thai
ORDER BY total_tickets DESC, trang_thai
""".strip()
        elif (
            contains("ticket")
            and contains("mức ưu tiên", "muc uu tien", "ưu tiên", "uu tien")
        ):
            sql = """
SELECT
    muc_uu_tien,
    COUNT(*) AS total_tickets
FROM qlsp_backup.spdv_tickets
GROUP BY muc_uu_tien
ORDER BY total_tickets DESC, muc_uu_tien
""".strip()
        elif (
            contains("đơn vị", "don vi", "units")
            and contains("người dùng", "nguoi dung", "users")
            and contains("nhiều", "nhieu", "top")
        ):
            sql = f"""
SELECT
    u.code,
    u.name,
    COUNT(us.id) AS total_users
FROM {_as("qlsp_backup.units", "u")}
LEFT JOIN {_as("qlsp_backup.users", "us")} ON us.unit_id = u.id
GROUP BY u.id, u.code, u.name
ORDER BY total_users DESC, u.code
{_limit(10)}
""".strip()
        elif (
            contains("danh mục", "danh muc", "spdv")
            and contains("ticket")
            and contains("nhiều", "nhieu", "top")
        ):
            sql = f"""
SELECT
    c.code,
    c.name,
    COUNT(t.id) AS total_tickets
FROM {_as("qlsp_backup.spdv_categories", "c")}
LEFT JOIN {_as("qlsp_backup.spdv_tickets", "t")} ON t.spdv_id = c.id
GROUP BY c.id, c.code, c.name
ORDER BY total_tickets DESC, c.code
{_limit(10)}
""".strip()
        elif (
            contains("ticket")
            and contains("tháng", "thang", "monthly", "month")
        ):
            sql = """
SELECT
    TO_CHAR(created_at, 'YYYY-MM') AS month,
    COUNT(*) AS total_tickets
FROM qlsp_backup.spdv_tickets
GROUP BY TO_CHAR(created_at, 'YYYY-MM')
ORDER BY month
""".strip()

        if sql is None:
            return None
        return ToolCall(
            id=f"qlsp_analysis_{uuid.uuid4().hex[:8]}",
            name="run_sql",
            arguments={"sql": sql},
        )

    def _text_tool_call(
        self, request: LlmRequest, content: str | None
    ) -> ToolCall | None:
        """Convert tool-call JSON emitted as plain model text into a real call."""
        if not content:
            return None

        user_index, _ = self._latest_user(request)
        if user_index < 0:
            return None

        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", content):
            try:
                payload, _ = decoder.raw_decode(content[match.start() :])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            name = payload.get("name")
            arguments = payload.get("parameters", payload.get("arguments"))
            if name not in {"run_sql", "visualize_data"}:
                continue
            if not isinstance(arguments, dict):
                continue
            if name == "run_sql":
                sql = str(arguments.get("sql", "")).lstrip()
                if not re.match(r"(?is)^(SELECT|WITH)\b", sql):
                    continue

            return ToolCall(
                id=f"text_tool_{uuid.uuid4().hex[:8]}",
                name=name,
                arguments=arguments,
            )
        return None

    @staticmethod
    def _has_tool_result(request: LlmRequest) -> bool:
        user_index, _ = ChartAwareOllamaLlmService._latest_user(request)
        return user_index >= 0 and any(
            message.role == "tool"
            for message in request.messages[user_index + 1 :]
        )

    # RunSqlTool puts one of these in every successful result. Their absence is
    # the only reliable failure signal here: the tool reports errors in its
    # message rather than raising, and the framework strips the tool's
    # "Error executing query: " prefix before the message reaches us.
    _sql_success_markers = (
        "Results saved to file:",
        "Query executed successfully.",
    )

    @staticmethod
    def _failed_tool_error(request: LlmRequest) -> str | None:
        """Error text of the most recent run_sql call, or None if it succeeded.

        Without this check a query that errored still receives the cheerful
        completion message below. That is how a completely broken SQL layer
        managed to look identical to a healthy one, both to the user and to
        any load test that only checks the stream reached [DONE].
        """
        user_index, _ = ChartAwareOllamaLlmService._latest_user(request)
        if user_index < 0:
            return None

        tail = request.messages[user_index + 1 :]
        for position in range(len(tail) - 1, -1, -1):
            message = tail[position]
            if message.role != "tool":
                continue
            # Only run_sql failures are worth reporting as a data problem, so
            # resolve the call id back to the tool that produced it.
            name = None
            for earlier in reversed(tail[:position]):
                for call in earlier.tool_calls or []:
                    if call.id == message.tool_call_id:
                        name = call.name
                        break
                if name:
                    break
            if name not in (None, "run_sql"):
                return None

            content = (message.content or "").strip()
            if any(m in content for m in ChartAwareOllamaLlmService._sql_success_markers):
                return None
            return content or None
        return None

    @staticmethod
    def _tool_finished_message(request: LlmRequest | None = None) -> str:
        error = (
            ChartAwareOllamaLlmService._failed_tool_error(request)
            if request is not None
            else None
        )
        if error:
            detail = error.splitlines()[0].removeprefix("Error executing query: ").strip()
            return (
                "Truy vấn không chạy được nên không có dữ liệu để hiển thị. "
                f"Lỗi từ cơ sở dữ liệu: {detail}"
            )
        return (
            "Đã hoàn tất truy vấn. Kết quả chính xác được hiển thị "
            "trong bảng phía trên."
        )

    def _private_joke_message(self, request: LlmRequest) -> str | None:
        """Handle explicitly enabled private jokes without writing fake DB facts."""
        enabled = os.getenv("ENABLE_PRIVATE_JOKES", "false").casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not enabled:
            return None

        user_index, user_text = self._latest_user(request)
        if user_index < 0 or self._has_tool_result(request):
            return None
        normalized = user_text.casefold().strip()

        nga_terms = ("nga lê", "nga le")
        silly_terms = ("có hâm không", "co ham khong")
        bullying_terms = (
            "có bắt nạt ducanh không",
            "co bat nat ducanh khong",
            "có bắt nạt đức anh không",
            "co bat nat duc anh khong",
        )
        if any(term in normalized for term in nga_terms) and any(
            term in normalized for term in silly_terms
        ):
            return (
                "Có 😄 — đây là câu trả lời đùa riêng do Đức Anh cấu hình, "
                "không phải dữ liệu hay kết luận thật về Nga Lê."
            )
        if any(term in normalized for term in nga_terms) and any(
            term in normalized for term in bullying_terms
        ):
            return (
                "Có 😄 — đây là kịch bản đùa riêng do Đức Anh cấu hình, "
                "không phải cáo buộc hoặc dữ liệu xác thực về Nga Lê."
            )
        return None

    def _off_topic_message(self, request: LlmRequest) -> str | None:
        """Keep unrelated chat from replaying the previous SQL tool call."""
        user_index, user_text = self._latest_user(request)
        if user_index < 0 or self._has_tool_result(request):
            return None

        normalized = user_text.casefold().strip()
        if not normalized:
            return None

        data_terms = (
            "dữ liệu",
            "du lieu",
            "database",
            "postgres",
            "sql",
            "bảng",
            "bang ",
            "cột",
            "cot ",
            "thống kê",
            "thong ke",
            "đếm",
            "dem ",
            "tổng",
            "tong ",
            "trung bình",
            "trung binh",
            "liệt kê",
            "liet ke",
            "danh sách",
            "danh sach",
            "tìm",
            "tim ",
            "kiểm tra",
            "kiem tra",
            "phân tích",
            "phan tich",
            "biểu đồ",
            "bieu do",
            "ticket",
            "spdv",
            "người dùng",
            "nguoi dung",
            "user",
            "đơn vị",
            "don vi",
            "kế hoạch",
            "ke hoach",
            "tài liệu",
            "tai lieu",
            "wiki",
            "báo cáo",
            "bao cao",
            "chương trình",
            "chuong trinh",
            "trạng thái",
            "trang thai",
            "ưu tiên",
            "uu tien",
            "doanh thu",
            "chi phí",
            "chi phi",
            "sla",
            "qlsp",
        )
        if any(term in normalized for term in data_terms):
            return None

        if normalized in {"chào", "chao", "hello", "hi", "xin chào", "xin chao"}:
            return (
                "Chào bạn! Tôi hỗ trợ truy vấn và phân tích dữ liệu QLSP/SPDV "
                "trên PostgreSQL."
            )
        return (
            "Câu hỏi này nằm ngoài phạm vi phân tích dữ liệu QLSP/SPDV. "
            "Tôi không đánh giá cá nhân; bạn có thể hỏi về người dùng, đơn vị, "
            "ticket, kế hoạch, SPDV hoặc báo cáo trong cơ sở dữ liệu."
        )

    def _return_rate_tool_call(self, request: LlmRequest) -> ToolCall | None:
        """Use the agreed business formula instead of asking a small model to infer it."""
        user_index, user_text = self._latest_user(request)
        if user_index < 0:
            return None

        normalized_question = user_text.casefold()
        if not (
            any(term in normalized_question for term in self._return_rate_terms)
            and any(term in normalized_question for term in self._category_terms)
        ):
            return None

        if any(
            message.role == "tool"
            for message in request.messages[user_index + 1 :]
        ):
            return None

        return ToolCall(
            id=f"return_rate_{uuid.uuid4().hex[:8]}",
            name="run_sql",
            arguments={
                "sql": """
SELECT
    category,
    COUNT(DISTINCT order_id)
        FILTER (WHERE status = 'Returned') AS returned_orders,
    COUNT(DISTINCT order_id) AS total_orders,
    ROUND(
        100.0 * COUNT(DISTINCT order_id)
            FILTER (WHERE status = 'Returned')
        / NULLIF(COUNT(DISTINCT order_id), 0),
        2
    ) AS return_rate_pct
FROM sales_enriched
GROUP BY category
ORDER BY return_rate_pct DESC
""".strip()
            },
        )

    @staticmethod
    def _visualization_finished(request: LlmRequest) -> bool:
        user_index, _ = ChartAwareOllamaLlmService._latest_user(request)
        return any(
            message.role == "tool"
            and (message.content or "").startswith("Created visualization from ")
            for message in request.messages[user_index + 1 :]
        )

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        if self._visualization_finished(request):
            return LlmResponse(
                content="Đã tạo biểu đồ từ kết quả truy vấn.",
                finish_reason="stop",
            )
        private_joke_message = self._private_joke_message(request)
        if private_joke_message:
            return LlmResponse(
                content=private_joke_message,
                finish_reason="stop",
            )
        off_topic_message = self._off_topic_message(request)
        if off_topic_message:
            return LlmResponse(
                content=off_topic_message,
                finish_reason="stop",
            )
        qlsp_analysis_call = self._qlsp_analysis_tool_call(request)
        if qlsp_analysis_call:
            return LlmResponse(
                tool_calls=[qlsp_analysis_call],
                finish_reason="tool_calls",
            )
        known_counts_call = self._known_counts_tool_call(request)
        if known_counts_call:
            return LlmResponse(
                tool_calls=[known_counts_call],
                finish_reason="tool_calls",
            )
        database_counts_call = self._database_counts_tool_call(request)
        if database_counts_call:
            return LlmResponse(
                tool_calls=[database_counts_call],
                finish_reason="tool_calls",
            )
        chart_call = self._chart_tool_call(request)
        if chart_call:
            return LlmResponse(tool_calls=[chart_call], finish_reason="tool_calls")
        if self._has_tool_result(request):
            return LlmResponse(
                content=self._tool_finished_message(request),
                finish_reason="stop",
            )
        response = await super().send_request(request)
        if response.is_tool_call() and self._has_tool_result(request):
            return LlmResponse(
                content=self._tool_finished_message(request),
                finish_reason="stop",
                usage=response.usage,
            )
        text_tool_call = self._text_tool_call(request, response.content)
        if text_tool_call:
            if self._has_tool_result(request):
                return LlmResponse(
                    content=self._tool_finished_message(request),
                    finish_reason="stop",
                    usage=response.usage,
                )
            return LlmResponse(
                tool_calls=[text_tool_call],
                finish_reason="tool_calls",
                usage=response.usage,
            )
        return response

    async def stream_request(
        self, request: LlmRequest
    ) -> AsyncGenerator[LlmStreamChunk, None]:
        if self._visualization_finished(request):
            yield LlmStreamChunk(content="Đã tạo biểu đồ từ kết quả truy vấn.")
            yield LlmStreamChunk(finish_reason="stop")
            return
        private_joke_message = self._private_joke_message(request)
        if private_joke_message:
            yield LlmStreamChunk(content=private_joke_message)
            yield LlmStreamChunk(finish_reason="stop")
            return
        off_topic_message = self._off_topic_message(request)
        if off_topic_message:
            yield LlmStreamChunk(content=off_topic_message)
            yield LlmStreamChunk(finish_reason="stop")
            return
        qlsp_analysis_call = self._qlsp_analysis_tool_call(request)
        if qlsp_analysis_call:
            yield LlmStreamChunk(
                tool_calls=[qlsp_analysis_call],
                finish_reason="tool_calls",
            )
            return
        known_counts_call = self._known_counts_tool_call(request)
        if known_counts_call:
            yield LlmStreamChunk(
                tool_calls=[known_counts_call],
                finish_reason="tool_calls",
            )
            return
        database_counts_call = self._database_counts_tool_call(request)
        if database_counts_call:
            yield LlmStreamChunk(
                tool_calls=[database_counts_call],
                finish_reason="tool_calls",
            )
            return
        chart_call = self._chart_tool_call(request)
        if chart_call:
            yield LlmStreamChunk(
                tool_calls=[chart_call],
                finish_reason="tool_calls",
            )
            return
        if self._has_tool_result(request):
            yield LlmStreamChunk(content=self._tool_finished_message(request))
            yield LlmStreamChunk(finish_reason="stop")
            return

        accumulated_content = ""
        accumulated_tool_calls: list[ToolCall] = []
        finish_reason = "stop"
        async for chunk in super().stream_request(request):
            if chunk.content:
                accumulated_content += chunk.content
            if chunk.tool_calls:
                accumulated_tool_calls.extend(chunk.tool_calls)
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        if accumulated_tool_calls:
            if self._has_tool_result(request):
                yield LlmStreamChunk(content=self._tool_finished_message(request))
                yield LlmStreamChunk(finish_reason="stop")
                return
            yield LlmStreamChunk(
                tool_calls=accumulated_tool_calls,
                finish_reason=finish_reason,
            )
            return

        text_tool_call = self._text_tool_call(request, accumulated_content)
        if text_tool_call:
            if self._has_tool_result(request):
                yield LlmStreamChunk(content=self._tool_finished_message(request))
                yield LlmStreamChunk(finish_reason="stop")
                return
            yield LlmStreamChunk(
                tool_calls=[text_tool_call],
                finish_reason="tool_calls",
            )
            return

        if accumulated_content:
            yield LlmStreamChunk(content=accumulated_content)
        yield LlmStreamChunk(finish_reason=finish_reason)


class TrackingRunSqlTool(RunSqlTool):
    def __init__(
        self,
        *args,
        latest_files: dict[tuple[str, str], str],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.latest_files = latest_files

    async def execute(self, context, args):
        start = time.perf_counter()
        result = await super().execute(context, args)
        elapsed = time.perf_counter() - start

        # Ghi thời gian chạy lệnh vào metadata và phần trả về cho mô hình
        result.metadata["execution_time_seconds"] = round(elapsed, 3)
        timing_note = f"⏱️ Thời gian chạy lệnh: {elapsed:.3f} giây"
        result.result_for_llm = f"{result.result_for_llm}\n\n{timing_note}"

        # Hiển thị thời gian trong UI (component văn bản đơn giản), nếu có
        simple = getattr(result.ui_component, "simple_component", None)
        if simple is not None and hasattr(simple, "text"):
            simple.text = f"{simple.text}\n\n{timing_note}"

        # A failed query still streams a complete, cheerful answer, so without
        # this line a broken SQL layer looks identical to a healthy one in the
        # logs and in any load test that only checks for [DONE].
        if not result.success:
            print(
                f"run_sql THAT BAI: {result.result_for_llm[:600]}\n"
                f"  SQL: {getattr(args, 'sql', '?')}",
                flush=True,
            )

        output_file = result.metadata.get("output_file")
        if result.success and output_file:
            key = (context.user.id, context.conversation_id)
            self.latest_files[key] = output_file
        return result


class ResilientVisualizeDataTool(VisualizeDataTool):
    def __init__(
        self,
        *args,
        latest_files: dict[tuple[str, str], str],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.latest_files = latest_files

    async def _latest_csv(self, context) -> str | None:
        key = (context.user.id, context.conversation_id)
        tracked_file = self.latest_files.get(key)
        if tracked_file and await self.file_system.exists(tracked_file, context):
            return tracked_file

        try:
            csv_files = [
                name
                for name in await self.file_system.list_files(".", context)
                if name.lower().endswith(".csv")
            ]
        except (FileNotFoundError, NotADirectoryError):
            return None

        if not csv_files:
            return None

        if isinstance(self.file_system, LocalFileSystem):
            return max(
                csv_files,
                key=lambda name: self.file_system._resolve_path(name, context)
                .stat()
                .st_mtime_ns,
            )
        return csv_files[-1]

    async def execute(self, context, args):
        if not await self.file_system.exists(args.filename, context):
            latest_file = await self._latest_csv(context)
            if latest_file:
                args = args.model_copy(update={"filename": latest_file})
        return await super().execute(context, args)


def seed_database() -> None:
    """Create a deterministic multi-table sales dataset for the demo."""
    conn = sqlite3.connect(DB_PATH)
    try:
        existing_orders = conn.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'orders'
            """
        ).fetchone()[0]
        if existing_orders:
            order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if order_count == 10_000:
                return

        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            DROP VIEW IF EXISTS sales_enriched;
            DROP TABLE IF EXISTS orders;
            DROP TABLE IF EXISTS products;
            DROP TABLE IF EXISTS customers;

            CREATE TABLE customers (
                customer_id   INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL,
                segment       TEXT NOT NULL,
                region        TEXT NOT NULL,
                signup_date   TEXT NOT NULL
            );

            CREATE TABLE products (
                product_id   INTEGER PRIMARY KEY,
                product_name TEXT NOT NULL,
                category     TEXT NOT NULL,
                unit_price   REAL NOT NULL,
                unit_cost    REAL NOT NULL
            );

            CREATE TABLE orders (
                order_id     INTEGER PRIMARY KEY,
                customer_id  INTEGER NOT NULL,
                product_id   INTEGER NOT NULL,
                quantity     INTEGER NOT NULL,
                discount_pct REAL NOT NULL,
                order_date   TEXT NOT NULL,
                status       TEXT NOT NULL,
                channel      TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            CREATE INDEX idx_orders_date ON orders(order_date);
            CREATE INDEX idx_orders_customer ON orders(customer_id);
            CREATE INDEX idx_orders_product ON orders(product_id);
            """
        )

        rng = random.Random(42)
        regions = ["North", "South", "East", "West", "Central"]
        segments = ["Consumer", "Corporate", "Small Business"]
        channels = ["Online", "Retail", "Partner"]
        first_names = [
            "An", "Binh", "Chi", "Dung", "Giang", "Ha", "Hieu", "Khanh",
            "Lan", "Linh", "Mai", "Minh", "Nam", "Ngoc", "Phong", "Quang",
            "Son", "Thao", "Trang", "Tuan",
        ]
        last_names = [
            "Nguyen", "Tran", "Le", "Pham", "Hoang",
            "Phan", "Vu", "Vo", "Dang", "Bui",
        ]

        customers = []
        for customer_id in range(1, 501):
            signup = date(2023, 1, 1) + timedelta(days=rng.randrange(730))
            customers.append(
                (
                    customer_id,
                    f"{rng.choice(last_names)} {rng.choice(first_names)} {customer_id:03d}",
                    rng.choices(segments, weights=[55, 25, 20], k=1)[0],
                    rng.choices(regions, weights=[24, 27, 17, 18, 14], k=1)[0],
                    signup.isoformat(),
                )
            )
        conn.executemany(
            """
            INSERT INTO customers
                (customer_id, customer_name, segment, region, signup_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            customers,
        )

        products = [
            (1, "Laptop Pro 14", "Electronics", 1299.0, 910.0),
            (2, "Laptop Air 13", "Electronics", 899.0, 650.0),
            (3, "Monitor 27 inch", "Electronics", 329.0, 215.0),
            (4, "Tablet 11 inch", "Electronics", 549.0, 380.0),
            (5, "Wireless Keyboard", "Accessories", 79.0, 34.0),
            (6, "Wireless Mouse", "Accessories", 49.0, 19.0),
            (7, "USB-C Hub", "Accessories", 69.0, 31.0),
            (8, "Webcam HD", "Accessories", 89.0, 42.0),
            (9, "Office Chair", "Furniture", 259.0, 155.0),
            (10, "Standing Desk", "Furniture", 499.0, 315.0),
            (11, "Desk Lamp", "Furniture", 59.0, 25.0),
            (12, "Bookshelf", "Furniture", 179.0, 102.0),
            (13, "Printer Laser", "Office", 349.0, 238.0),
            (14, "Paper A4 Box", "Office", 42.0, 24.0),
            (15, "Notebook Pack", "Office", 18.0, 7.0),
            (16, "Premium Pen Set", "Office", 29.0, 11.0),
            (17, "Noise-Cancel Headset", "Accessories", 189.0, 105.0),
            (18, "Portable SSD 1TB", "Electronics", 139.0, 82.0),
            (19, "Wi-Fi Router", "Electronics", 119.0, 67.0),
            (20, "Ergonomic Footrest", "Furniture", 75.0, 38.0),
        ]
        conn.executemany(
            """
            INSERT INTO products
                (product_id, product_name, category, unit_price, unit_cost)
            VALUES (?, ?, ?, ?, ?)
            """,
            products,
        )

        customer_regions = {row[0]: row[3] for row in customers}
        start_date = date(2025, 1, 1)
        total_days = (date(2026, 6, 30) - start_date).days + 1
        orders = []
        for order_id in range(1, 10_001):
            customer_id = rng.randint(1, 500)
            product_id = rng.choices(
                range(1, 21),
                weights=[
                    4, 6, 8, 5, 13, 15, 12, 9, 6, 4,
                    9, 5, 5, 10, 14, 12, 7, 9, 8, 7,
                ],
                k=1,
            )[0]
            order_date = start_date + timedelta(days=rng.randrange(total_days))
            quantity = rng.choices([1, 2, 3, 4, 5], weights=[45, 28, 15, 8, 4], k=1)[0]
            discount_choices = [0, 5, 10, 15, 20, 25]
            discount_weights = [36, 24, 19, 12, 7, 2]
            if customer_regions[customer_id] == "West":
                discount_weights = [18, 20, 24, 19, 13, 6]
            discount = rng.choices(
                discount_choices, weights=discount_weights, k=1
            )[0]
            status = rng.choices(
                ["Completed", "Returned", "Cancelled"],
                weights=[94, 4, 2],
                k=1,
            )[0]
            channel = rng.choices(channels, weights=[52, 32, 16], k=1)[0]
            orders.append(
                (
                    order_id,
                    customer_id,
                    product_id,
                    quantity,
                    discount,
                    order_date.isoformat(),
                    status,
                    channel,
                )
            )

        conn.executemany(
            """
            INSERT INTO orders (
                order_id, customer_id, product_id, quantity,
                discount_pct, order_date, status, channel
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            orders,
        )

        conn.executescript(
            """
            CREATE VIEW sales_enriched AS
            SELECT
                o.order_id,
                o.order_date,
                strftime('%Y-%m', o.order_date) AS year_month,
                c.customer_id,
                c.customer_name,
                c.segment,
                c.region,
                p.product_id,
                p.product_name,
                p.category,
                o.quantity,
                o.discount_pct,
                o.status,
                o.channel,
                ROUND(o.quantity * p.unit_price * (1 - o.discount_pct / 100.0), 2)
                    AS revenue,
                ROUND(
                    o.quantity * (
                        p.unit_price * (1 - o.discount_pct / 100.0) - p.unit_cost
                    ),
                    2
                ) AS profit
            FROM orders o
            JOIN customers c ON c.customer_id = o.customer_id
            JOIN products p ON p.product_id = o.product_id;
            """
        )
        conn.commit()
    finally:
        conn.close()


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL and not IS_ORACLE:
    raise RuntimeError(
        "DATABASE_URL is not set. Add the Neon connection string to the "
        ".env file next to main.py."
    )

DATABASE_SCHEMA = os.getenv("DATABASE_SCHEMA", "qlsp_backup").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", DATABASE_SCHEMA):
    raise RuntimeError("DATABASE_SCHEMA contains invalid characters.")

def _database_url_with_schema(database_url: str, schema: str) -> str:
    """Use Neon's direct endpoint because pooled startup options reject search_path."""
    parts = urlsplit(database_url)
    direct_netloc = parts.netloc.replace("-pooler.", ".")
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.casefold() != "options"
    ]
    query.append(("options", f"-csearch_path={schema},public"))
    return urlunsplit(
        (
            parts.scheme,
            direct_netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


if DATABASE_URL:
    DATABASE_URL = _database_url_with_schema(DATABASE_URL, DATABASE_SCHEMA)


def _build_sql_runner():
    """Pick the runner for the configured backend.

    Both pool sizes and both timeouts are read from the same environment
    variables so a load test measures the two backends under one configuration.
    """
    pool_min = int(os.getenv("DB_POOL_MIN", "4"))
    pool_max = int(os.getenv("DB_POOL_MAX", "16"))
    timeout_ms = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "20000"))

    if IS_ORACLE:
        missing = [
            name
            for name in ("ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN")
            if not os.getenv(name)
        ]
        if missing:
            raise RuntimeError(
                "DATABASE_BACKEND=oracle (the default) requires "
                + ", ".join(missing)
                + " in the .env file next to main.py. To use the Neon "
                "PostgreSQL database instead, set DATABASE_BACKEND=postgres."
            )
        return OracleRunner(
            user=os.environ["ORACLE_USER"],
            password=os.environ["ORACLE_PASSWORD"],
            dsn=os.environ["ORACLE_DSN"],
            min_connections=pool_min,
            max_connections=pool_max,
            # Oracle has no server-side statement_timeout; this is the closest
            # equivalent and is enforced client-side per round trip.
            call_timeout_ms=timeout_ms,
            current_schema=DATABASE_SCHEMA,
        )

    return PostgresRunner(
        connection_string=DATABASE_URL,
        # Reuse warm connections: a fresh TLS handshake to Neon costs more
        # than the analytical queries themselves.
        min_connections=pool_min,
        max_connections=pool_max,
        statement_timeout_ms=timeout_ms,
    )

llm = ChartAwareOllamaLlmService(
    model=os.getenv("OLLAMA_MODEL", "llama3.2"),
    host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
    temperature=0.0,
    # The system prompt plus one tool result stays well under 4k tokens; a smaller
    # context lets Ollama keep more parallel slots loaded at the same VRAM budget.
    num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "4096")),
    # Generation runs on CPU here (~18 tok/s), so an answer that never stops is
    # what produces the worst tail latency under load.
    num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "512")),
)

RESULTS_PATH.mkdir(exist_ok=True)
file_system = LocalFileSystem(working_directory=str(RESULTS_PATH))
latest_result_files: dict[tuple[str, str], str] = {}

tools = ToolRegistry()
tools.register_local_tool(
    TrackingRunSqlTool(
        sql_runner=_build_sql_runner(),
        file_system=file_system,
        latest_files=latest_result_files,
    ),
    access_groups=["read_qlsp"],
)
tools.register_local_tool(
    ResilientVisualizeDataTool(
        file_system=file_system,
        latest_files=latest_result_files,
    ),
    access_groups=["read_qlsp"],
)

agent = Agent(
    llm_service=llm,
    tool_registry=tools,
    user_resolver=MyUserResolver(),
    agent_memory=DemoAgentMemory(),
    config=AgentConfig(
        max_tool_iterations=int(os.getenv("MAX_TOOL_ITERATIONS", "3")),
        stream_responses=True,
        temperature=0.0,
    ),
    system_prompt_builder=DefaultSystemPromptBuilder(
        base_prompt=_apply_dialect("""
You are a Vietnamese data analyst for a __ENGINE__ database containing the
VNPT QLSP/SPDV operational backup. Answer in the user's language and keep the
answer concise.
__DIALECT_RULES__

Database scope:
- The active schema is qlsp_backup, containing 94 imported tables and 11,687
  rows. It is not the legacy e-commerce demo in public.
- Always qualify tables as qlsp_backup.<table_name>.
- The backup is intended for read-only analytics and Text-to-SQL.

Core tables:
- users(id, username, full_name, phone, email, unit_id, role, status,
  employee_code, job_title, position, gender, created_at, updated_at)
- units(id, code, name, annual_budget_ceiling, is_active, abbr, ma_3)
- spdv_categories(id, code, name, status, nhom_gp, line_spdv, ten_viet_tat,
  loai_spdv, trang_thai_vong_doi, trang_thai_spdv, don_vi_chu_quan_id,
  don_vi_con_id, don_vi_kinh_doanh_id, ngay_kinh_doanh, ngay_eos, ngay_eol,
  created_at, updated_at)
- spdv_tickets(id, code, spdv_id, loai, tieu_de, trang_thai, muc_uu_tien,
  khach_hang, nguoi_xu_ly, mo_ta, tac_dong, gia_tri, cam_ket_gio,
  da_dung_gio, mo_luc, dong_luc, created_at, updated_at)
- spdv_ticket_events(id, ticket_id, xay_ra_luc, nguoi_thuc_hien, hanh_dong,
  ghi_chu, thu_tu, created_at)
- plan_applications(id, code, timeline_id, unit_id, created_by, type,
  adjust_type, status, version, registration_type, created_at, updated_at)
- plan_items(id, application_id, spdv_name, spdv_id, doi_tuong_kh,
  thoi_gian_bat_dau, thoi_gian_ket_thuc, doanh_thu_du_kien, estimated_cost,
  deploy_status, completion_percent, approval_status, loai_ke_hoach,
  created_at, updated_at)
- sales_programs(id, ma, ten, spdv_id, spdv_name, kenh, tu_ngay, den_ngay,
  status, unit_id, created_by, created_at, updated_at)
- sales_program_results(id, program_id, ky, so_luong, doanh_thu, chi_phi,
  ghi_chu)
- documents(id, file_name, object_key, source_type, total_chunks, created_at)
- document_chunks(id, document_id, chunk_index, content_text, embedding,
  created_at)
- wiki_pages(id, document_id, slug, title, category, source_key, summary,
  created_by, created_at, updated_at)
- work_items(id, work_register_id, plan_item_id, task_name, expected_result,
  thoi_gian_du_kien, ghi_chu, created_at, updated_at)
- task_assignments(id, draft_code, created_by, status, total_mm,
  eoffice_document_id, created_at, loai_van_ban, trich_yeu, don_vi_ban_hanh)

Other table families include idea_*, eol_*, gtm_*, progress_*, proposal_*,
channel_*, category_*, completion_*, work_*, deep_search_*, system_*,
notifications, user_roles, audit_logs and report-related tables.

Known relationships:
- users.unit_id = units.id
- spdv_tickets.spdv_id = spdv_categories.id
- spdv_ticket_events.ticket_id = spdv_tickets.id
- plan_items.application_id = plan_applications.id
- plan_items.spdv_id = spdv_categories.id
- sales_programs.spdv_id = spdv_categories.id
- wiki_pages.document_id = documents.id

Validation counts:
- qlsp_backup.users: 2,580 rows
- qlsp_backup.units: 33 rows
- qlsp_backup.spdv_tickets: 640 rows
- qlsp_backup.spdv_ticket_events: 1,289 rows
- qlsp_backup.spdv_finance_monthly: 1,113 rows

Rules:
1. For a data question, call run_sql exactly once, then answer from that result.
2. Never call the same tool again after a successful result.
3. Use only SELECT or WITH queries. Never modify data or database objects.
4. Never invent table or column names. If the requested schema is unknown,
   query __SCHEMA_CATALOG__ and present that schema result only.
5. Do not select password_hash, reset_password_token, OTP values or other
   authentication secrets.
6. For a chart, call run_sql once with compact aggregated data, then call
   visualize_data with the exact CSV filename returned by run_sql.
7. Never count unrelated tables in one FROM clause or with CROSS JOIN. Use
   independent scalar subqueries for counts from different tables.
8. Kết quả run_sql có kèm dòng "⏱️ Thời gian chạy lệnh: X giây". Luôn hiển thị
   lại thời gian chạy này cho người dùng ở cuối câu trả lời.

Canonical database check:
SELECT
  (SELECT COUNT(*) FROM qlsp_backup.users) AS total_users,
  (SELECT COUNT(*) FROM qlsp_backup.units) AS total_units,
  (SELECT COUNT(*) FROM qlsp_backup.spdv_tickets) AS total_spdv_tickets;
""".strip())
    ),
)

register_chat_routes(app, ChatHandler(agent))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
