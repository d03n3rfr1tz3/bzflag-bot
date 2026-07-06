"""Tests für die Status-Bit-Zusammensetzung in BZBot._send_update (PRO-07).

Prüft PS_CROSSING: OO-Tank durch eine Wand und (jede Flagge) das Straddeln einer
Teleporter-Querungsebene setzen das CrossingWall-Bit, das clientseitig den Phasing-
Effekt triggert. Ausgelesen wird das an build_player_update übergebene `status`-kwarg
(robust gegen das Payload-Layout).
"""
import time
from unittest.mock import patch

from bzflag.world_map import BoxObstacle, WorldMap, TeleporterObstacle
from bzflag.protocol import PS_CROSSING, PS_TELEPORTING, PS_ALIVE


def _make_world(boxes=None, teleporters=None):
    return WorldMap(boxes=boxes or [], teleporters=teleporters or [],
                    links=[], world_half=100.0, world_hash="test")


def _sent_status(bot):
    """Ruft _send_update auf und liefert das gesetzte `status`-Feld zurück."""
    with patch("bot.core.build_player_update") as bpu:
        bot._send_update()
    assert bpu.called, "erwartete ein MsgPlayerUpdate"
    return bpu.call_args.kwargs["status"]


def _solid_box():
    # Box um den Ursprung, groß genug, dass der Tank an (0,0,0) sie überlappt.
    return BoxObstacle(cx=0.0, cy=0.0, bottom_z=0.0, angle=0.0,
                       half_w=5.0, half_d=5.0, height=5.0)


def _teleporter():
    # Dünne Querungsebene um den Ursprung: Feld = half_w × (half_d-border).
    return TeleporterObstacle(name="tele", cx=0.0, cy=0.0, bottom_z=0.0, angle=0.0,
                              half_w=0.5, half_d=5.0, height=10.0, border=1.0)


class TestCrossingWall:
    def test_oo_in_wand_setzt_ps_crossing(self, bot):
        bot.own_flag = "OO"
        bot._world_map = _make_world(boxes=[_solid_box()])
        bot.pos = [0.0, 0.0, 0.0]
        status = _sent_status(bot)
        assert status & PS_ALIVE
        assert status & PS_CROSSING

    def test_oo_frei_kein_ps_crossing(self, bot):
        bot.own_flag = "OO"
        bot._world_map = _make_world(boxes=[_solid_box()])
        bot.pos = [50.0, 0.0, 0.0]   # neben der Box
        assert not (_sent_status(bot) & PS_CROSSING)

    def test_nicht_oo_in_wand_kein_ps_crossing(self, bot):
        # Nur OO phast durch Wände — eine andere Flagge in derselben Box crosst nicht.
        bot.own_flag = ""
        bot._world_map = _make_world(boxes=[_solid_box()])
        bot.pos = [0.0, 0.0, 0.0]
        assert not (_sent_status(bot) & PS_CROSSING)

    def test_teleporter_straddle_setzt_ps_crossing(self, bot):
        # Gilt für jede Flagge (hier ohne).
        bot.own_flag = ""
        bot._world_map = _make_world(teleporters=[_teleporter()])
        bot.pos = [0.0, 0.0, 0.0]
        assert _sent_status(bot) & PS_CROSSING

    def test_neben_teleporter_kein_ps_crossing(self, bot):
        bot.own_flag = ""
        bot._world_map = _make_world(teleporters=[_teleporter()])
        bot.pos = [10.0, 0.0, 0.0]   # außerhalb des Querungsfelds
        assert not (_sent_status(bot) & PS_CROSSING)

    def test_ps_teleporting_bleibt_erhalten(self, bot):
        # Regression: PS_CROSSING-Logik darf PS_TELEPORTING nicht verdrängen.
        bot.own_flag = ""
        bot._world_map = _make_world()
        bot.pos = [0.0, 0.0, 0.0]
        bot._teleporting_until = time.monotonic() + 1.0
        status = _sent_status(bot)
        assert status & PS_TELEPORTING
        assert not (status & PS_CROSSING)
