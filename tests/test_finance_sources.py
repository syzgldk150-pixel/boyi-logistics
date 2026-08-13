from __future__ import annotations

import unittest

from shared.finance.sources import (
    FINANCE_SOURCE_SPECS,
    enabled_finance_account_ids,
    enabled_finance_platforms,
    enabled_finance_source_specs,
    finance_source_spec,
    is_finance_source_enabled,
)


class FinanceSourceRegistryTests(unittest.TestCase):
    def test_declared_source_keys_and_account_ids_are_unique(self) -> None:
        pairs = [(spec.platform, spec.account_id) for spec in FINANCE_SOURCE_SPECS]
        account_ids = [spec.account_id for spec in FINANCE_SOURCE_SPECS]

        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(len(account_ids), len(set(account_ids)))

    def test_only_production_ready_sources_are_enabled(self) -> None:
        enabled = enabled_finance_source_specs()

        self.assertTrue(enabled)
        self.assertTrue(all(spec.production_ready for spec in enabled))
        self.assertEqual(enabled_finance_platforms(), ("ronghui",))
        self.assertEqual(
            enabled_finance_account_ids(),
            (
                "price_default",
                "ronghui_daxiang_s",
                "ronghui_self_pickup_problem",
            ),
        )

    def test_yunda_is_declared_for_future_support_but_not_enabled(self) -> None:
        source = finance_source_spec("yunda", "yunda_default")

        self.assertIsNotNone(source)
        self.assertFalse(source.production_ready)
        self.assertEqual(source.status, "not_launched")
        self.assertFalse(is_finance_source_enabled("yunda", "yunda_default"))


if __name__ == "__main__":
    unittest.main()
