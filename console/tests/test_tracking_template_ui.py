from pathlib import Path
import unittest


CONSOLE_DIR = Path(__file__).resolve().parents[1]


def read_tracking_template() -> str:
    return (CONSOLE_DIR / "templates" / "tracking.html").read_text(encoding="utf-8")


class TrackingTemplateUITests(unittest.TestCase):
    def test_tracking_result_tabs_are_preserved(self):
        template = read_tracking_template()

        self.assertIn('data-shipment-tabs', template)
        self.assertIn('data-shipment-tab="routes"', template)
        self.assertIn('data-shipment-tab="details"', template)
        self.assertIn('data-shipment-panel="routes"', template)
        self.assertIn('data-shipment-panel="details"', template)
        self.assertIn("扫描轨迹", template)
        self.assertIn("运单详情", template)

    def test_tracking_details_use_reference_card_layout(self):
        template = read_tracking_template()

        for class_name in (
            "shipment-hero-stats",
            "shipment-stat-card",
            "shipment-detail-layout",
            "shipment-section",
            "shipment-metric-list",
            "shipment-metric-list--ledger",
        ):
            self.assertIn(class_name, template)

        for title in ("运单信息", "运单号", "当前状态", "货物名称", "运费", "保价金额", "代收款"):
            self.assertIn(title, template)

        self.assertNotIn("shipment-compact-sheet", template)
        self.assertNotIn("shipment-summary-row", template)
        self.assertNotIn("shipment-field-grid", template)
        self.assertNotIn("shipment-status-board", template)
        self.assertNotIn("shipmentStatusPill", template)
        self.assertNotIn("shipment-status-pill", template)
        self.assertNotIn("shipmentBadge", template)
        self.assertNotIn("shipmentNumber", template)
        self.assertNotIn("shipment-key-facts", template)
        self.assertNotIn("shipment-collapsed-details", template)
        self.assertNotIn("shipment-info-grid", template)
        self.assertNotIn("shipment-info-panel", template)
        self.assertNotIn("shipment-fee-note-grid", template)

    def test_tracking_details_render_sender_and_receiver_as_aligned_rows(self):
        template = read_tracking_template()

        for expected in (
            "shipment-metric-row",
            "renderMetricRows",
            "label: '发货人'",
            "label: '收货人'",
            "label: '寄件地址'",
            "label: '收件地址'",
            "label: '电话'",
        ):
            self.assertIn(expected, template)

        self.assertNotIn("type: 'party'", template)

    def test_ronghui_route_columns_keep_only_scan_time(self):
        template = read_tracking_template()
        start_marker = "if (data && (data.type === 'ronghui_tms' || data.type === 'ronghui')) {"
        ronghui_columns = template.split(start_marker, 1)[1].split("    }\n    return [", 1)[0]

        self.assertEqual(ronghui_columns.count("key: 'scan_time'"), 1)
        self.assertNotIn("key: 'upload_time'", ronghui_columns)
        self.assertNotIn("上传时间", ronghui_columns)

    def test_ronghui_route_columns_hide_low_value_metadata(self):
        template = read_tracking_template()
        start_marker = "if (data && (data.type === 'ronghui_tms' || data.type === 'ronghui')) {"
        ronghui_columns = template.split(start_marker, 1)[1].split("    }\n    return [", 1)[0]

        for hidden_column in (
            "运输方式",
            "备注",
            "扫描类型",
            "扫描人",
            "来源",
            "key: 'transport_method'",
            "key: 'remark'",
            "key: 'scan_type'",
            "key: 'scan_user'",
            "key: 'source'",
        ):
            self.assertNotIn(hidden_column, ronghui_columns)


if __name__ == "__main__":
    unittest.main()
