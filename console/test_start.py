"""V2 source-start URL construction."""

from __future__ import annotations

import unittest

from console.start import console_url


class ConsoleStartTests(unittest.TestCase):
    def test_default_start_keeps_the_existing_console_route(self):
        self.assertEqual(
            console_url(8765, "synthetic-token"),
            "http://127.0.0.1:8765/?token=synthetic-token",
        )

    def test_v2_start_opens_the_v2_manager(self):
        self.assertEqual(
            console_url(8765, "synthetic-token", v2=True),
            "http://127.0.0.1:8765/v2/?token=synthetic-token",
        )
