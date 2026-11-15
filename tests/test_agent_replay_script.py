"""The replay evaluation uses the production prompt/tool rules and never executes anything."""
import json

from scripts.replay_agent_utterances import DEFAULT_CASES, build_turn, main, offline_model, score

FIXTURE = json.loads(DEFAULT_CASES.read_text(encoding="utf-8"))


def test_fixture_cases_are_unique_and_well_formed():
    ids = [case["id"] for case in FIXTURE["cases"]]
    assert len(ids) == len(set(ids)) >= 30
    keys = {f"plugin:{plugin['key']}" for plugin in FIXTURE["plugins"]}
    for case in FIXTURE["cases"]:
        for action in case["expect"]["action"].split("+"):
            assert action == "none" or action in keys or "." in action


def test_turn_mirrors_production_and_p0_rules():
    messages, tools, names = build_turn("报个价，测试市 300 公斤", FIXTURE["plugins"], baseline_p0=True)
    assert "plugin:quote" not in names.values() and "ask_user" not in names  # P0: hidden
    _, _, names = build_turn("今天扫描了多少票", FIXTURE["plugins"], baseline_p0=True)
    assert not any(value.startswith("plugin:") for value in names.values())
    _, _, names = build_turn("报个价，测试市 300 公斤", FIXTURE["plugins"])
    assert {"plugin:quote", "plugin:scan", "ask_user"} <= set(names.values())  # P1: offered, gateway decides
    assert messages[0]["role"] == "system" and "博益物流" in messages[0]["content"]


def _always(name):
    def model(messages, tools):
        return {"tool_calls": [{"id": "x", "function": {"name": name, "arguments": "{}"}}]}
    return model


def test_wrong_write_for_a_question_is_only_asked_not_run():
    cases = [{"id": "q", "text": "到货清单为什么没更新", "expect": {"action": "none"}}]
    report = score(cases, FIXTURE["plugins"], _always("run_plugin_arrival_list"))
    assert report["wrong_writes_chosen"] == 1 and report["misexecutions"] == 0
    assert report["rows"][0]["gateway"] == {"plugin:arrival_list": "CONFIRM"}


def test_wrong_write_for_a_command_counts_as_misexecution():
    cases = [{"id": "c", "text": "同步一下到货清单", "expect": {"action": "plugin:arrival_stats"}}]
    report = score(cases, FIXTURE["plugins"], _always("run_plugin_arrival_list"))
    assert report["misexecutions"] == 1 and report["action_hit_rate"] == 0


def test_ask_user_counts_as_asking_back():
    def asks(messages, tools):
        return {"tool_calls": [{"id": "x", "function": {"name": "ask_user", "arguments": '{"question":"发到哪里？"}'}}]}
    report = score([{"id": "q", "text": "报价", "expect": {"action": "none"}}], FIXTURE["plugins"], asks)
    assert report["action_hit_rate"] == 1 and report["rows"][0]["asked_back"]


def test_offline_run_has_no_misexecution(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert main(["--offline", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["misexecutions"] == 0 and report["cases"] == len(FIXTURE["cases"])
    assert offline_model([{"role": "user", "content": "你好"}], []) == {"content": "请再说明一下。"}
