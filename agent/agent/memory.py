"""对话记忆：MySQL 存储，最近 N 轮上下文"""

import os
import json
import time
import logging
from typing import Any, Optional

import pymysql
from shared.redaction import redact_sensitive

from shared.runtime_repositories import ScheduledTaskRepository

logger = logging.getLogger("agent")

MAX_TOOL_LOG_DEPTH = 6
MAX_TOOL_LOG_ITEMS = 50
MAX_TOOL_LOG_STRING = 2000


def _compact_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_TOOL_LOG_DEPTH:
        if isinstance(value, dict):
            return {"__truncated__": f"dict({len(value)})"}
        if isinstance(value, (list, tuple, set)):
            return [f"truncated:{type(value).__name__}({len(value)})"]
        if isinstance(value, str) and len(value) > MAX_TOOL_LOG_STRING:
            return value[:MAX_TOOL_LOG_STRING] + f"... [truncated {len(value) - MAX_TOOL_LOG_STRING} chars]"
        return value

    if isinstance(value, dict):
        items = list(value.items())
        compacted = {
            str(key): _compact_json_value(item, depth=depth + 1)
            for key, item in items[:MAX_TOOL_LOG_ITEMS]
        }
        extra = len(items) - len(compacted)
        if extra > 0:
            compacted["__truncated_items__"] = extra
        return compacted

    if isinstance(value, (list, tuple)):
        compacted = [_compact_json_value(item, depth=depth + 1) for item in value[:MAX_TOOL_LOG_ITEMS]]
        extra = len(value) - len(compacted)
        if extra > 0:
            compacted.append({"__truncated_items__": extra})
        return compacted

    if isinstance(value, set):
        compacted = [_compact_json_value(item, depth=depth + 1) for item in list(value)[:MAX_TOOL_LOG_ITEMS]]
        extra = len(value) - len(compacted)
        if extra > 0:
            compacted.append({"__truncated_items__": extra})
        return compacted

    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
        if len(text) > MAX_TOOL_LOG_STRING:
            return text[:MAX_TOOL_LOG_STRING] + f"... [truncated {len(text) - MAX_TOOL_LOG_STRING} chars]"
        return text

    if isinstance(value, str) and len(value) > MAX_TOOL_LOG_STRING:
        return value[:MAX_TOOL_LOG_STRING] + f"... [truncated {len(value) - MAX_TOOL_LOG_STRING} chars]"

    return value


def _json_for_storage(value: Any) -> str:
    return json.dumps(
        _compact_json_value(redact_sensitive(value)),
        ensure_ascii=False,
        default=str,
    )


class Memory:
    def __init__(self):
        self._pool = None
        self._scheduled_tasks: ScheduledTaskRepository | None = None

    def init(self):
        """Initialize database access; schema changes are deployment migrations."""
        self._connect_params = {
            "host": os.getenv("AGENT_DB_HOST", "127.0.0.1"),
            "port": int(os.getenv("AGENT_DB_PORT", "3306")),
            "user": os.getenv("AGENT_DB_USER", "agent"),
            "password": os.getenv("AGENT_DB_PASS", ""),
            "database": os.getenv("AGENT_DB_NAME", "agent_db"),
            "charset": "utf8mb4",
            "autocommit": True,
        }
        self._scheduled_tasks = ScheduledTaskRepository(
            self._conn,
            cursor_factory=pymysql.cursors.DictCursor,
        )
        self._create_tables()
        logger.info("对话记忆初始化完成 (MySQL %s:%s/%s)",
                     self._connect_params["host"],
                     self._connect_params["port"],
                     self._connect_params["database"])

    def _conn(self):
        return pymysql.connect(**self._connect_params)

    def _create_tables(self):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id VARCHAR(64) PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_user (user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        conversation_id VARCHAR(64) NOT NULL,
                        role VARCHAR(16) NOT NULL,
                        content TEXT,
                        tool_calls JSON,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_conv (conversation_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tool_logs (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        message_id BIGINT,
                        conversation_id VARCHAR(64),
                        tool_name VARCHAR(64) NOT NULL,
                        params JSON,
                        result JSON,
                        success BOOLEAN,
                        duration_ms INT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_conv (conversation_id),
                        INDEX idx_tool (tool_name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        category VARCHAR(64),
                        content TEXT NOT NULL,
                        source VARCHAR(256),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FULLTEXT INDEX ft_content (content) WITH PARSER ngram
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
        finally:
            conn.close()

    def get_or_create_conversation(self, user_id: str, conversation_id: Optional[str] = None) -> str:
        """获取或创建对话"""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                if conversation_id:
                    cur.execute("SELECT id FROM conversations WHERE id=%s", (conversation_id,))
                    if cur.fetchone():
                        return conversation_id

                import uuid
                conv_id = conversation_id or str(uuid.uuid4())[:16]
                cur.execute(
                    "INSERT INTO conversations (id, user_id) VALUES (%s, %s)",
                    (conv_id, user_id),
                )
                return conv_id
        finally:
            conn.close()

    def save_message(self, conversation_id: str, role: str, content: str, tool_calls: list | None = None) -> int:
        """保存消息，返回 message_id"""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (conversation_id, role, content, tool_calls) VALUES (%s, %s, %s, %s)",
                    (conversation_id, role, content, json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None),
                )
                return cur.lastrowid
        finally:
            conn.close()

    def save_tool_log(self, conversation_id: str, message_id: int, tool_name: str,
                      params: dict, result: dict, success: bool, duration_ms: int):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tool_logs
                       (message_id, conversation_id, tool_name, params, result, success, duration_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (message_id, conversation_id, tool_name,
                     _json_for_storage(params),
                     _json_for_storage(result),
                     success, duration_ms),
                )
        finally:
            conn.close()

    def get_recent_messages(self, conversation_id: str, limit: int = 10) -> list[dict]:
        """获取最近 N 轮消息"""
        conn = self._conn()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    """SELECT role, content, tool_calls FROM messages
                       WHERE conversation_id=%s ORDER BY id DESC LIMIT %s""",
                    (conversation_id, limit * 2),  # 多取一些，user+assistant 各一条算一轮
                )
                rows = cur.fetchall()
                # 反转为时间顺序
                rows.reverse()
                result = []
                for row in rows:
                    # 不回放 tool_calls：因为对应的 tool 响应消息没有持久化，
                    # 把 tool_calls 喂给 LLM 会被判为"调用没有匹配的 tool 响应"而拒绝。
                    msg = {"role": row["role"], "content": row["content"] or ""}
                    result.append(msg)
                return result
        finally:
            conn.close()

    def search_knowledge(self, query: str, limit: int = 3) -> list[dict]:
        """全文搜索知识库"""
        conn = self._conn()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    """SELECT category, content, source,
                              MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score
                       FROM knowledge
                       WHERE MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE)
                       ORDER BY score DESC LIMIT %s""",
                    (query, query, limit),
                )
                return cur.fetchall()
        finally:
            conn.close()

    def add_knowledge(self, content: str, category: str | None = None, source: str | None = None) -> int:
        """写入知识库"""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO knowledge (category, content, source) VALUES (%s, %s, %s)",
                    (category, content, source),
                )
                return cur.lastrowid
        finally:
            conn.close()

    def get_tool_logs(
        self,
        limit: int = 20,
        tool_name: str | None = None,
        success: bool | None = None,
    ) -> list[dict]:
        """获取最近工具执行日志"""
        conn = self._conn()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                sql = """
                    SELECT id, conversation_id, tool_name, params, result, success, duration_ms, created_at
                    FROM tool_logs
                    WHERE 1=1
                """
                query_params: list = []
                if tool_name:
                    sql += " AND tool_name=%s"
                    query_params.append(tool_name)
                if success is not None:
                    sql += " AND success=%s"
                    query_params.append(success)
                sql += " ORDER BY id DESC LIMIT %s"
                query_params.append(limit)
                cur.execute(sql, tuple(query_params))
                rows = cur.fetchall()

            for row in rows:
                if row.get("params"):
                    try:
                        row["params"] = redact_sensitive(json.loads(row["params"]))
                    except Exception:
                        row["params"] = redact_sensitive(row["params"])
                if row.get("result"):
                    try:
                        row["result"] = redact_sensitive(json.loads(row["result"]))
                    except Exception:
                        row["result"] = redact_sensitive(row["result"])
                if row.get("created_at") is not None:
                    row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            return rows
        finally:
            conn.close()

    def list_scheduled_tasks(self) -> list[dict]:
        """列出定时任务定义"""
        return self._scheduled_task_repository().list_tasks()

    def list_enabled_scheduled_tasks(self) -> list[dict]:
        return self._scheduled_task_repository().list_tasks(enabled_only=True)

    def upsert_scheduled_task(self, task: dict) -> None:
        """新增或更新定时任务"""
        self._scheduled_task_repository().upsert_task(task)

    def update_scheduled_task_runtime(
        self,
        task_id: str,
        *,
        last_status: str | None,
        last_duration_ms: int | None,
        last_message: str | None,
    ) -> None:
        self._scheduled_task_repository().update_runtime(
            task_id,
            last_status=last_status,
            last_duration_ms=last_duration_ms,
            last_message=last_message,
        )

    def _scheduled_task_repository(self) -> ScheduledTaskRepository:
        if self._scheduled_tasks is None:
            raise RuntimeError("Memory.init() must run before scheduled task access")
        return self._scheduled_tasks

    def status(self) -> str:
        try:
            conn = self._conn()
            conn.ping()
            conn.close()
            return "ok"
        except Exception as e:
            return f"error | {str(e)[:80]}"
