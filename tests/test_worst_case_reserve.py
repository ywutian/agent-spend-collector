"""Worst-case pre-spend hold for LLM forwards (gateway.worst_case_amount).

Run under pytest, or standalone: `PYTHONPATH=. python3 tests/test_worst_case_reserve.py`
"""
from spend_collector.gateway import _estimate_input_tokens, worst_case_amount

_MSG = {"messages": [{"role": "user", "content": "hi there"}]}


def test_bounded_output_holds_and_scales_with_max_tokens():
    small = worst_case_amount({"model": "gpt-4o", "max_tokens": 100, **_MSG}, {"rail": "llm_token"})
    big = worst_case_amount({"model": "gpt-4o", "max_tokens": 100_000, **_MSG}, {"rail": "llm_token"})
    assert small is not None and big is not None
    assert big >= small  # a higher output ceiling never reserves less


def test_unbounded_output_without_cap_keeps_flat():
    # no max_tokens on the request and no provider cap -> can't bound -> None (caller keeps flat)
    assert worst_case_amount({"model": "gpt-4o", **_MSG}, {"rail": "llm_token"}) is None


def test_provider_cap_bounds_an_unbounded_request():
    got = worst_case_amount({"model": "gpt-4o", **_MSG}, {"rail": "llm_token", "max_output_tokens": 4096})
    assert got is not None


def test_non_llm_rail_is_skipped():
    assert worst_case_amount({"model": "x", "max_tokens": 5}, {"rail": "api_x402"}) is None


def test_missing_model_is_skipped():
    assert worst_case_amount({"max_tokens": 5, **_MSG}, {"rail": "llm_token"}) is None


def test_estimate_input_tokens_chars_over_four():
    assert _estimate_input_tokens({"messages": [{"content": "abcdefgh"}]}) == 2  # 8 chars / 4
    assert _estimate_input_tokens({"prompt": "abcd"}) == 1


if __name__ == "__main__":  # PYTHONPATH=. python3 tests/test_worst_case_reserve.py
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all passed")
