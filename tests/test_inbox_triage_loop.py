"""AUTO #17 — caddy-inbox loop observability: heartbeat + loud-on-exit.

The triage daemon once went silent for ~24h looking exactly like a clean
stop (no error, no heartbeat). These tests pin the hardening:

* ``_heartbeat`` emits a logfire-visible record every cycle so an idle
  daemon is distinguishable from a dead one — and is best-effort (a
  logfire outage must never break the loop).
* the loop is loud on the way out: an unexpected unwind logs CRITICAL,
  a trapped signal logs ERROR (via ``_handle_stop_signal``), and both
  ``force_flush`` logfire in ``finally`` so a stop is never silent.

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import pytest

import scripts.inbox_triage as it


class _FakeLogfire:
    """Stand-in for the ``logfire`` module: records info() + force_flush()
    so the heartbeat and the loud-on-exit flush are observable. Set
    ``fail=True`` to prove the best-effort guards swallow a logfire fault.
    """

    def __init__(self, fail: bool = False) -> None:
        self.infos: list[tuple[str, dict]] = []
        self.flushes = 0
        self._fail = fail

    def info(self, msg: str, **kwargs) -> None:
        if self._fail:
            raise RuntimeError("logfire exploded")
        self.infos.append((msg, kwargs))

    def force_flush(self) -> None:
        self.flushes += 1


# ---------------------------------------------------------------------------
# _heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_emits_logfire_info(monkeypatch):
    fake = _FakeLogfire()
    monkeypatch.setattr(it, "logfire", fake)

    it._heartbeat("notmuch")

    assert len(fake.infos) == 1
    msg, kwargs = fake.infos[0]
    assert "heartbeat" in msg
    assert kwargs["backend"] == "notmuch"


def test_heartbeat_survives_logfire_failure(monkeypatch):
    """A logfire outage must not propagate out of the heartbeat — the loop
    keeps running even when logfire is down (best-effort)."""
    fake = _FakeLogfire(fail=True)
    monkeypatch.setattr(it, "logfire", fake)

    # Must not raise.
    it._heartbeat("notmuch")


def test_heartbeat_noop_without_logfire(monkeypatch):
    """When logfire is unavailable (import failed) the heartbeat is a no-op."""
    monkeypatch.setattr(it, "logfire", None)

    it._heartbeat(None)  # must not raise


# ---------------------------------------------------------------------------
# _handle_stop_signal — loud ERROR + flush + _SignalExit
# ---------------------------------------------------------------------------


def test_handle_stop_signal_logs_error_flushes_and_raises(monkeypatch, caplog):
    fake = _FakeLogfire()
    monkeypatch.setattr(it, "logfire", fake)

    with caplog.at_level(logging.DEBUG, logger="scripts.inbox_triage"):
        with pytest.raises(it._SignalExit) as excinfo:
            it._handle_stop_signal(signal.SIGTERM)

    assert excinfo.value.signum == signal.SIGTERM
    assert fake.flushes >= 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# _run_loop — heartbeat-per-cycle, clean signal exit, loud unexpected exit
# ---------------------------------------------------------------------------


def _patch_loop_internals(monkeypatch, sleep_impl):
    """Common loop wiring: no real signal handlers, a counting run_once,
    and a caller-supplied asyncio.sleep that drives loop termination."""
    monkeypatch.setattr(it, "_install_signal_handlers", lambda: None)

    calls = {"run_once": 0}

    async def fake_run_once(limit, backend, days_back):
        calls["run_once"] += 1

    monkeypatch.setattr(it, "run_once", fake_run_once)
    monkeypatch.setattr(it.asyncio, "sleep", sleep_impl)
    return calls


def test_loop_heartbeats_each_cycle_and_exits_cleanly_on_signal(monkeypatch, caplog):
    """Heartbeat fires once per cycle; a trapped signal unwinds the loop
    WITHOUT a CRITICAL (clean stop), and logfire is flushed on the way out."""
    fake = _FakeLogfire()
    monkeypatch.setattr(it, "logfire", fake)

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            # Simulate the trapped SIGTERM unwinding the loop (the handler
            # would already have logged ERROR + flushed).
            raise it._SignalExit(signal.SIGTERM)

    calls = _patch_loop_internals(monkeypatch, fake_sleep)

    with caplog.at_level(logging.DEBUG, logger="scripts.inbox_triage"):
        with pytest.raises(it._SignalExit):
            asyncio.run(it._run_loop(limit=5, backend="notmuch", days_back=14, interval=1))

    # One heartbeat + one run_once per cycle, for all three cycles.
    assert len(fake.infos) == 3
    assert calls["run_once"] == 3
    # Buffered records flushed on exit.
    assert fake.flushes >= 1
    # A signal-driven stop is a clean exit — never CRITICAL.
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_loop_continues_when_run_once_crashes(monkeypatch):
    """A single bad cycle must not kill the daemon — run_once raising is
    swallowed-and-continued (mirrors run_once's per-email isolation)."""
    fake = _FakeLogfire()
    monkeypatch.setattr(it, "logfire", fake)
    monkeypatch.setattr(it, "_install_signal_handlers", lambda: None)

    calls = {"run_once": 0}

    async def flaky_run_once(limit, backend, days_back):
        calls["run_once"] += 1
        raise RuntimeError("transient run_once boom")

    monkeypatch.setattr(it, "run_once", flaky_run_once)

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 4:
            raise it._SignalExit(signal.SIGINT)

    monkeypatch.setattr(it.asyncio, "sleep", fake_sleep)

    with pytest.raises(it._SignalExit):
        asyncio.run(it._run_loop(limit=5, backend="notmuch", days_back=14, interval=1))

    # The loop kept going past the crashes — four cycles, four run_once calls.
    assert calls["run_once"] == 4
    assert len(fake.infos) == 4


def test_loop_logs_critical_on_unexpected_exit(monkeypatch, caplog):
    """An unexpected unwind (not a signal, not KeyboardInterrupt) is loud:
    CRITICAL + force_flush, so a 24h silence can never look clean again."""
    fake = _FakeLogfire()
    monkeypatch.setattr(it, "logfire", fake)

    class _Boom(Exception):
        pass

    async def boom_sleep(_seconds):
        raise _Boom("unexpected loop death")

    _patch_loop_internals(monkeypatch, boom_sleep)

    with caplog.at_level(logging.DEBUG, logger="scripts.inbox_triage"):
        with pytest.raises(_Boom):
            asyncio.run(it._run_loop(limit=5, backend="notmuch", days_back=14, interval=1))

    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert crit, "unexpected loop exit must log CRITICAL"
    assert "unexpectedly" in crit[0].getMessage()
    assert fake.flushes >= 1
