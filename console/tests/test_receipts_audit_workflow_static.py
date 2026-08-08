import sys
import types
import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

_CONFIG_WAS_STUBBED = "config" not in sys.modules
sys.modules.setdefault("config", types.SimpleNamespace(Settings=object))

from database import DocumentRepository  # noqa: E402

if _CONFIG_WAS_STUBBED:
    sys.modules.pop("config", None)


class ReceiptDetailSummaryTests(unittest.TestCase):
    def test_receipt_record_exposes_audit_detail_summary_from_raw_payload(self):
        repo = DocumentRepository.__new__(DocumentRepository)
        repo._safe_int = DocumentRepository._safe_int

        row = {
            "id": 12,
            "platform": "ronghui",
            "direction": "send",
            "waybill_no": "2606000040",
            "receipt_no": "HD001",
            "photo_count": 1,
            "raw_payload_json": (
                '{"收货人":"潍坊恒泰海运有限公司","收件地址":"山东省潍坊市奎文区",'
                '"货物名称":"设备配件","包装类型":"纸箱","件数":"1",'
                '"实际重量":"106","体积":"0.35"}'
            ),
        }

        result = repo._row_to_receipt_record(row)

        self.assertEqual(
            {
                "recipient_name": "潍坊恒泰海运有限公司",
                "recipient_address": "山东省潍坊市奎文区",
                "goods_name": "设备配件",
                "package_type": "纸箱",
                "piece_count": "1",
                "actual_weight": "106",
                "volume": "0.35",
                "waybill_no": "2606000040",
            },
            result["detail_summary"],
        )

    def test_receipt_detail_summary_leaves_unknown_fields_empty(self):
        summary = DocumentRepository._receipt_detail_summary({"BILL_CODE": "R0001"}, {"waybill_no": ""})

        self.assertEqual("R0001", summary["waybill_no"])
        self.assertEqual("", summary["recipient_name"])
        self.assertEqual("", summary["recipient_address"])


class ReceiptAuditWorkflowStaticTests(unittest.TestCase):
    def test_receipts_template_has_compact_table_original_button_and_audit_workspace(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertNotIn("<th>回单号</th>", template)
        self.assertNotIn("<th>返回单号</th>", template)
        self.assertNotIn("<th>照片数</th>", template)
        self.assertIn("data-receipt-original-open", template)
        self.assertIn("data-receipt-review-modal", template)
        self.assertIn("data-receipt-review-status", template)
        self.assertIn("data-receipt-review-summary", template)
        self.assertNotIn("data-receipt-review-source", template)
        self.assertNotIn("来源：", template)
        self.assertNotIn("reviewSourceLabel", template)
        self.assertNotIn("data-receipt-zoom-in", template)
        self.assertNotIn("data-receipt-zoom-out", template)
        self.assertNotIn("data-receipt-reset-zoom", template)
        self.assertNotIn("receipt-review-image-tools", template)
        self.assertIn("receipt-review-image-overlay", template)
        self.assertIn("data-receipt-fail-reason-panel", template)
        self.assertIn("data-receipt-fail-reason-close", template)
        self.assertIn("data-receipt-same-prev", template)
        self.assertIn("data-receipt-same-next", template)
        self.assertIn("data-receipt-record-prev", template)
        self.assertIn("data-receipt-record-next", template)
        self.assertIn("data-receipt-audit-pass", template)
        self.assertIn("data-receipt-audit-fail", template)
        self.assertIn("data-receipt-fail-reason-submit", template)
        self.assertIn("data-receipt-audit-confirm", template)
        self.assertIn("data-receipt-audit-confirm-cancel", template)
        self.assertIn("data-receipt-audit-confirm-execute", template)
        self.assertIn('colspan="9"', template)

    def test_receipts_review_workspace_prioritizes_large_image_and_hidden_fail_reason(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("grid-template-rows: auto minmax(0, 1fr);", template)
        self.assertIn(".receipt-review-summary {", template)
        self.assertIn("min-height: 96px;", template)
        self.assertIn("max-height: 108px;", template)
        self.assertIn(".receipt-review-stage img {", template)
        self.assertIn("width: 100%;", template)
        self.assertIn("height: 100%;", template)
        self.assertIn("object-fit: contain;", template)
        self.assertIn('data-receipt-fail-reason-panel hidden', template)
        self.assertIn("showFailReasonPanel", template)
        self.assertIn("hideFailReasonPanel", template)
        self.assertIn("receipt-review-image-overlay", template)
        self.assertIn("data-receipt-rotate-left", template)
        self.assertIn("data-receipt-rotate-right", template)
        self.assertIn("data-receipt-rotate-reset", template)
        self.assertIn("data-receipt-review-rotation", template)
        self.assertIn(">左转</button>", template)
        self.assertIn(">右转</button>", template)
        self.assertIn(">复位</button>", template)
        self.assertNotIn('data-feather="rotate-ccw"', template)
        self.assertNotIn('data-feather="rotate-cw"', template)

    def test_receipts_review_address_gets_more_space(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("receipt-review-field--recipient_address", template)
        self.assertIn(".receipt-review-field--recipient_address {", template)
        self.assertIn("grid-column: span 2;", template)
        self.assertIn("-webkit-line-clamp: 2;", template)
        self.assertIn("white-space: normal;", template)

    def test_receipts_review_wheel_zoom_reason_popover_and_no_duplicate_waybill(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn('reviewStage?.addEventListener("wheel"', template)
        self.assertIn("event.preventDefault();", template)
        self.assertIn("passive: false", template)
        self.assertIn("setReviewZoom(reviewZoom * factor, event)", template)
        self.assertIn("let reviewRotation = 0", template)
        self.assertIn("rotateReviewImage", template)
        self.assertIn("rotate(${reviewRotation}deg)", template)
        self.assertIn("resetReviewImageTransform", template)
        self.assertIn("summaryFieldsForRecord", template)
        self.assertIn('field === "waybill_no"', template)
        self.assertIn("receipt-review-reason-popover", template)
        self.assertIn("receipt-review-reason-head", template)
        self.assertIn('reviewReasonClose?.addEventListener("click", hideFailReasonPanel)', template)
        self.assertIn('reviewReasonSubmit?.addEventListener("click", () => requestReceiptAuditConfirmation("failed"))', template)

    def test_receipts_audit_pass_executes_in_background_without_visible_original_page(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "executeReceiptAuditInBackground(receiptAuditPayloadFor(\"passed\"))",
            "requestReceiptAuditConfirmation(\"failed\")",
            "submitReceiptAuditDirect",
            "showReceiptAuditFeedback",
            "receipt-audit-pass--success",
            "已审核通过",
            "审核通过成功",
            "executeReceiptAuditInHiddenOriginalPage",
            "ensureBackgroundAuditFrame",
            'reason: result === "failed" ? (reviewReason?.value || "") : ""',
            "submitOriginalAuditForm",
            "mini.get(\"AUDIT_STATUS\")",
            "mini.get(\"saveBtn\")",
            'audit.result === "failed" ? "3" : "2"',
            "startOriginalPageAuditExecution",
            "findOriginalAuditActionButton",
            "clickOriginalAuditConfirm",
            "verifyOriginalPageAuditResult",
            "原页仍未显示",
            "AUDIT_CAPTURE_REQUIRED",
            'execution: "original_page"',
            "确认驳回回单",
            "确认后将把当前回单提交为审核不通过，并使用上方填写的原因。请确认回单照片和运单信息无误。",
            "确认驳回",
        ):
            self.assertIn(expected, template)
        self.assertNotIn('requestReceiptAuditConfirmation("passed")', template)
        self.assertNotIn("executeReceiptAuditInOriginalPage", template)
        self.assertNotIn("确认后会打开原页模式", template)
        self.assertNotIn("接口未抓取时会在后台原页执行", template)
        self.assertNotIn("confirmResult.ok || Date.now() - actionClickedAt > 4500", template)
        self.assertNotIn("确认执行审核通过", template)

    def test_receipts_original_page_verification_reads_easyui_datagrid_rows(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "easyuiDatagridShowsAuditResult",
            'jquery("#dg")',
            'datagrid("getRows")',
            "datagridRows.some((row)",
            "easyuiDatagridShowsAuditResult(doc, identifiers, targetStatus)",
        ):
            self.assertIn(expected, template)

    def test_receipts_original_page_verification_resyncs_source_before_local_writeback(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "verifyReceiptAuditViaSync",
            'fetch("/receipts/sync"',
            'syncParams.set("platform", record.platform || "")',
            'syncParams.set("q", payload.query || "")',
            "freshRecord.audit_status === targetStatus",
            "await complete(`${meta || \"原系统审核页面\"} · 已通过真实接口复核${label}`)",
        ):
            self.assertIn(expected, template)

    def test_app_has_receipt_audit_route_and_agent_target_name(self):
        app_source = (CONSOLE_DIR / "app.py").read_text(encoding="utf-8")

        self.assertIn('path.startswith("/receipts/") and path.endswith("/audit")', app_source)
        self.assertIn("def _handle_receipt_audit", app_source)
        self.assertIn('"/tms/receipts_audit"', app_source)
        self.assertIn("update_receipt_audit_status", app_source)
        self.assertIn("audit_log_request", app_source)
        self.assertNotIn("request_summary=params", app_source)


if __name__ == "__main__":
    unittest.main()
