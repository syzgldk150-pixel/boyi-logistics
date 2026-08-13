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


if __name__ == "__main__":
    unittest.main()
