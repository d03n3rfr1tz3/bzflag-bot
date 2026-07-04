"""Tests für die MsgPlayerUpdate-Kadenz (N1a, FABLE-PLAN.md Teil 1b).

Der historische Bug: `_next_server_update = 0.0` lag um die komplette
System-Uptime hinter `time.monotonic()`; der `+=`-Catch-up trug das Defizit
nur mit 1s/s ab → der Bot sendete dauerhaft mit Tick-Rate (~60 Hz) statt
30 Hz. Die Tests simulieren die 60-Hz-Schleife mit synthetischen Zeiten und
zählen die tatsächlichen Sends.
"""
from unittest.mock import MagicMock

TICK = 1.0 / 60.0


def _run_ticks(bot, t0, seconds):
    """Simuliert die 60-Hz-Schleife und zählt Sends über `seconds` Sekunden."""
    bot._send_update = MagicMock()
    n_ticks = int(round(seconds / TICK))
    for i in range(n_ticks):
        bot._maybe_send_server_update(t0 + i * TICK)
    return bot._send_update.call_count


class TestUpdateCadence:
    def test_30hz_bei_grossem_monotonic_offset(self, bot):
        """Verankerte Kadenz: ~30 Sends/s, auch wenn monotonic() riesig ist."""
        t0 = 1_000_000.0   # simulierte hohe System-Uptime
        bot._next_server_update = t0   # entspricht dem Anker vor der Schleife
        sends = _run_ticks(bot, t0, seconds=2.0)
        assert 59 <= sends <= 61, f"erwartet ~60 Sends in 2s (30 Hz), waren {sends}"

    def test_selbstheilung_bei_altem_defizit(self, bot):
        """Regression N1a: selbst mit dem Alt-Zustand (_next_server_update=0.0)
        darf ab dem ersten Send kein Tick-Raten-Dauersenden mehr entstehen —
        die Stall-Klemme setzt die Kadenz neu auf."""
        bot._next_server_update = 0.0   # Alt-Init aus __init__
        t0 = 1_000_000.0
        sends = _run_ticks(bot, t0, seconds=2.0)
        assert 59 <= sends <= 61, f"Defizit-Katchup darf nicht senden pro Tick: {sends}"

    def test_kein_burst_nach_stall(self, bot):
        """Nach einem 1s-Stall: genau 1 Send beim nächsten Tick, kein Nachholen."""
        t0 = 500.0
        bot._next_server_update = t0
        bot._send_update = MagicMock()
        # normale Kadenz etablieren
        for i in range(6):
            bot._maybe_send_server_update(t0 + i * TICK)
        established = bot._send_update.call_count
        # 1s Stall, dann zwei direkt aufeinanderfolgende Ticks
        t_after = t0 + 6 * TICK + 1.0
        bot._maybe_send_server_update(t_after)
        assert bot._send_update.call_count == established + 1
        bot._maybe_send_server_update(t_after + TICK)
        assert bot._send_update.call_count == established + 1, \
            "direkt nach dem Stall-Send darf kein Nachhol-Burst folgen"

    def test_update_throttle_rate_wird_respektiert(self, bot):
        """_updateThrottleRate=20 → Intervall 1/20 → ~40 Sends in 2s."""
        bot._server_update_interval = 1.0 / 20.0
        t0 = 500.0
        bot._next_server_update = t0
        sends = _run_ticks(bot, t0, seconds=2.0)
        assert 39 <= sends <= 41, f"erwartet ~40 Sends in 2s (20 Hz), waren {sends}"
