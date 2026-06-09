"""Increment E — unit tests for the ``ToolCallAccumulator`` mixin
(``orchestrator/providers/voice_base.py``).

The mixin replaces three near-identical pairs of dicts
(``_pending_calls`` + ``_pending_args``) that previously lived on
each voice provider. Behavior pinned here:

1. ``register_call`` records id→name and resets the args buffer.
2. ``accumulate_args`` is a no-op when the call_id wasn't registered
   (legacy ``if call_id in self._pending_args`` guard).
3. ``pop_name`` / ``pop_args`` are consuming reads — entries vanish.
4. ``peek_name`` / ``peek_args`` are non-consuming.
5. ``clear_pending_calls`` drops everything (called from reconnect).
6. Empty / missing call_id or name silently no-op (defensive against
   malformed provider frames).
"""

from __future__ import annotations

from orchestrator.providers.voice_base import ToolCallAccumulator


class _Bag(ToolCallAccumulator):
    """Bare instantiation of the mixin for unit testing."""

    def __init__(self) -> None:
        ToolCallAccumulator.__init__(self)


# ---------- register_call ---------------------------------------------------


def test_register_call_records_id_to_name():
    bag = _Bag()
    bag.register_call("call-1", "search_history")
    assert bag.peek_name("call-1") == "search_history"


def test_register_call_resets_args_buffer():
    bag = _Bag()
    bag.register_call("call-1", "search_history")
    bag.accumulate_args("call-1", '{"query": "abc"')
    # Re-registering must zero the buffer (defensive: call_ids should
    # be unique, but if a provider reuses one we don't want bleed).
    bag.register_call("call-1", "search_memory")
    assert bag.peek_args("call-1") == ""
    assert bag.peek_name("call-1") == "search_memory"


def test_register_call_empty_id_or_name_is_noop():
    bag = _Bag()
    bag.register_call("", "search_history")
    bag.register_call("call-1", "")
    assert bag.peek_name("") == ""
    assert bag.peek_name("call-1") == ""


# ---------- accumulate_args -------------------------------------------------


def test_accumulate_args_appends_in_order():
    bag = _Bag()
    bag.register_call("call-1", "search_history")
    bag.accumulate_args("call-1", '{"query"')
    bag.accumulate_args("call-1", ': "abc"')
    bag.accumulate_args("call-1", "}")
    assert bag.peek_args("call-1") == '{"query": "abc"}'


def test_accumulate_args_for_unregistered_id_is_noop():
    """Legacy ``if call_id in self._pending_args:`` guard. Out-of-order
    frames (delta arriving before output_item.added) silently drop.
    """
    bag = _Bag()
    bag.accumulate_args("ghost-call", '{"q":')
    assert bag.peek_args("ghost-call") == ""
    # And register_call after the fact must NOT pick up the stale delta.
    bag.register_call("ghost-call", "x")
    assert bag.peek_args("ghost-call") == ""


# ---------- pop / peek invariants -------------------------------------------


def test_pop_name_consumes():
    bag = _Bag()
    bag.register_call("call-1", "search_history")
    assert bag.pop_name("call-1") == "search_history"
    assert bag.pop_name("call-1") == ""  # gone
    assert bag.peek_name("call-1") == ""


def test_pop_args_consumes():
    bag = _Bag()
    bag.register_call("call-1", "search_history")
    bag.accumulate_args("call-1", '{"q": "x"}')
    assert bag.pop_args("call-1") == '{"q": "x"}'
    assert bag.pop_args("call-1") == ""


def test_peek_does_not_consume():
    bag = _Bag()
    bag.register_call("call-1", "search_history")
    bag.accumulate_args("call-1", '{"q": "x"}')
    assert bag.peek_name("call-1") == "search_history"
    assert bag.peek_name("call-1") == "search_history"  # still there
    assert bag.peek_args("call-1") == '{"q": "x"}'
    assert bag.peek_args("call-1") == '{"q": "x"}'


def test_pop_missing_returns_empty_string():
    bag = _Bag()
    assert bag.pop_name("ghost") == ""
    assert bag.pop_args("ghost") == ""


# ---------- clear_pending_calls --------------------------------------------


def test_clear_pending_calls_drops_everything():
    bag = _Bag()
    bag.register_call("a", "tool_a")
    bag.register_call("b", "tool_b")
    bag.accumulate_args("a", "{}")
    bag.clear_pending_calls()
    assert bag.peek_name("a") == ""
    assert bag.peek_name("b") == ""
    assert bag.peek_args("a") == ""
    assert bag.peek_args("b") == ""


# ---------- multiple in-flight calls ----------------------------------------


def test_multiple_calls_do_not_collide():
    bag = _Bag()
    bag.register_call("a", "tool_a")
    bag.register_call("b", "tool_b")
    bag.accumulate_args("a", '{"x": 1}')
    bag.accumulate_args("b", '{"y": 2}')
    assert bag.peek_args("a") == '{"x": 1}'
    assert bag.peek_args("b") == '{"y": 2}'
    assert bag.pop_name("a") == "tool_a"
    # popping "a" leaves "b" untouched
    assert bag.peek_name("b") == "tool_b"
