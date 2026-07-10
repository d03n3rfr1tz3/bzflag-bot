"""
Laser/Thief-Mehrfach-Abpraller: End-to-End über bot._on_shot_begin.

Der echte BZFlag-Client empfängt in MsgShotBegin BASIS-Velocity und BASIS-
Lifetime und multipliziert lokal nach (SegmentedShotStrategy.cxx:
`f.shot.vel[i] *= <flag>AdVel`, `f.lifetime *= <flag>AdLife`). Erst mit dieser
Nachjustierung erreicht ein Laser seine echte Reichweite
(Default: 100·1000 u/s × 3,5·0,1 s = 35 000 u) und damit viele Abpraller —
BZFlag erlaubt bis zu 100. Diese Tests speisen deshalb bewusst BASIS-Werte
über den Handler-Pfad ein; reine Physik-Tests (test_shot_physics.py) fangen
den Bug NICHT, weil sie bereits skalierte Werte an simulate_shot_path geben.

Geometrie: Korridor aus zwei parallelen dünnen Wänden. Der Strahl startet
fast senkrecht zur Wand (kleiner Winkel zur x-Achse) und pendelt mit
konstantem y-Drift pro Querung durch den Korridor; der Bot wird gezielt auf
Querung N platziert.
"""
import math
import struct

import pytest

from bzflag.world_map import WorldMap, BoxObstacle
from bzflag.protocol import MsgShotBegin, MsgKilled, MsgTransferFlag

TANK_CZ = 1.025   # Mündungshöhe = Tank-Zentrum (Standard-Defaults)


def _shot_payload(shooter_id=2, shot_id=1, pos=(5.0, -50.0, TANK_CZ),
                  vel=(100.0, 0.0, 0.0), team=2, flag=b"L\x00", lifetime=3.5):
    """Gültiges 43-Byte-MsgShotBegin-Payload mit BASIS-Werten (wie vom Server)."""
    return (struct.pack(">f", 0.0)
            + struct.pack(">B", shooter_id)
            + struct.pack(">H", shot_id)
            + struct.pack(">fff", *pos)
            + struct.pack(">fff", *vel)
            + struct.pack(">f", 0.0)
            + struct.pack(">h", team)
            + flag
            + struct.pack(">f", lifetime))


def _wire_corridor(bot, x0=0.0, x1=100.0, hd=250.0, world_half=400.0):
    """Zwei parallele dünne Wände bei x0/x1 (hw=1 → Innenflächen x0+1 / x1−1)."""
    walls = [
        BoxObstacle(cx=x0, cy=0.0, bottom_z=0.0, angle=0.0,
                    half_w=1.0, half_d=hd, height=20.0),
        BoxObstacle(cx=x1, cy=0.0, bottom_z=0.0, angle=0.0,
                    half_w=1.0, half_d=hd, height=20.0),
    ]
    bot._world_map = WorldMap(boxes=walls, teleporters=[], links=[],
                              world_half=world_half, world_hash="")
    bot.world_half = world_half
    bot._server_ricochet = True


def _crossing_mid_y(x_start, y_start, x_lo, x_hi, tan_a, n):
    """y-Position, an der der Strahl (Start Richtung +x, Steigung tan_a) die
    Korridor-Mitte bei Querung n kreuzt (Querung 0 = Erstdurchflug)."""
    mid = (x_lo + x_hi) / 2.0
    width = x_hi - x_lo
    if n == 0:
        travel = mid - x_start
    else:
        travel = (x_hi - x_start) + (n - 1) * width + width / 2.0
    return y_start + travel * tan_a


class TestLaserMultibounce:
    """Korridor 98u breit (Innenflächen x=1/x=99); Laser bei 2° zur x-Achse
    → y-Drift ≈ 3,42u pro Querung, Reichweite 35 000u → max_bounces-Kappe."""
    ANGLE = math.radians(2.0)

    def _fire(self, bot, shot_id=1):
        vel = (100.0 * math.cos(self.ANGLE), 100.0 * math.sin(self.ANGLE), 0.0)
        bot._on_shot_begin(MsgShotBegin, _shot_payload(
            shot_id=shot_id, pos=(5.0, -50.0, TANK_CZ), vel=vel,
            flag=b"L\x00", lifetime=3.5))

    def _place_bot(self, bot, crossing):
        _wire_corridor(bot)
        bot.pos_x = 50.0
        bot.pos_y = _crossing_mid_y(5.0, -50.0, 1.0, 99.0,
                                    math.tan(self.ANGLE), crossing)
        bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True

    @pytest.mark.parametrize("crossing", [4, 12, 40])
    def test_laser_kills_after_many_bounces(self, bot, crossing):
        """Bot auf Querung N: der Strahl geht durch ihn hindurch → Kill.
        Ohne AdVel/AdLife-Nachjustierung reicht der Laser nur 350u
        (≈ 3 Querungen) — bereits N=4 wäre unerreichbar."""
        self._place_bot(bot, crossing)
        self._fire(bot)
        segs = bot._ricochet_paths.get((2, 1))
        assert segs is not None and len(segs) >= 10
        codes = [c[0][0] for c in bot.client.send.call_args_list]
        assert MsgKilled in codes

    def test_laser_no_kill_off_beam(self, bot):
        """Negativkontrolle: Bot unterhalb der ersten Strahlbahn (der Drift
        läuft nur nach +y) → voller Segment-Pfad, aber kein Treffer."""
        self._place_bot(bot, 0)
        bot.pos_y = -100.0
        self._fire(bot)
        segs = bot._ricochet_paths.get((2, 1))
        assert segs is not None and len(segs) >= 10
        codes = [c[0][0] for c in bot.client.send.call_args_list]
        assert MsgKilled not in codes
        assert bot.alive is True


class TestThiefMultibounce:
    """Enger Korridor 20u (Innenflächen x=1/x=21); TH-Reichweite mit
    Nachjustierung 100·8 u/s × 3,5·0,05 s = 140u → ~6 volle Querungen.
    OHNE Nachjustierung wären es fälschlich 100·3,5 = 350u (~17 Querungen)."""
    ANGLE = math.radians(5.0)

    def _fire(self, bot, shot_id=1):
        vel = (100.0 * math.cos(self.ANGLE), 100.0 * math.sin(self.ANGLE), 0.0)
        bot._on_shot_begin(MsgShotBegin, _shot_payload(
            shot_id=shot_id, pos=(3.0, -50.0, TANK_CZ), vel=vel,
            flag=b"TH", lifetime=3.5))

    def _place_bot(self, bot, crossing):
        _wire_corridor(bot, x0=0.0, x1=22.0)
        bot.pos_x = 11.0
        bot.pos_y = _crossing_mid_y(3.0, -50.0, 1.0, 21.0,
                                    math.tan(self.ANGLE), crossing)
        bot.pos_z = 0.0
        bot.azimuth = 0.0
        bot.alive = True
        bot.own_flag = "GM"

    def test_thief_steals_after_many_bounces(self, bot):
        """Bot auf Querung 4 (Pfadweg ≈ 88u < 140u): TH-Abpraller stiehlt die
        Flagge → MsgTransferFlag."""
        self._place_bot(bot, 4)
        self._fire(bot)
        segs = bot._ricochet_paths.get((2, 1))
        assert segs is not None and len(segs) >= 4
        codes = [c[0][0] for c in bot.client.send.call_args_list]
        assert MsgTransferFlag in codes

    def test_thief_out_of_range_no_steal(self, bot):
        """Bot auf Querung 10 (Pfadweg ≈ 208u > 140u): außerhalb der ECHTEN
        TH-Reichweite → kein Diebstahl. Ohne AdVel/AdLife-Nachjustierung
        (350u Reichweite) würde hier fälschlich MsgTransferFlag gesendet."""
        self._place_bot(bot, 10)
        self._fire(bot)
        codes = [c[0][0] for c in bot.client.send.call_args_list]
        assert MsgTransferFlag not in codes
        assert bot.own_flag == "GM"
