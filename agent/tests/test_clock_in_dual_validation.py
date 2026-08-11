"""Clock-in writes must require an explicit, complete site identity."""

import unittest
from unittest.mock import patch

from agent.tms_runtime.scripts import clock_in_dual


class ClockInDualValidationTests(unittest.TestCase):
    def test_missing_site_identity_fails_before_submit(self):
        with patch.object(clock_in_dual, "submit_dual_clockin") as submit:
            with self.assertRaisesRegex(ValueError, "explicit parameters"):
                clock_in_dual.run_once({"mode": "api"})
        submit.assert_not_called()

    def test_partial_site_identity_fails_before_submit(self):
        params = {
            "mode": "api",
            "sitecode": "7390004",
            "site_name": "邵阳大祥站",
            "site_fb_name": "邵阳操作场",
        }
        with patch.object(clock_in_dual, "submit_dual_clockin") as submit:
            with self.assertRaisesRegex(ValueError, "sitefbcode"):
                clock_in_dual.run_once(params)
        submit.assert_not_called()

    def test_browser_mode_never_falls_back_to_api_write(self):
        with patch.object(clock_in_dual, "submit_dual_clockin") as submit:
            with self.assertRaisesRegex(NotImplementedError, "not implemented"):
                clock_in_dual.run_once({"mode": "browser"})
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
