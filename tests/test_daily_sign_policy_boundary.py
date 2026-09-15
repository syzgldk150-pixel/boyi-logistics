"""Host persists explicit plugin decisions without importing a due-date policy."""
import pytest
from tools.daily_sign_store import _normalize_problem_events


def event(**overrides):
    return {"source": "isolated", "external_id": "case-1", "tracking_number": "R00021074198",
            "problem_type": "隔离测试新增类型", "registered_at": "2026-09-14 18:00:00",
            "upload_complete": True, "before_cutoff": True, "postpones_sign": True, **overrides}


def test_host_preserves_new_plugin_type_and_cutoff_decision():
    assert _normalize_problem_events([event()])[0]["postpones_sign"] is True
    assert _normalize_problem_events([event()])[0]["before_cutoff"] is True
    row = _normalize_problem_events([event(problem_type="客户要求延迟派送", postpones_sign=False)])[0]
    assert row["postpones_sign"] is False


@pytest.mark.parametrize("field", ["upload_complete", "before_cutoff", "postpones_sign"])
@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_host_rejects_missing_or_ambiguous_decisions(field, value):
    with pytest.raises(ValueError, match=field):
        _normalize_problem_events([event(**{field: value})])
