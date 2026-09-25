"""Read only the authorized Wiki spaces through the installed Feishu CLI.

Contract checked against larksuite/cli v1.0.3: bot Wiki list/get_node,
docs +fetch and drive +download. That version's docs +search is user-only; this adapter
explicitly searches bounded in-scope bodies instead. It never changes login.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlparse


AUTHORIZED_SPACES = ("融辉红头文件", "韵达红头文件")
_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_PERMISSION_CODES = {99991672, 99991679, 99991663, 99991668, 131006, 131008}


class KnowledgeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fail(code, message):
    raise KnowledgeError(code, message)


def _identity(value):
    if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
        _fail("KNOWLEDGE_SCHEMA_INVALID", "知识来源标识无法核实。")
    return value


class KnowledgeCli:
    """Fixed read verbs, bounded bytes/time, no shell and no raw CLI errors."""
    _COMMANDS = {("wiki", "spaces", "list"), ("wiki", "spaces", "get_node"),
                 ("wiki", "nodes", "list"), ("docs", "+fetch")}

    def __call__(self, command, parameters, *, deadline):
        command = tuple(command)
        if command not in self._COMMANDS:
            _fail("KNOWLEDGE_COMMAND_DENIED", "不支持该知识操作。")
        if os.name != "posix":
            _fail("KNOWLEDGE_RUNTIME_UNSUPPORTED", "知识读取需要受控的 Linux 运行环境。")
        args = ["lark-cli", *command]
        if command == ("docs", "+fetch"):
            if set(parameters) != {"doc"}:
                _fail("KNOWLEDGE_ARGUMENT_INVALID", "正文查询参数无效。")
            args.extend(["--doc", _identity(parameters["doc"])])
        else:
            args.extend(["--params", json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))])
        args.extend(["--as", "bot", "--format", "json"])
        return self._run(args, deadline=deadline)

    def read_pdf(self, object_id, *, deadline):
        """v1.0.3 download uses relative output and has no --format flag."""
        object_id = _identity(object_id)
        if os.name != "posix":
            _fail("KNOWLEDGE_RUNTIME_UNSUPPORTED", "知识读取需要受控的 Linux 运行环境。")
        temporary_root = Path(__file__).resolve().parents[1] / ".task_tmp" / "knowledge"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pdf-", dir=temporary_root) as directory:
            pdf = Path(directory) / "source.pdf"
            self._run(["lark-cli", "drive", "+download", "--file-token", object_id,
                       "--output", "./source.pdf", "--as", "bot"], deadline=deadline,
                      cwd=directory, download_path=pdf)
            if not pdf.is_file() or pdf.is_symlink() or not 0 < pdf.stat().st_size <= 16_000_000:
                _fail("KNOWLEDGE_PDF_INVALID", "PDF 下载为空或超过读取上限。")
            return self._run([sys.executable, "-I", str(Path(__file__).with_name("feishu_knowledge_pdf.py")),
                              str(pdf)], deadline=deadline, cwd=directory,
                             env={"PATH": os.defpath, "LANG": "C.UTF-8"})

    @staticmethod
    def _run(args, *, deadline, cwd=None, download_path=None, env=None):
        remaining = min(15, deadline-time.monotonic())
        if remaining <= 0:
            _fail("KNOWLEDGE_TIMEOUT", "知识读取超时，本次未返回完整依据。")
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True, cwd=cwd, env=env)
        except FileNotFoundError:
            _fail("KNOWLEDGE_CLI_UNAVAILABLE", "飞书 CLI 尚未安装。")
        output = bytearray()
        errors = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, data=output)
                selector.register(process.stderr, selectors.EVENT_READ, data=errors)
                expires = time.monotonic() + remaining
                while selector.get_map():
                    if download_path is not None and download_path.exists() and download_path.stat().st_size > 16_000_000:
                        _fail("KNOWLEDGE_OUTPUT_LIMIT", "知识文件超过本次读取上限。")
                    wait = expires-time.monotonic()
                    if wait <= 0:
                        _fail("KNOWLEDGE_TIMEOUT", "知识读取超时，本次未返回完整依据。")
                    for key, _ in selector.select(min(wait, .1)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(chunk)
                        if len(output) + len(errors) > 2_000_000:
                            _fail("KNOWLEDGE_OUTPUT_LIMIT", "知识正文超过本次读取上限，未作为完整依据。")
                process.wait(timeout=max(.01, expires-time.monotonic()))
            if process.returncode and any(str(code).encode() in errors for code in _PERMISSION_CODES):
                _fail("KNOWLEDGE_PERMISSION_DENIED", "机器人没有读取授权知识库的权限，请开通只读权限并加入这两个空间。")
            try:
                payload = json.loads(output or errors)
            except (UnicodeDecodeError, ValueError):
                _fail("KNOWLEDGE_CLI_FAILED", "飞书 CLI 未返回可核验的数据。")
            if not isinstance(payload, dict):
                _fail("KNOWLEDGE_SCHEMA_INVALID", "飞书知识响应格式不支持。")
            code = payload.get("code")
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code", code)
            if isinstance(code, str) and code.startswith("KNOWLEDGE_PDF_"):
                _fail(code, "PDF 文字层不能完整提取，未作为知识依据。")
            if code in _PERMISSION_CODES:
                _fail("KNOWLEDGE_PERMISSION_DENIED", "机器人没有读取授权知识库的权限，请开通只读权限并加入这两个空间。")
            if process.returncode or code not in (None, 0) or error:
                _fail("KNOWLEDGE_CLI_FAILED", "飞书知识读取失败，没有使用旧缓存代替。")
            return payload
        except subprocess.TimeoutExpired:
            _fail("KNOWLEDGE_TIMEOUT", "知识读取超时，本次未返回完整依据。")
        finally:
            # Also kill descendants that inherited the pipe after the CLI exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


def _data(payload):
    if not isinstance(payload, dict):
        _fail("KNOWLEDGE_SCHEMA_INVALID", "知识响应格式不支持。")
    value = payload.get("data", payload)
    if not isinstance(value, dict):
        _fail("KNOWLEDGE_SCHEMA_INVALID", "知识响应没有有效正文数据。")
    return value


class FeishuKnowledgeSource:
    """No persistent body cache: every answer rechecks current scope and revision."""
    def __init__(self, *, cli=None, space_names=AUTHORIZED_SPACES, pdf_reader=None):
        self._cli = cli or KnowledgeCli()
        self._pdf_reader = pdf_reader or getattr(self._cli, "read_pdf", None)
        self._space_names = tuple(space_names)
        if not self._space_names or len(set(self._space_names)) != len(self._space_names):
            raise ValueError("unique authorized space names required")
        self._slots = BoundedSemaphore(2)

    def _pages(self, command, parameters, *, deadline, limit=300):
        rows, cursor, seen = [], "", set()
        for _ in range(10):
            params = {**parameters, "page_size": 50}
            if cursor:
                params["page_token"] = cursor
            data = _data(self._cli(command, params, deadline=deadline))
            items, more = data.get("items"), data.get("has_more")
            if not isinstance(items, list) or type(more) is not bool:
                _fail("KNOWLEDGE_PAGINATION_UNVERIFIED", "知识目录分页未完整核实。")
            rows.extend(items)
            if len(rows) > limit:
                _fail("KNOWLEDGE_SEARCH_LIMIT", "授权知识目录超过本次检索上限，请缩小指定范围。")
            if not more:
                return rows
            cursor = data.get("page_token")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                _fail("KNOWLEDGE_PAGINATION_UNVERIFIED", "知识目录分页游标异常。")
            seen.add(cursor)
        _fail("KNOWLEDGE_SEARCH_LIMIT", "知识目录分页超过本次预算。")

    def _spaces(self, deadline):
        spaces = self._pages(("wiki", "spaces", "list"), {}, deadline=deadline)
        result = []
        for name in self._space_names:
            candidates = [item for item in spaces if isinstance(item, dict) and item.get("name") == name]
            if len(candidates) != 1:
                _fail("KNOWLEDGE_SCOPE_UNAVAILABLE", f"无法唯一读取授权知识库“{name}”；请检查机器人空间成员权限。")
            result.append((name, _identity(candidates[0].get("space_id"))))
        return result

    def _nodes(self, space_id, deadline):
        queue, nodes, seen = [None], [], set()
        while queue:
            parent = queue.pop(0)
            params = {"space_id": space_id}
            if parent is not None:
                params["parent_node_token"] = parent
            for node in self._pages(("wiki", "nodes", "list"), params, deadline=deadline):
                if not isinstance(node, dict):
                    _fail("KNOWLEDGE_SCHEMA_INVALID", "知识节点格式不支持。")
                identity = _identity(node.get("node_token"))
                if identity in seen or node.get("space_id", space_id) != space_id:
                    _fail("KNOWLEDGE_SCOPE_MISMATCH", "知识节点范围或身份不一致。")
                seen.add(identity)
                nodes.append(node)
                if len(nodes) > 100:
                    _fail("KNOWLEDGE_SEARCH_LIMIT", "授权知识内容超过本次检索上限。")
                if node.get("has_child") is True:
                    queue.append(identity)
                elif node.get("has_child") is not False:
                    _fail("KNOWLEDGE_SCHEMA_INVALID", "知识节点子目录状态未知。")
        return nodes

    def _node(self, node_id, space_id, deadline):
        node = _data(self._cli(("wiki", "spaces", "get_node"), {"token": node_id}, deadline=deadline)).get("node")
        if not isinstance(node, dict) or node.get("node_token") != node_id or node.get("space_id") != space_id:
            _fail("KNOWLEDGE_SCOPE_MISMATCH", "知识正文已移出授权范围或身份不一致。")
        if node.get("node_type") == "shortcut" and node.get("origin_space_id") != space_id:
            _fail("KNOWLEDGE_SCOPE_MISMATCH", "该快捷方式指向授权范围之外，未读取目标正文。")
        if node.get("obj_type") not in {"docx", "file"}:
            _fail("KNOWLEDGE_FORMAT_UNSUPPORTED", "该知识内容不是受支持的文档正文，未读取表格或附件。")
        if node.get("obj_type") == "file" and not str(node.get("title", "")).lower().endswith(".pdf"):
            _fail("KNOWLEDGE_FORMAT_UNSUPPORTED", "当前只支持在线文档和 PDF 文件。")
        _identity(node.get("obj_token"))
        return node

    def _body(self, node_id, space_id, deadline, query):
        before = self._node(node_id, space_id, deadline)
        is_pdf = before["obj_type"] == "file"
        pdf_metadata = {}
        if is_pdf:
            if self._pdf_reader is None:
                _fail("KNOWLEDGE_PDF_UNAVAILABLE", "PDF 读取尚未配置。")
            from tools.feishu_knowledge_pdf import select_pages
            body = _data(self._pdf_reader(before["obj_token"], deadline=deadline))
            try:
                markdown, pdf_metadata = select_pages(body, query)
            except ValueError as exc:
                _fail("KNOWLEDGE_PDF_EXCERPT_LIMIT", str(exc))
        else:
            body = _data(self._cli(("docs", "+fetch"), {"doc": before["obj_token"]}, deadline=deadline))
            markdown = body.get("markdown")
            if not isinstance(markdown, str) or not markdown.strip() or type(body.get("has_more")) is not bool:
                _fail("KNOWLEDGE_BODY_UNVERIFIED", "没有取得完整可核验的知识正文。")
        if not is_pdf and body["has_more"]:
            # v1.0.3 advertises offsets without a verifiable continuation cursor
            # in the checked response contract. Never guess a character offset.
            _fail("KNOWLEDGE_BODY_INCOMPLETE", "正文仍有后续内容，当前 CLI 合同未核实续页位置，不能作为完整依据。")
        if len(markdown) > 12000:
            _fail("KNOWLEDGE_BODY_INCOMPLETE", "正文超过本次上下文上限，不能截断后声称完整读取。")
        after = self._node(node_id, space_id, deadline)
        for key in ("obj_token", "obj_type", "obj_edit_time", "title"):
            if before.get(key) != after.get(key):
                _fail("KNOWLEDGE_CHANGED_DURING_READ", "文档在读取期间更新，请重新查询。")
        title = after.get("title")
        if not isinstance(title, str) or not title.strip():
            _fail("KNOWLEDGE_SCHEMA_INVALID", "知识文档缺少可核验标题。")
        url = body.get("url") or body.get("doc_url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed and (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".feishu.cn")):
            url = None
        return {"source_id": node_id, "document_id": after["obj_token"], "title": title, "url": url,
                "updated_at": after.get("obj_edit_time"), "read_at": datetime.now(timezone.utc).isoformat(),
                "revision": None, "effective_date": None, "body": markdown,
                "body_complete": not is_pdf, "embedded_content_read": False, **pdf_metadata,
                "authority": "untrusted_business_document"}

    def search(self, query: str, limit: int) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(limit) is not int or not 1 <= limit <= 20:
            return {"ok": False, "status": "invalid", "code": "KNOWLEDGE_ARGUMENT_INVALID", "message": "请提供明确的知识查询词。"}
        if not self._slots.acquire(blocking=False):
            return {"ok": False, "status": "busy", "code": "KNOWLEDGE_BUSY", "message": "知识读取暂忙，请稍后重试。"}
        deadline = time.monotonic() + 25
        try:
            candidates = []
            for space_name, space_id in self._spaces(deadline):
                for node in self._nodes(space_id, deadline):
                    candidates.append((space_name, space_id, node["node_token"]))
                    if len(candidates) > 20:
                        _fail("KNOWLEDGE_SEARCH_LIMIT", "本次未能完整检索授权范围，请指定文档或缩小查询。")
            def read(candidate):
                space_name, space_id, node_id = candidate
                item = self._body(node_id, space_id, deadline, query)
                if item.get("format") == "pdf" and not item["body"] and query.casefold() in item["title"].casefold():
                    _fail("KNOWLEDGE_PDF_TOPIC_REQUIRED", "PDF 较长，请指定需要查阅的业务主题。")
                if item["body"] and query.casefold() in (item["title"] + "\n" + item["body"]).casefold():
                    item["space"] = space_name
                    return item
                return None
            # The two independent PDF reads share the same deadline. Bounded
            # parallelism leaves time for the model's answer or a refined query
            # within the existing 30-second conversation budget; no body cache.
            with ThreadPoolExecutor(max_workers=2) as pool:
                evidence = [item for item in pool.map(read, candidates) if item is not None]
            return {"ok": True, "status": "found" if evidence else "no_match", "source": "feishu_wiki_cli",
                    "identity": "bot", "spaces": list(self._space_names), "search_mode": "bounded_literal_body_search",
                    "scope_complete": True, "matched": len(evidence), "items": evidence[:limit],
                    "results_truncated": len(evidence) > limit, "cache": "none_revalidate_each_read",
                    "message": "" if evidence else "本次在授权范围内未找到匹配依据；不表示不存在相关规则。"}
        except KnowledgeError as exc:
            return {"ok": False, "status": "unavailable", "code": exc.code, "message": str(exc), "items": []}
        finally:
            self._slots.release()
