"""Worst-case pre-spend hold for LLM forwards."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spend_collector.gateway import _estimate_input_tokens, worst_case_amount


_MSG = {"messages": [{"role": "user", "content": "hi there"}]}


class WorstCaseReserveTest(unittest.TestCase):
    def test_bounded_output_holds_and_scales_with_max_tokens(self) -> None:
        small = worst_case_amount({"model": "gpt-4o", "max_tokens": 100, **_MSG}, {"rail": "llm_token"})
        big = worst_case_amount({"model": "gpt-4o", "max_tokens": 100_000, **_MSG}, {"rail": "llm_token"})
        self.assertIsNotNone(small)
        self.assertIsNotNone(big)
        self.assertGreaterEqual(big, small)

    def test_unbounded_output_without_cap_keeps_flat(self) -> None:
        self.assertIsNone(worst_case_amount({"model": "gpt-4o", **_MSG}, {"rail": "llm_token"}))

    def test_provider_cap_bounds_an_unbounded_request(self) -> None:
        got = worst_case_amount({"model": "gpt-4o", **_MSG}, {"rail": "llm_token", "max_output_tokens": 4096})
        self.assertIsNotNone(got)

    def test_non_llm_rail_is_skipped(self) -> None:
        self.assertIsNone(worst_case_amount({"model": "x", "max_tokens": 5}, {"rail": "api_x402"}))

    def test_missing_model_is_skipped(self) -> None:
        self.assertIsNone(worst_case_amount({"max_tokens": 5, **_MSG}, {"rail": "llm_token"}))

    def test_estimate_input_tokens_chars_over_four(self) -> None:
        self.assertEqual(_estimate_input_tokens({"messages": [{"content": "abcdefgh"}]}), 2)
        self.assertEqual(_estimate_input_tokens({"prompt": "abcd"}), 1)


if __name__ == "__main__":
    unittest.main()
