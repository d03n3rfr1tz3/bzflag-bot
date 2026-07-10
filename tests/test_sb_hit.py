"""
SB-Treffer-Korrektur: Teleporter-Pfad (phase_walls), SB-Längskapsel und
lastfeste Hit-Detection (echtes Prüf-Fenster + Relativ-Sweep).

Hintergrund (BZFlag-Client-Quellcode): SB erbt checkHit unverändert (Geometrie
wie Normal), aber makeSegments(Through) überspringt nur die Gebäude-Suche —
der Teleporter-Lookup läuft unabhängig davon: SB-Schüsse TELEPORTIEREN (live
verifiziert). Die Längskapsel ist eine bewusste, kleine Abweichung vom Client
(Längsreichweite 2×_shotRadius, seitlich unverändert).

Konstanten (Defaults):
  half_len = 6.0/2 + 0.5 = 3.5   half_w = 2.8/2 + 0.5 = 1.9
  half_h   = 2.05/2 + 0.5 = 1.525   tank_cz = 1.025
  SB-Kapsel: Segment beidseitig +0.5 → Längsreichweite bis 3.5+0.5 = 4.0
"""
import math
import struct
import time
import pytest

from conftest import make_shot, make_player
from bzflag.world_map import WorldMap, BoxObstacle, TeleporterObstacle
from bzflag.shot_physics import (
    build_link_map, simulate_shot_path, Segment, _extend_segment,
)

TANK_CZ = 1.025


def _tele(name, cx, cy, bz=0.0, angle=0.0):
    return TeleporterObstacle(name=name, cx=cx, cy=cy, bottom_z=bz, angle=angle,
                              half_w=0.5, half_d=5.0, height=10.0, border=1.0)


def _pair(x0=0.0, x1=50.0):
    teles = [_tele("t0", x0, 0.0), _tele("t1", x1, 0.0)]
    lmap = build_link_map([(0, 3), (3, 0), (1, 2), (2, 1)])
    return teles, lmap


def _wall(cx=-5.0):
    return BoxObstacle(cx=cx, cy=0.0, bottom_z=0.0, angle=0.0,
                       half_w=1.0, half_d=20.0, height=10.0)


def _shot_payload(shooter_id=2, shot_id=1, pos=(-10.0, 0.0, 3.0),
                  vel=(100.0, 0.0, 0.0), team=2, flag=b"\x00\x00",
                  lifetime=2.0):
    """Gültiges MsgShotBegin-Payload (43 Bytes)."""
    return (struct.pack(">f", 0.0)
            + struct.pack(">B", shooter_id)
            + struct.pack(">H", shot_id)
            + struct.pack(">fff", *pos)
            + struct.pack(">fff", *vel)
            + struct.pack(">f", 0.0)
            + struct.pack(">h", team)
            + flag
            + struct.pack(">f", lifetime))


# ── Teil A1: simulate_shot_path(phase_walls=True) ────────────────────────────

class TestPhaseWalls:
    def test_wall_does_not_stop_phasing_shot(self):
        """phase_walls: Wand zwischen Start und Teleporter wird durchflogen,
        der Teleporter greift trotzdem (Kern des SB-Fixes)."""
        teles, lmap = _pair()
        segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                                  b"SB", [_wall()], 400.0, False,
                                  teleporters=teles, link_map=lmap,
                                  phase_walls=True)
        assert len(segs) == 2
        assert abs(segs[0].ex - 0.0) < 1e-3       # bis zur T0-Ebene (NICHT Wand bei x=-6)
        assert abs(segs[1].px - 50.0) < 1.0       # Austritt am Ziel-Teleporter
        assert segs[1].ex > segs[1].px            # läuft weiter

    def test_same_wall_stops_normal_shot(self):
        """Kontrast: identische Geometrie ohne phase_walls → Wand stoppt."""
        teles, lmap = _pair()
        segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                                  b"\x00\x00", [_wall()], 400.0, False,
                                  teleporters=teles, link_map=lmap)
        assert len(segs) == 1
        assert abs(segs[0].ex - (-6.0)) < 1e-3    # Wand-Vorderseite (cx=-5, half_w=1)

    def test_phase_walls_ignores_world_boundary(self):
        """phase_walls überfliegt auch die Weltgrenze bis zum Lifetime-Ende."""
        teles, lmap = _pair(x0=-300.0, x1=-200.0)   # Tore abseits der Bahn
        segs = simulate_shot_path((390.0, 100.0, 3.0), (100.0, 0.0, 0.0), 0.0, 1.0,
                                  b"SB", [], 400.0, False,
                                  teleporters=teles, link_map=lmap,
                                  phase_walls=True)
        assert len(segs) == 1
        assert abs(segs[0].ex - 490.0) < 1e-3
        # Kontrast: Normal-Schuss endet an der Weltgrenze x=400
        segs_n = simulate_shot_path((390.0, 100.0, 3.0), (100.0, 0.0, 0.0), 0.0, 1.0,
                                    b"\x00\x00", [], 400.0, False,
                                    teleporters=teles, link_map=lmap)
        assert abs(segs_n[-1].ex - 400.0) < 1e-3

    def test_phase_walls_without_teleporters_is_straight(self):
        """Früher Zweig: phase_walls ohne Tore → EIN gerades Segment, auch mit
        Server-Ricochet (SB reflektiert nie)."""
        segs = simulate_shot_path((0.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 1.0,
                                  b"SB", [_wall(cx=50.0)], 400.0, True,
                                  phase_walls=True)
        assert len(segs) == 1
        assert abs(segs[0].ex - 100.0) < 1e-6


# ── Teil B: _extend_segment (Längskapsel-Helfer) ─────────────────────────────

class TestExtendSegment:
    def test_extends_both_ends_along_direction(self):
        ax, ay, az, bx, by, bz = _extend_segment(0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.5)
        assert (ax, ay, az) == pytest.approx((-0.5, 0.0, 0.0))
        assert (bx, by, bz) == pytest.approx((10.5, 0.0, 0.0))

    def test_diagonal_keeps_direction(self):
        ax, ay, az, bx, by, bz = _extend_segment(0.0, 0.0, 0.0, 3.0, 4.0, 0.0, 5.0)
        assert (ax, ay) == pytest.approx((-3.0, -4.0))    # 5u entlang (0.6, 0.8)
        assert (bx, by) == pytest.approx((6.0, 8.0))

    def test_degenerate_zero_length_unchanged(self):
        res = _extend_segment(1.0, 2.0, 3.0, 1.0, 2.0, 3.0, 0.5)
        assert res == (1.0, 2.0, 3.0, 1.0, 2.0, 3.0)


# ── Teil A2: _on_shot_begin routet SB/PZ durch die Pfad-Sim ──────────────────

class TestSbShotRouting:
    def _wire_world(self, bot, boxes=None, with_teles=True):
        teles, lmap = _pair()
        wm = WorldMap(boxes=boxes or [], teleporters=teles if with_teles else [],
                      links=[(0, 3), (3, 0), (1, 2), (2, 1)],
                      world_half=400.0, world_hash="")
        bot._world_map = wm
        bot._link_map = lmap
        bot.world_half = 400.0
        return wm

    def test_sb_gets_teleported_phase_path(self, bot):
        """SB auf Teleporter-Karte: Pfad-Cache mit 2 Segmenten, Wand durchflogen."""
        self._wire_world(bot, boxes=[_wall()])
        bot.pos_x = 0.0; bot.pos_y = 100.0; bot.pos_z = 0.0                  # abseits der Schussbahn
        bot._on_shot_begin(0, _shot_payload(flag=b"SB"))
        segs = bot._ricochet_paths.get((2, 1))
        assert segs is not None and len(segs) == 2
        assert abs(segs[0].ex - 0.0) < 1e-3          # durch die Wand bis zur T0-Ebene
        assert abs(segs[1].px - 50.0) < 1.0

    def test_normal_shot_still_stops_at_wall(self, bot):
        """Kontrast: Normal-Schuss auf derselben Karte endet an der Wand."""
        self._wire_world(bot, boxes=[_wall()])
        bot.pos_x = 0.0; bot.pos_y = 100.0; bot.pos_z = 0.0
        bot._server_ricochet = False
        bot._on_shot_begin(0, _shot_payload(flag=b"\x00\x00"))
        segs = bot._ricochet_paths.get((2, 1))
        assert segs is not None and len(segs) == 1
        assert abs(segs[0].ex - (-6.0)) < 1e-3

    def test_pz_shooter_gets_phase_path(self, bot):
        """Phantom-Zone-Schütze: Normal-Flagge phased ebenfalls (inkl. Teleport)."""
        self._wire_world(bot, boxes=[_wall()])
        bot.pos_x = 0.0; bot.pos_y = 100.0; bot.pos_z = 0.0
        p = make_player(bot, 2)
        p.is_phantom_zoned = True
        bot._on_shot_begin(0, _shot_payload(flag=b"\x00\x00"))
        segs = bot._ricochet_paths.get((2, 1))
        assert segs is not None and len(segs) == 2

    def test_sb_without_teleporters_stays_uncached(self, bot):
        """Ohne Tore bleibt SB im geraden else-Zweig (kein Cache)."""
        self._wire_world(bot, with_teles=False)
        bot.pos_x = 0.0; bot.pos_y = 100.0; bot.pos_z = 0.0
        bot._server_ricochet = True                  # selbst mit Rico: SB prallt nie ab
        bot._on_shot_begin(0, _shot_payload(flag=b"SB"))
        assert (2, 1) not in bot._ricochet_paths


# ── Teil B: SB-Längskapsel in _resolve_incoming_shots ────────────────────────

class TestSbCapsule:
    def test_sb_tip_hits_where_normal_misses(self, bot):
        """Schusszentrum endet 0,3u vor der OBB-Stirn (x=3.8 > 3.5): die um
        _shotRadius verlängerte SB-Spitze trifft, Normal nicht."""
        now = time.monotonic()
        for flag, expect_dead in ((b"\x00\x00", False), (b"SB", True)):
            bot.alive = True
            bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
            with bot._shots_lock:
                bot._shots.clear(); bot._ricochet_paths.clear()
            make_shot(bot, shooter_id=2, shot_id=1,
                      pos=(4.8, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                      flag_abbr=flag, fire_time=now - 0.01)
            bot._resolve_incoming_shots(now, 0.02)
            assert bot.alive is (not expect_dead), flag

    def test_sb_capsule_does_not_widen_laterally(self, bot):
        """Vorbeiflug bei y=2.0 (> half_w=1.9): SB bleibt ein Miss — die Kapsel
        verlängert nur längs. Sanity: y=1.8 trifft."""
        for y, expect_dead in ((2.0, False), (1.8, True)):
            now = time.monotonic()
            bot.alive = True
            bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
            bot._last_hit_check_t = now - 0.5
            bot._last_hit_check_pos = (0.0, 0.0, 0.0)
            with bot._shots_lock:
                bot._shots.clear(); bot._ricochet_paths.clear()
            make_shot(bot, shooter_id=2, shot_id=1,
                      pos=(50.0, y, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                      flag_abbr=b"SB", fire_time=now - 0.5)
            bot._resolve_incoming_shots(now, 0.02)
            assert bot.alive is (not expect_dead), y

    def test_sb_tip_in_cached_segment_branch(self, bot):
        """Ricochet/Teleporter-Zweig: gecachtes Segment endet 0,3u vor der OBB
        (wie ein Teleporter-Eintritt kurz vor dem Opfer) → SB trifft via
        Spitzen-Verlängerung, Normal nicht."""
        for flag, expect_dead in ((b"\x00\x00", False), (b"SB", True)):
            now = time.monotonic()
            bot.alive = True
            bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
            bot._last_hit_check_t = now - 0.5
            bot._last_hit_check_pos = (0.0, 0.0, 0.0)
            with bot._shots_lock:
                bot._shots.clear(); bot._ricochet_paths.clear()
            make_shot(bot, shooter_id=2, shot_id=1,
                      pos=(50.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                      flag_abbr=flag, fire_time=now - 0.4)
            with bot._shots_lock:
                bot._ricochet_paths[(2, 1)] = [
                    Segment(50.0, 0.0, TANK_CZ, 3.8, 0.0, TANK_CZ,
                            now - 0.4, now)]
            bot._resolve_incoming_shots(now, 0.02)
            assert bot.alive is (not expect_dead), flag


# ── Teil C: Lastfeste Hit-Detection ──────────────────────────────────────────

class TestLoadRobustHitWindow:
    def test_long_gap_since_last_check_is_covered(self, bot):
        """C1: Querung liegt 0,3s zurück (älter als der alte 0,2s-Deckel), der
        letzte Check 0,5s — das echte Fenster deckt sie ab, der alte
        dt-geklemmte Test hätte sie übersprungen (away-skip)."""
        now = time.monotonic()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._last_hit_check_t = now - 0.5
        bot._last_hit_check_pos = (0.0, 0.0, 0.0)
        # gefeuert vor 0,4s bei x=10 → quert den Tank bei now-0,3, jetzt x=-30
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(10.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                  fire_time=now - 0.4)
        bot._resolve_incoming_shots(now, 0.016)
        assert bot.alive is False

    def test_respawn_resets_window_no_ghost_hit(self, bot):
        """C1: Spawn setzt die Fenster-Referenz zurück — ein Schuss, der die
        Spawn-Position VOR dem Spawn querte, trifft nicht (Geister-Treffer)."""
        now = time.monotonic()
        bot._last_hit_check_t = now - 5.0           # lange tot
        bot._last_hit_check_pos = (0.0, 0.0, 0.0)
        payload = (struct.pack(">B", 1) + struct.pack(">fff", 0.0, 0.0, 0.0)
                   + struct.pack(">f", 0.0))
        bot._on_alive(0, payload)
        assert bot._last_hit_check_t >= now - 0.1
        assert bot._last_hit_check_pos == (0.0, 0.0, 0.0)
        # Schuss querte den Spawn-Punkt bei now-0,5 (Bot war da noch tot)
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(50.0, 0.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                  fire_time=now - 1.0)
        bot._resolve_incoming_shots(time.monotonic(), 0.016)
        assert bot.alive is True

    def test_relative_sweep_catches_own_motion(self, bot):
        """C2: Der Tank fährt WÄHREND des Fensters durch die Schussbahn (Schuss
        bei y=−5, Tank von y=−10 nach y=0): der Relativ-Sweep trifft, der
        statische Test (keine Eigenbewegung) nicht."""
        for prev_pos, expect_dead in (((0.0, -10.0, 0.0), True),
                                      ((0.0, 0.0, 0.0), False)):
            now = time.monotonic()
            bot.alive = True
            bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
            bot._last_hit_check_t = now - 0.5
            bot._last_hit_check_pos = prev_pos
            with bot._shots_lock:
                bot._shots.clear(); bot._ricochet_paths.clear()
            make_shot(bot, shooter_id=2, shot_id=1,
                      pos=(25.0, -5.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                      fire_time=now - 0.5)
            bot._resolve_incoming_shots(now, 0.016)
            assert bot.alive is (not expect_dead), prev_pos

    def test_teleport_jump_disables_sweep(self, bot):
        """C2-Guard: unplausibel großer Eigen-Sprung (Teleport) → Korrektur
        entfällt, statischer Test, kein Falsch-Treffer."""
        now = time.monotonic()
        bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
        bot._last_hit_check_t = now - 0.5
        bot._last_hit_check_pos = (0.0, -200.0, 0.0)   # >> 2*speed*0.5+5
        make_shot(bot, shooter_id=2, shot_id=1,
                  pos=(25.0, -5.0, TANK_CZ), vel=(-100.0, 0.0, 0.0),
                  fire_time=now - 0.5)
        bot._resolve_incoming_shots(now, 0.016)
        assert bot.alive is True
