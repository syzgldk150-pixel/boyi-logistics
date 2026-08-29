from __future__ import annotations

import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent.finance_brain import FinanceBrain
from agent.llm_client import LLMClient
from agent.llm_settings import (
    LLMSettingsError,
    PROVIDERS,
    RuntimeLLMConfig,
    _decrypt_api_key,
    _encrypt_api_key,
)
from tools.finance_review_analysis_tool import run_finance_review_analysis


class _RuntimeRepository:
    def __init__(self, *, provider: str = "deepseek", model: str = "deepseek-chat") -> None:
        self.provider = provider
        self.model = model

    def runtime_descriptor(self):
        return (
            {
                "provider": self.provider,
                "model_id": self.model,
                "source": "database",
                "config_version_id": 7,
                "credential_available": True,
            },
            True,
        )

    def active_config(self):
        return RuntimeLLMConfig(
            provider=self.provider,
            model_id=self.model,
            api_key="synthetic-unit-key",
            source="database",
            config_version_id=7,
        )


class _FinanceRepository:
    def __init__(self) -> None:
        self.ai_runs = []
        self.finished = []
        self.recovery_cutoffs = []

    def recover_interrupted_review_ai_runs(self, *, stale_before):
        self.recovery_cutoffs.append(stale_before)
        return 0

    def pending_review_evidence(self, *, limit):
        return [
            {
                "id": 11,
                "platform": "ronghui",
                "primary_fee_name": "未知类目",
                "secondary_fee_name": "",
                "direction": "expense",
                "first_seen_date": "2026-08-10",
                "last_seen_date": "2026-08-12",
                "transaction_count": 4,
                "waybill_present_count": 3,
                "income": "0.0000",
                "expense": "12.5000",
                "net_change": "-12.5000",
            }
        ][:limit]

    def get_knowledge_snapshot(self):
        return {
            "version_no": 3,
            "items": [
                {
                    "platform": "ronghui",
                    "primary_fee_name": "收派送费",
                    "secondary_fee_name": "",
                    "direction": "expense",
                    "subject_name": "派送费",
                    "fee_level": "waybill",
                    "requires_waybill": True,
                }
            ],
        }

    def start_review_ai_run(self, **kwargs):
        self.ai_runs.append(kwargs)
        return 21

    def finish_review_ai_run(self, **kwargs):
        self.finished.append(kwargs)
        return True


class LLMRuntimeTests(unittest.TestCase):
    def test_provider_endpoints_are_fixed_official_urls(self):
        self.assertEqual("https://api.deepseek.com/v1", PROVIDERS["deepseek"]["base_url"])
        self.assertEqual("https://open.bigmodel.cn/api/paas/v4", PROVIDERS["glm"]["base_url"])
        self.assertEqual({"deepseek", "glm"}, set(PROVIDERS))

    def test_aes_gcm_round_trip_and_missing_master_key_fail_explicitly(self):
        master = base64.b64encode(b"x" * 32).decode("ascii")
        with patch.dict(os.environ, {"AGENT_LLM_CONFIG_MASTER_KEY": master}, clear=False):
            ciphertext, nonce, tag, _version, hint = _encrypt_api_key("synthetic-unit-key")
            self.assertNotIn(b"synthetic-unit-key", ciphertext)
            self.assertEqual("synthetic-unit-key", _decrypt_api_key(ciphertext, nonce, tag))
            self.assertNotEqual("synthetic-unit-key", hint)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LLMSettingsError, "master key|MASTER_KEY|missing"):
                _encrypt_api_key("synthetic-unit-key")

    def test_failed_active_provider_is_not_retried_on_another_provider(self):
        async def scenario():
            with patch("agent.llm_client.environment_runtime_config", return_value=None):
                client = LLMClient()
            await client.bind_repository(_RuntimeRepository())
            client._call = AsyncMock(side_effect=RuntimeError("synthetic provider failure"))
            with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
                await client.chat([{"role": "user", "content": "test"}])
            self.assertEqual(1, client._call.await_count)
            config = client._call.await_args.args[0]
            self.assertEqual("deepseek", config.provider)

        asyncio.run(scenario())

    def test_request_rejects_an_activation_change_instead_of_using_stale_identity(self):
        async def scenario():
            with patch("agent.llm_client.environment_runtime_config", return_value=None):
                client = LLMClient()
            repository = _RuntimeRepository()
            await client.bind_repository(repository)
            repository.model = "deepseek-reasoner"
            with self.assertRaisesRegex(RuntimeError, "active model changed"):
                await client.chat(
                    [{"role": "user", "content": "test"}],
                    provider="deepseek",
                    expected_model="deepseek-chat",
                    expected_config_version_id=7,
                )

        asyncio.run(scenario())


class FinanceBrainTests(unittest.TestCase):
    def test_core_tool_process_composes_finance_brain_and_forwards_limit(self):
        llm = SimpleNamespace(bind_repository=AsyncMock())
        brain = SimpleNamespace(
            analyze_pending=AsyncMock(return_value={"status": "complete", "processed": 2}),
            notify_unreported_anomalies=AsyncMock(
                side_effect=AssertionError("analysis tool must not send external notifications")
            ),
        )
        connection_factory = Mock()

        with patch(
            "tools.finance_review_analysis_tool.LLMSettingsRepository",
            return_value="llm-settings",
        ) as settings, patch(
            "tools.finance_review_analysis_tool.FinanceRepository",
            return_value="finance-repository",
        ) as repository, patch(
            "tools.finance_review_analysis_tool.FinanceBrain",
            return_value=brain,
        ) as finance_brain:
            result = asyncio.run(
                run_finance_review_analysis(
                    {
                        "trigger_id": "event-1",
                        "source_run_id": "run-1",
                        "limit": 7,
                    },
                    connection_factory=connection_factory,
                    llm=llm,
                )
            )

        settings.assert_called_once_with(connection_factory)
        llm.bind_repository.assert_awaited_once_with("llm-settings")
        repository.assert_called_once_with(connection_factory)
        finance_brain.assert_called_once_with("finance-repository", llm)
        brain.analyze_pending.assert_awaited_once_with(limit=7)
        brain.notify_unreported_anomalies.assert_not_awaited()
        self.assertTrue(result["ok"])
        self.assertEqual("event-1", result["trigger_id"])

    def test_ai_receives_only_aggregate_evidence_and_never_approves_mapping(self):
        repository = _FinanceRepository()
        llm = SimpleNamespace(
            public_status=Mock(
                return_value={
                    "configured": True,
                    "health": "ready",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "config_version_id": 7,
                }
            ),
            chat=AsyncMock(),
        )
        llm.chat.return_value = {
            "content": json.dumps(
                {
                    "fee_level": "waybill",
                    "canonical_subject": "待确认服务费",
                    "reason": "运单号覆盖率较高，但仍需人工确认。",
                    "confidence": "0.82",
                    "uncertainties": ["名称可能在历史期间变更"],
                },
                ensure_ascii=False,
            )
        }
        brain = FinanceBrain(repository, llm)

        result = asyncio.run(brain.analyze_pending(limit=5))

        self.assertEqual(1, result["completed"])
        self.assertEqual(0, result["recovered_interrupted"])
        self.assertEqual(1, len(repository.recovery_cutoffs))
        evidence = repository.ai_runs[0]["evidence"]
        self.assertEqual(
            {
                "platform",
                "raw_fee_name",
                "direction",
                "first_seen_date",
                "last_seen_date",
                "transaction_count",
                "income_total",
                "expense_total",
                "net_change",
                "waybill_coverage",
                "confirmed_related_rules",
            },
            set(evidence),
        )
        self.assertNotIn("account_id", json.dumps(evidence, ensure_ascii=False))
        self.assertNotIn("waybill_no", json.dumps(evidence, ensure_ascii=False))
        self.assertFalse(hasattr(repository, "save_fee_mapping"))
        self.assertEqual("waybill", repository.finished[0]["suggestion"]["fee_level"])

    def test_late_ai_result_is_not_counted_as_completed_or_failed(self):
        valid = json.dumps(
            {
                "fee_level": "waybill",
                "canonical_subject": "待确认服务费",
                "reason": "仅供人工确认。",
                "confidence": "0.8",
                "uncertainties": [],
            },
            ensure_ascii=False,
        )
        for content in (valid, "not-json"):
            with self.subTest(content=content):
                repository = _FinanceRepository()
                repository.finish_review_ai_run = Mock(return_value=False)
                llm = SimpleNamespace(
                    public_status=Mock(
                        return_value={
                            "configured": True,
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "config_version_id": 7,
                        }
                    ),
                    chat=AsyncMock(return_value={"content": content}),
                )

                with patch("agent.finance_brain.publish_finance_alert") as publish:
                    result = asyncio.run(FinanceBrain(repository, llm).analyze_pending(limit=1))

                self.assertEqual(0, result["completed"])
                self.assertEqual(0, result["failed"])
                self.assertEqual(1, result["late_rejected"])
                publish.assert_not_called()

    def test_analysis_failure_is_persisted_without_sending_external_notification(self):
        repository = _FinanceRepository()
        llm = SimpleNamespace(
            public_status=Mock(
                return_value={
                    "configured": True,
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "config_version_id": 7,
                }
            ),
            chat=AsyncMock(return_value={"content": "not-json"}),
        )

        with patch("agent.finance_brain.publish_finance_alert") as publish:
            result = asyncio.run(FinanceBrain(repository, llm).analyze_pending(limit=1))

        self.assertEqual(1, result["failed"])
        self.assertEqual("FINANCE_AI_ANALYSIS_FAILED", repository.finished[0]["error_code"])
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
