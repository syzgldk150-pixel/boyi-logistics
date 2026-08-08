import io
import json
import re
import sys
import unittest
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import LocalDocFlowApp  # noqa: E402
from database import DocumentRepository  # noqa: E402
from line_haul_contacts import parse_line_haul_paste, parse_line_haul_source_text  # noqa: E402


class _Handler:
    def __init__(self, path="/", form=None):
        body = urlencode(form or {}).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        for header_name, header_value in self.sent_headers:
            if header_name.lower() == name.lower():
                return header_value
        return ""

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class _LineHaulRepo:
    def __init__(self):
        self.rows = [
            {
                "id": 1,
                "company_name": "武汉宏大物流",
                "service_area": "武汉总部",
                "address": "东西湖区革新大道富民路天黎物流",
                "contact_name": "张",
                "phone_numbers": "027-83089895 / 13035114111",
                "remark": "查询",
                "source_text": "东西湖区革新大道富民路天黎物流 027-83089895/13035114111张",
                "is_active": True,
                "sort_order": 10,
                "created_at": "2026-05-14 10:00:00",
                "updated_at": "2026-05-14 10:00:00",
            }
        ]
        self.created = []
        self.updated = []
        self.imported_rows = []

    def search_line_haul_contacts(self, filters):
        self.filters = filters
        return list(self.rows)

    def search_line_haul_contacts_page(self, filters, *, page=1, page_size=50):
        self.filters = filters
        self.page_args = {"page": page, "page_size": page_size}
        total = getattr(self, "total", len(self.rows))
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(max(int(page), 1), total_pages)
        offset = (page - 1) * page_size
        return {
            "rows": list(self.rows),
            "summary": {
                "total": total,
                "active_count": sum(1 for row in self.rows if row.get("is_active")),
                "inactive_count": sum(1 for row in self.rows if not row.get("is_active")),
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "offset": offset,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        }

    def create_line_haul_contact(self, payload):
        row = {
            **payload,
            "id": 2,
            "is_active": True,
            "sort_order": 20,
            "created_at": "2026-05-14 10:01:00",
            "updated_at": "2026-05-14 10:01:00",
        }
        self.created.append(payload)
        return row

    def update_line_haul_contact(self, contact_id, payload):
        row = {
            **payload,
            "id": contact_id,
            "is_active": True,
            "sort_order": 10,
            "created_at": "2026-05-14 10:00:00",
            "updated_at": "2026-05-14 10:02:00",
        }
        self.updated.append((contact_id, payload))
        return row

    def import_line_haul_contacts(self, rows):
        self.imported_rows.extend(rows)
        return {"inserted": len(rows), "skipped_duplicate": 1}


class LineHaulParserTests(unittest.TestCase):
    def test_parse_source_text_extracts_address_phone_contact_and_remark(self):
        row = parse_line_haul_source_text(
            "长沙县榔梨镇青园路辉邦物流园A栋4-5号门面 0731-89676795 李璐：15802623322专线负责人"
        )

        self.assertEqual("长沙县榔梨镇青园路辉邦物流园A栋4-5号门面", row["address"])
        self.assertEqual("0731-89676795 / 15802623322", row["phone_numbers"])
        self.assertEqual("李璐", row["contact_name"])
        self.assertEqual("专线负责人", row["remark"])

    def test_parse_paste_inherits_merged_company_and_skips_empty_rows(self):
        parsed = parse_line_haul_paste(
            "武汉宏大物流\t武汉总部\t东西湖区革新大道富民路天黎物流 027-83089895/13035114111张/83089622查询\n"
            "\t武汉分部\t东西湖区舵落口大市场16区15栋10号 13971580921齐/13972346594\n"
            "\t\t\n"
            "凯明物流\t济南\t济南市槐荫区传化方华 0531-82518680/15053103046"
        )

        self.assertEqual(3, len(parsed["rows"]))
        self.assertEqual(1, parsed["skipped_empty"])
        self.assertEqual("武汉宏大物流", parsed["rows"][1]["company_name"])
        self.assertEqual("张", parsed["rows"][0]["contact_name"])
        self.assertEqual("查询", parsed["rows"][0]["remark"])
        self.assertIn("13971580921", parsed["rows"][1]["phone_numbers"])


class LineHaulRepositoryHelperTests(unittest.TestCase):
    def test_build_where_filters_search_and_ignores_obsolete_status(self):
        repository = DocumentRepository.__new__(DocumentRepository)

        where_sql, params = repository._build_line_haul_contact_where({"q": "武汉", "status": "inactive"})

        self.assertIn("company_name LIKE %s", where_sql)
        self.assertNotIn("is_active", where_sql)
        self.assertEqual(["%武汉%"] * 6, params)

    def test_row_to_line_haul_contact_formats_datetimes_and_status(self):
        repository = DocumentRepository.__new__(DocumentRepository)

        row = repository._row_to_line_haul_contact(
            {
                "id": 1,
                "company_name": "凯明物流",
                "is_active": 0,
                "created_at": datetime(2026, 5, 14, 9, 30),
                "updated_at": datetime(2026, 5, 14, 10, 30),
            }
        )

        self.assertFalse(row["is_active"])
        self.assertEqual("2026-05-14 09:30:00", row["created_at"])
        self.assertEqual("2026-05-14 10:30:00", row["updated_at"])

    def test_restore_line_haul_contacts_active_state_reenables_disabled_rows(self):
        repository = DocumentRepository.__new__(DocumentRepository)
        executed = []
        cursor = SimpleNamespace(execute=lambda sql: executed.append(sql))

        repository._restore_line_haul_contacts_active_state(cursor)

        self.assertEqual(["UPDATE line_haul_contacts SET is_active = 1 WHERE is_active = 0"], executed)


class LineHaulStylesheetTests(unittest.TestCase):
    def test_reveal_final_state_removes_transform_so_fixed_modals_use_viewport(self):
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        match = re.search(r"body\.ui-ready\s+\[data-reveal\]\s*\{(?P<body>[^}]*)\}", stylesheet)

        self.assertIsNotNone(match)
        self.assertRegex(match.group("body"), r"transform\s*:\s*none\s*;")


class LineHaulRouteTests(unittest.TestCase):
    def _build_app(self, repository=None):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.repository = repository or _LineHaulRepo()
        app.settings = SimpleNamespace(app_title="Test Console")
        app.template_env = Environment(
            loader=FileSystemLoader(str(CONSOLE_DIR / "templates")),
            autoescape=select_autoescape(["html", "xml"]),
        )
        return app

    def test_render_line_haul_contacts_page(self):
        app = self._build_app()
        handler = _Handler(path="/line-haul-contacts")

        app._render_line_haul_contacts(handler, {"q": ["武汉"], "status": ["all"]})

        html = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertIn("专线分流维护", html)
        self.assertIn('action="/line-haul-contacts/create"', html)
        self.assertIn('data-line-haul-modal hidden', html)
        self.assertIn('data-line-haul-open="create"', html)
        self.assertIn('placeholder="搜索公司、站点、地址、电话"', html)
        self.assertIn("data-line-haul-filter-form", html)
        self.assertIn("data-line-haul-filter-empty hidden", html)
        self.assertNotIn('line-haul-entry-grid', html)
        self.assertIn('data-line-haul-panel="edit"', html)
        self.assertIn("data-line-haul-edit=", html)
        self.assertNotIn('data-update-url="/line-haul-contacts/1/update"', html)
        self.assertNotIn("/line-haul-contacts/1/toggle", html)
        self.assertNotIn("line-haul-cell-input", html)
        self.assertEqual({"q": "武汉"}, app.repository.filters)
        self.assertEqual({"page": 1, "page_size": 50}, app.repository.page_args)
        self.assertIn("显示 1 到 1，共 1 条", html)

    def test_render_line_haul_contacts_requires_search_before_loading_rows(self):
        repo = _LineHaulRepo()
        app = self._build_app(repo)
        handler = _Handler(path="/line-haul-contacts")

        app._render_line_haul_contacts(handler, {})

        html = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertIn("请输入关键词查询专线分流资料。", html)
        self.assertNotIn("data-line-haul-edit=", html)
        self.assertFalse(hasattr(repo, "filters"))

    def test_render_line_haul_contacts_paginates_search_results(self):
        repo = _LineHaulRepo()
        repo.total = 135
        app = self._build_app(repo)
        handler = _Handler(path="/line-haul-contacts")

        app._render_line_haul_contacts(
            handler,
            {"q": ["长沙"], "page": ["2"], "page_size": ["10"]},
        )

        html = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual({"page": 2, "page_size": 20}, repo.page_args)
        self.assertIn("显示 21 到 21，共 135 条", html)
        self.assertIn("第 2 / 7 页", html)
        self.assertIn("page=1", html)
        self.assertIn("page=3", html)

    def test_line_haul_contact_display_rows_merge_adjacent_company_names(self):
        app = self._build_app()
        rows = [
            {"id": 1, "company_name": "辉邦物流", "service_area": "长沙"},
            {"id": 2, "company_name": "辉邦物流", "service_area": "凯里"},
            {"id": 3, "company_name": "云翔物流", "service_area": "长沙"},
        ]

        display_rows = app._line_haul_contact_display_rows(rows)

        self.assertTrue(display_rows[0]["show_company"])
        self.assertEqual(2, display_rows[0]["company_rowspan"])
        self.assertFalse(display_rows[1]["show_company"])
        self.assertEqual(0, display_rows[1]["company_rowspan"])
        self.assertTrue(display_rows[2]["show_company"])
        self.assertEqual(1, display_rows[2]["company_rowspan"])

    def test_create_line_haul_contact_redirects_after_save(self):
        repo = _LineHaulRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/line-haul-contacts/create",
            form={
                "company_name": "凯明物流",
                "service_area": "济南",
                "address": "济南市槐荫区传化方华",
                "phone_numbers": "0531-82518680/15053103046",
            },
        )

        app._handle_line_haul_contact_create(handler)

        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)
        self.assertEqual("0531-82518680 / 15053103046", repo.created[0]["phone_numbers"])
        self.assertIn("/line-haul-contacts?message=", handler.header("Location"))

    def test_update_line_haul_contact_returns_saved_row(self):
        repo = _LineHaulRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/line-haul-contacts/1/update",
            form={
                "company_name": "武汉宏大物流",
                "service_area": "武汉分部",
                "address": "东西湖区舵落口大市场",
                "contact_name": "齐",
                "phone_numbers": "13971580921/13972346594",
                "remark": "查货",
            },
        )

        app._handle_line_haul_contact_update(handler, "/line-haul-contacts/1/update")

        payload = handler.json_body()
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual("13971580921 / 13972346594", payload["row"]["phone_numbers"])
        self.assertEqual(1, repo.updated[0][0])
        self.assertEqual("武汉分部", repo.updated[0][1]["service_area"])

    def test_update_line_haul_contact_redirects_for_modal_form(self):
        repo = _LineHaulRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/line-haul-contacts/1/update",
            form={
                "company_name": "武汉宏大物流",
                "service_area": "武汉分部",
                "address": "东西湖区舵落口大市场",
                "phone_numbers": "13971580921/13972346594",
                "return_to": "/line-haul-contacts?q=武汉&page_size=50&page=1",
            },
        )

        app._handle_line_haul_contact_update(handler, "/line-haul-contacts/1/update")

        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)
        self.assertIn("/line-haul-contacts?q=武汉&page_size=50&page=1&message=", handler.header("Location"))

    def test_import_paste_parses_rows_before_repository_import(self):
        repo = _LineHaulRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/line-haul-contacts/import-paste",
            form={
                "paste_text": "金湾物流\t佛山\t南海区狮山镇桃园西路富众仓库A区29-30 15107041730杨",
            },
        )

        app._handle_line_haul_contact_import_paste(handler)

        self.assertEqual(1, len(repo.imported_rows))
        self.assertEqual("金湾物流", repo.imported_rows[0]["company_name"])
        self.assertEqual("杨", repo.imported_rows[0]["contact_name"])
        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)


if __name__ == "__main__":
    unittest.main()
