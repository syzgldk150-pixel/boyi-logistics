"""Static UI guardrails for the unified Console control plane."""

from __future__ import annotations

import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class ControlPlaneUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.list_template = (CONSOLE_DIR / "templates" / "work_items.html").read_text(
            encoding="utf-8"
        )
        cls.detail_template = (
            CONSOLE_DIR / "templates" / "work_item_detail.html"
        ).read_text(encoding="utf-8")
        cls.script = (CONSOLE_DIR / "static" / "control_plane.js").read_text(
            encoding="utf-8"
        )
        cls.styles = (CONSOLE_DIR / "static" / "control_plane.css").read_text(
            encoding="utf-8"
        )

    def test_pages_reuse_base_shell_and_local_assets(self):
        for template in (self.list_template, self.detail_template):
            self.assertIn('{% extends "base.html" %}', template)
            self.assertIn("/static/control_plane.css", template)
            self.assertIn("/static/control_plane.js", template)
            self.assertNotIn("https://", template)
            self.assertNotIn("http://", template)

    def test_list_has_visible_filters_loading_feedback_and_mobile_labels(self):
        self.assertIn('data-cp-filter-form', self.list_template)
        self.assertIn('aria-live="polite"', self.list_template)
        self.assertIn('aria-busy="true"', self.list_template)
        self.assertIn('dataset.label = "状态"', self.script)
        self.assertIn('dataset.label = "操作"', self.script)

    def test_detail_has_plan_evidence_timeline_and_explicit_dialogs(self):
        for marker in (
            "data-cp-plan-content",
            "data-cp-evidence-list",
            "data-cp-timeline-list",
            "data-cp-approval-dialog",
            "data-cp-run-action-dialog",
            "data-cp-clarification-account-id",
            "data-cp-clarification-arguments",
            "data-cp-assign-dialog",
        ):
            self.assertIn(marker, self.detail_template)

    def test_plan_renders_governed_tool_account_risk_and_impact(self):
        for marker in (
            "step.tool_version",
            "step.operation_type",
            "step.account_id",
            "step.risk_level",
            "step.requires_approval",
            "plan.impact",
            '"影响范围"',
        ):
            self.assertIn(marker, self.script)

    def test_assignment_is_available_without_an_unimplemented_owner_catalog(self):
        self.assertIn('data-cp-assign-owner type="text"', self.detail_template)
        self.assertIn('if (actionAllowed("assign"))', self.script)
        self.assertNotIn("assignableOwners", self.script)

    def test_dom_data_is_written_with_text_content_not_html_injection(self):
        self.assertIn("textContent", self.script)
        self.assertNotIn("innerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)

    def test_clarification_requires_explicit_json_not_natural_language_guessing(self):
        self.assertIn("JSON.parse(updatesText)", self.script)
        self.assertIn("structured.account_id = accountId", self.script)
        self.assertIn("structured.argument_updates = updates", self.script)
        self.assertIn("普通说明不会被猜成业务参数", self.detail_template)

    def test_polling_pauses_when_hidden_and_respects_agent_interval(self):
        self.assertIn('document.addEventListener("visibilitychange"', self.script)
        self.assertIn("document.hidden", self.script)
        self.assertIn("next_poll_after_ms", self.script)
        self.assertIn("TERMINAL_RUN_STATES", self.script)

    def test_styles_cover_keyboard_mobile_and_reduced_motion(self):
        self.assertIn(":focus-visible", self.styles)
        self.assertIn("@media (max-width: 360px)", self.styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.styles)
        self.assertNotIn("transition: all", self.styles)
        self.assertNotIn("linear-gradient", self.styles)


if __name__ == "__main__":
    unittest.main()
