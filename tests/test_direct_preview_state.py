"""Consumed previews are execution history, not expired business failures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from playwright.sync_api import sync_playwright

from agent.orchestration.direct_invocation_previews import project_preview
from console.services.automation_preview_support import normalize_selection_preview_projection
from shared.automation_preview_contract import valid_preview_state


def selection_projection(*, consumed=False, expired=False):
    identity = str(uuid4())
    observed = datetime.now(timezone.utc) - timedelta(minutes=20 if expired else 1)
    row = {
        "status": "COMPLETED", "automation_id": "self_pickup_problem_upload", "generation": 1,
        "invocation_json": {"contract_hash": "contract", "project_configuration_version": 1},
        "arguments_json": {"dry_run": True}, "preview_consumed_by": str(uuid4()) if consumed else None,
        "result_json": {"status": "SUCCESS", "meta": {"observed_at": observed.isoformat()},
            "data": {"dry_run": True, "candidate_count": 1, "preview_fingerprint": "a" * 64,
                "candidates": [{"bill_code": "TEST-BILL", "arrival_count": 2, "goods_count": 2,
                    "delivery_method": "自提", "destination_site": "测试网点", "row_number": 2,
                    "source_id": "test-source", "source_name": "测试来源"}]}},
    }
    projection = project_preview(SimpleNamespace(get=lambda _identity: row), identity,
        entry=SimpleNamespace(automation_id=row["automation_id"], runtime_model="ACTION_V1", display_name="自提到货问题件"),
        contract=SimpleNamespace(automation_generation=1, contract_hash="contract", project_configuration_version=1), scan=False)
    assert row["status"] == "COMPLETED"
    return projection


@pytest.mark.parametrize("consumed,expired,state", [
    (False, False, "AVAILABLE"), (False, True, "EXPIRED"),
    (True, False, "CONSUMED"), (True, True, "CONSUMED"),
])
def test_projection_distinguishes_consumption_from_expiry(consumed, expired, state):
    projection = selection_projection(consumed=consumed, expired=expired)
    assert projection["preview_state"] == state
    assert projection["can_confirm"] is (state == "AVAILABLE")
    assert normalize_selection_preview_projection(projection,
        expected_automation_id=projection["automation_id"],
        expected_invocation_id=projection["preview_invocation_id"]) == projection
    assert "preview_consumed_by" not in projection


@pytest.mark.parametrize("state,confirm", [("CONSUMED", True), ("EXPIRED", True), ("AVAILABLE", False), ("INVALID", False), ({}, False)])
def test_invalid_or_conflicting_confirmation_state_is_rejected(state, confirm):
    assert not valid_preview_state({"preview_state": state, "can_confirm": confirm})


@pytest.mark.parametrize("service_v2", [False, True])
def test_browser_used_selection_stays_disabled_and_never_shows_expired(service_v2):
    def preview(**kwargs):
        value = selection_projection(**kwargs)
        if service_v2:
            value["automation_id"] = "87911cef-3773-4273-b644-030108c43678"
        return value

    template = (Path(__file__).resolve().parents[1] / "console/templates/automation.html").read_text()
    names = ("previewFeedback", "selectedBillCodes", "syncSelectionPreviewCount", "localizedSelectionStatus",
             "selectionMeta", "renderSelectionPreview", "formatScanPreviewTime")
    functions = []
    for name in names:
        match = re.search(rf"(?m)^( +)function {name}\(", template)
        assert match, name
        end = template.index("\n" + match[1] + "}", match.start())
        functions.append(template[match.start():end + len(match[1]) + 2])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('''<div id="panel"><b data-selection-preview-title></b>
            <p data-selection-preview-message></p><span data-selection-preview-state></span>
            <span data-selection-preview-count></span><span data-selection-preview-selected></span>
            <span data-selection-preview-expires></span><span data-selection-preview-empty></span>
            <div id="list"></div><button id="confirm">确认</button><button id="all">全选</button>
            <button id="regenerate">重新读取</button></div>''')
        page.add_script_tag(content='''
            const form = document.createElement('form');
            const taskRow = document.createElement('article');
            taskRow.setAttribute('data-automation-task-row', '');
            taskRow.appendChild(form);
            const selectionPreviewPanel = document.querySelector('#panel');
            const selectionPreviewList = document.querySelector('#list');
            const selectionPreviewConfirm = document.querySelector('#confirm');
            const selectionPreviewToggleAll = document.querySelector('#all');
            const selectionPreviewRegenerate = document.querySelector('#regenerate');
            let activeSelectionPreviewId = '', activeSelectionConfirmationRequestId = '';
            let selectionConfirmationRetryOnly = false, selectionPreviewCanConfirm = false;
            function syncRunButtonVisual() {}
        ''' + '\n'.join(functions))
        page.evaluate("v => taskRow.dataset.pluginId = v", "self_pickup_problem_upload_v2" if service_v2 else "self_pickup_problem_upload")
        page.evaluate("p => renderSelectionPreview(p)", preview())
        assert "到货 2 / 应到 2" in page.locator('#list').inner_text()
        page.locator('#list input').check()
        assert page.locator('#confirm').is_enabled()
        for expired in (False, True):
            page.evaluate("p => renderSelectionPreview(p)", preview(consumed=True, expired=expired))
            assert page.locator('[data-selection-preview-state]').inner_text() == "已使用"
            assert "正式执行记录" in page.locator('[data-selection-preview-message]').inner_text()
            assert not page.locator('#panel.is-expired').count()
            assert page.locator('#list input').is_disabled()
            assert page.locator('#all').is_disabled()
            assert page.locator('#confirm').is_disabled()
        page.evaluate("p => renderSelectionPreview(p)", preview(expired=True))
        assert page.locator('[data-selection-preview-state]').inner_text() == "已过期"
        assert page.locator('#panel.is-expired').count() == 1
        for kind in ("scan", "selection"):
            value = page.evaluate("kind => previewFeedback({preview_state:'CONSUMED',can_confirm:false}, kind)", kind)
            assert value["label"] == "已使用" and value["expired"] is False
        browser.close()
