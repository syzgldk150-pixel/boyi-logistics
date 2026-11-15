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


def test_turn_mirrors_production_visibility_and_parameter_rule():
    messages, tools, names = build_turn("报个价，测试市 300 公斤", FIXTURE["plugins"], with_parameters=False)
    assert "plugin:quote" not in names.values()  # needs this turn's input: hidden before P1
    _, _, names = build_turn("执行扫描", FIXTURE["plugins"], with_parameters=True)
    assert "plugin:scan" in names.values()
    _, _, names = build_turn("今天扫描了多少票", FIXTURE["plugins"], with_parameters=True)
    assert not any(value.startswith("plugin:") for value in names.values())
    assert messages[0]["role"] == "system" and "博益物流" in messages[0]["content"]


def test_scoring_counts_wrong_writes_as_misexecutions():
    def reckless(messages, tools):
        names = [tool["function"]["name"] for tool in tools]
        if "run_plugin_scan" in names:
            return {"tool_calls": [{"id": "x", "function": {"name": "run_plugin_scan", "arguments": "{}"}}]}
        return {"content": "?"}
    cases = [{"id": "q", "text": "扫描插件执行失败的原因怎么处理", "expect": {"action": "none"}}]
    report = score(cases, FIXTURE["plugins"], reckless)
    assert report["misexecutions"] == 1 and report["action_hit_rate"] == 0


def test_offline_run_has_no_misexecution(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert main(["--offline", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["misexecutions"] == 0 and report["cases"] == len(FIXTURE["cases"])
    assert offline_model([{"role": "user", "content": "你好"}], []) == {"content": "请再说明一下。"}
