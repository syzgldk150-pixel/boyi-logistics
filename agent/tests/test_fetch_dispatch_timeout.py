"""TMS dispatch requests must never wait indefinitely."""

import unittest
from unittest.mock import Mock

from agent.tms_runtime.scripts.fetch_dispatch import fetch_dispatch_records


class FetchDispatchTimeoutTests(unittest.TestCase):
    def test_post_has_finite_timeout(self):
        response = Mock()
        response.json.return_value = {"data": []}
        session = Mock()
        session.post.return_value = response

        fetch_dispatch_records(session, "73901")

        self.assertEqual(30, session.post.call_args.kwargs["timeout"])
        response.raise_for_status.assert_called_once_with()
