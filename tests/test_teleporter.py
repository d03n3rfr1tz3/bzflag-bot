"""
Tests für Teleporter-Support (P3-NAV-01a Schuss-Physik, P3-NAV-01c Gegner-Event).

Deckt die Geometrie-Helfer (Port von BZFlags hasCrossed/getPointWRT), die
teleporterbewusste simulate_shot_path() und den MsgTeleport-Handler ab.
"""
import math
import struct
import pytest

from bzflag.world_map import (TeleporterObstacle, BoxObstacle, WorldMap,
                              teleporter_solid_boxes, teleporter_field_box)
from bzflag.nav_graph import NavGraph
from bzflag.shot_physics import (
    build_link_map, ray_teleporter_crossing, simulate_shot_path,
)
from bzflag.protocol import MsgTeleport


def _tele(name, cx, cy, bz=0.0, angle=0.0, hw=0.5, hd=5.0, height=10.0, border=1.0):
    return TeleporterObstacle(name=name, cx=cx, cy=cy, bottom_z=bz, angle=angle,
                              half_w=hw, half_d=hd, height=height, border=border)


def _pair(x0=0.0, x1=50.0, bz1=0.0):
    """Zwei achsparallele Teleporter, gespiegelt verlinkt. face-Index = ti*2 + face."""
    teles = [_tele("t0", x0, 0.0), _tele("t1", x1, 0.0, bz=bz1)]
    lmap = build_link_map([(0, 3), (3, 0), (1, 2), (2, 1)])
    return teles, lmap


# ── Link-Map ────────────────────────────────────────────────────────────────

def test_link_map_first_wins():
    # ponytail: Mehrfach-Links → erster gewinnt (deterministisch)
    assert build_link_map([(0, 3), (0, 5), (1, 2)]) == {0: 3, 1: 2}


# ── ray_teleporter_crossing ───────────────────────────────────────────────────

def test_crossing_detects_field():
    teles, _ = _pair()
    res = ray_teleporter_crossing(-10.0, 0.0, 3.0, 100.0, 0.0, 0.0, teles[0])
    assert res is not None
    t, face = res
    assert abs(t - 0.1) < 1e-6       # x=0 nach 10 Einheiten bei 100 u/s
    assert face == 1                 # kommt von der x_local<0-Seite


def test_crossing_misses_outside_field():
    teles, _ = _pair()
    # y außerhalb des passierbaren Felds (half_d - border = 4)
    assert ray_teleporter_crossing(-10.0, 10.0, 3.0, 100.0, 0.0, 0.0, teles[0]) is None
    # z oberhalb (height - border = 9)
    assert ray_teleporter_crossing(-10.0, 0.0, 9.5, 100.0, 0.0, 0.0, teles[0]) is None


def test_crossing_parallel_returns_none():
    teles, _ = _pair()
    # Strahl parallel zur Feld-Ebene (rein in +y) quert nie
    assert ray_teleporter_crossing(0.0, -10.0, 3.0, 0.0, 100.0, 0.0, teles[0]) is None


# ── simulate_shot_path mit Teleportern ────────────────────────────────────────

def test_shot_transported_to_linked_teleporter():
    teles, lmap = _pair()
    segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                              b"PZ", [], 400.0, False,
                              teleporters=teles, link_map=lmap)
    assert len(segs) == 2
    assert abs(segs[0].ex - 0.0) < 1e-3      # Eintritt an T0-Ebene
    assert abs(segs[1].px - 50.0) < 1.0      # Austritt am Ziel-Teleporter
    assert abs(segs[1].pz - 3.0) < 1e-3      # z erhalten
    assert segs[1].ex > segs[1].px           # läuft weiter (+x), keine Reflexion


def test_z_height_offset_on_exit():
    # Ziel-Teleporter auf z=30-Plattform (wie HIX): Austritt erhöht
    teles, lmap = _pair(bz1=30.0)
    segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                              b"PZ", [], 400.0, False,
                              teleporters=teles, link_map=lmap)
    assert len(segs) == 2
    assert abs(segs[1].pz - 33.0) < 1e-3     # 30 + (3-0)*scale(=1)


def test_no_false_ricochet_off_wall_behind_teleporter():
    """Regression Selbstabschuss: Wand hinter T0 → R-Schuss teleportiert DURCH statt zurück."""
    teles, lmap = _pair()
    wall = BoxObstacle(cx=3.0, cy=0.0, bottom_z=0.0, angle=0.0,
                       half_w=1.0, half_d=20.0, height=10.0)
    segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                              b"R\x00", [wall], 400.0, False,
                              teleporters=teles, link_map=lmap)
    assert segs[-1].ex > 50.0                # jenseits des Ziel-Teleporters
    assert all(s.ex >= -1e-6 for s in segs)  # nirgends nach -x zurückgeprallt


def test_unlinked_face_is_ignored():
    teles, _ = _pair()
    segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 1.0,
                              b"PZ", [], 400.0, False,
                              teleporters=teles, link_map={})  # keine Links
    assert len(segs) == 1                    # kein Transport → gerader Pfad
    assert abs(segs[0].ex - 90.0) < 1e-6


def test_existing_behavior_without_teleporters():
    # Ohne tele-args: nicht-ricochet = ein gerades Segment (unverändert)
    segs = simulate_shot_path((0.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 1.0,
                              b"PZ", [], 400.0, False)
    assert len(segs) == 1
    assert abs(segs[0].ex - 100.0) < 1e-6


def test_rotated_teleporter_routes_shot():
    """HIX-Ecken sind diagonal (angle=45°) — Routing muss auch rotiert greifen.
    Schließt die angle=0-Lücke der übrigen Tests."""
    import math
    a = math.pi / 4
    teles = [_tele("t0", 0.0, 0.0, angle=a), _tele("t1", 50.0, 0.0, angle=a)]
    lmap = build_link_map([(0, 3), (3, 0), (1, 2), (2, 1)])
    # Schuss entlang lokaler X-Achse von T0 (Weltrichtung (cos45, sin45))
    d = math.cos(a) * 100.0
    segs = simulate_shot_path((-d * 0.0707, -d * 0.0707, 3.0), (d, d, 0.0),
                              0.0, 2.0, b"PZ", [], 400.0, False,
                              teleporters=teles, link_map=lmap)
    assert len(segs) >= 2                      # transportiert, nicht gerade durch
    assert abs(segs[-1].px - 50.0) < 2.0       # Austritt nahe T1
    assert abs(segs[-1].pz - 3.0) < 0.5        # z erhalten


# ── Echtes Wire-Format: Links sind nboStdString-Paare ("/tN:f|b") ─────────────

def test_resolve_link_face():
    from bzflag.world_parser import _resolve_link_face
    nm = {"/t0": 0, "/t1": 1, "Foo": 2}
    assert _resolve_link_face("/t0:f", nm) == 0    # tele 0 front
    assert _resolve_link_face("/t1:b", nm) == 3    # tele 1 back
    assert _resolve_link_face(":/t1:B", nm) == 3   # absolutes ':' + Großbuchstabe
    assert _resolve_link_face("Foo:f", nm) == 4    # benannter Teleporter
    assert _resolve_link_face("/t9:f", nm) == 18   # Auto-Name per Index
    assert _resolve_link_face("nope", nm) is None  # ohne :f/:b → unauflösbar


def test_link_wire_format_parsed_and_routes():
    """End-to-End: echtes String-Link-Format → parse_world → routing.
    Schlägt mit dem alten uint32-Parser fehl (Müll-Links → kein Transport)."""
    from bzflag.world_parser import parse_world
    from test_world_parser import _build_world_data
    data = _build_world_data(
        teleporters=[
            ("", 0.0,  0.0, 0.0, 0.0, 0.5, 5.0, 10.0, 1.0),   # unbenannt → /t0
            ("", 50.0, 0.0, 0.0, 0.0, 0.5, 5.0, 10.0, 1.0),   # unbenannt → /t1
        ],
        links=[(0, 3), (3, 0), (1, 2), (2, 1)],   # face-Indizes → "/tN:f|b"
    )
    wm = parse_world(data, world_half=400.0)
    assert wm is not None
    assert wm.links == [(0, 3), (3, 0), (1, 2), (2, 1)]
    segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                              b"PZ", wm.boxes, wm.world_half, False,
                              teleporters=wm.teleporters,
                              link_map=build_link_map(wm.links))
    assert len(segs) == 2
    assert abs(segs[1].px - 50.0) < 1.0        # am Ziel-Teleporter ausgetreten


def test_hix_fixture_links_resolve():
    """Regression gegen echten HIX-Welt-Buffer: alle 16 Links sauber aufgelöst,
    Eck-Paare z=0↔z=30 reziprok verlinkt."""
    from conftest import load_map_fixture
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    assert len(wm.teleporters) == 8
    assert len(wm.links) == 16                 # 4 Eck-Paare × 2 Faces × 2 Richtungen
    n_faces = 2 * len(wm.teleporters)
    lmap = build_link_map(wm.links)
    for src, dst in wm.links:
        assert 0 <= src < n_faces and 0 <= dst < n_faces
        assert lmap[dst] == src                # reziprok (HIX-Paare beidseitig)


# ── Querungsfeld-Größe: finalize()-Anpassung (Fix #2) ─────────────────────────

def test_hix_teleporter_field_width():
    """Regression: das HIX-Querungsfeld ist real ±5.60u breit, nicht ±3.36u.
    Schlägt mit dem rohen Parser fehl (fehlende finalize()-Vergrößerung)."""
    import math
    from conftest import load_map_fixture
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    tele = next(x for x in wm.teleporters if abs(x.bottom_z) < 1e-3)   # z=0-Tor (height 27.70)
    # effektive passierbare Halb-Breite = getBreadth()-border = half_d-border
    assert (tele.half_d - tele.border) == pytest.approx(5.60,  abs=0.05)  # nicht 3.36
    assert (tele.height - tele.border) == pytest.approx(27.70, abs=0.05)  # nicht 26.58

    # Schuss seitlich um 4.5u versetzt (innerhalb ±5.60, außerhalb des alten ±3.36)
    # durch das z=0-Eck-Tor muss teleportiert werden.
    lmap = build_link_map(wm.links)
    ti = wm.teleporters.index(tele)
    nx, ny = math.cos(tele.angle), math.sin(tele.angle)       # Feld-Normale
    ux, uy = -math.sin(tele.angle), math.cos(tele.angle)      # Passage-Richtung
    lat = 4.5
    sx = tele.cx + nx * 6.0 + ux * lat
    sy = tele.cy + ny * 6.0 + uy * lat
    segs = simulate_shot_path((sx, sy, 2.0), (-nx * 100.0, -ny * 100.0, 0.0),
                              0.0, 3.0, b"PZ", wm.boxes, wm.world_half, False,
                              teleporters=wm.teleporters, link_map=lmap)
    assert len(segs) >= 2                                      # transportiert, nicht gerade
    assert ti * 2 in lmap or ti * 2 + 1 in lmap               # Tor ist verlinkt


def test_tele_log_records_event():
    """simulate_shot_path(..., tele_log=L) hängt pro Querung genau ein Event an."""
    teles, lmap = _pair()
    log = []
    segs = simulate_shot_path((-10.0, 0.0, 3.0), (100.0, 0.0, 0.0), 0.0, 2.0,
                              b"PZ", [], 400.0, False,
                              teleporters=teles, link_map=lmap, tele_log=log)
    assert len(segs) == 2
    assert len(log) == 1
    e_ti, e_face, d_ti, d_face, ep, xp, ain, aout = log[0]
    assert e_ti == 0 and d_ti == 1                            # t0 → t1
    assert abs(ep[0] - 0.0) < 1e-3                            # Eintritt an T0-Ebene
    assert abs(xp[0] - 50.0) < 1.0                            # Austritt am Ziel-Teleporter


def test_compute_ricochet_aim_via_teleporter(bot):
    """_compute_ricochet_aim erkennt einen *Teleporter*-Schuss (via_tele=True) und liefert
    einen Winkel, wenn der Gegner nur über ein verlinktes Tor erreichbar ist."""
    from unittest.mock import MagicMock
    from conftest import make_player
    teles, lmap = _pair()                 # t0@(0,0), t1@(50,0), achsparallel verlinkt
    bot.pos_x = -20.0; bot.pos_y = -20.0; bot.pos_z = 0.0
    bot.own_flag = ""
    bot._server_ricochet = False          # kein Abprall → Treffer NUR via Teleporter
    bot.world_half = 400.0
    wmap = MagicMock(); wmap.boxes = []; wmap.teleporters = teles
    bot._world_map = wmap
    bot._link_map = lmap
    # 45°-Schuss durch t0 → Austritt an t1 in 45°-Richtung → trifft Gegner bei (80,30).
    make_player(bot, 2, pos=(80.0, 30.0, 0.0))
    result = bot._compute_ricochet_aim(2, None)
    assert result is not None, "Teleporter-Schuss soll gefunden werden"
    az, via_tele = result
    assert via_tele is True
    assert math.radians(35) <= az <= math.radians(55)


# ── MsgTeleport-Handler (P3-NAV-01c) ──────────────────────────────────────────

def test_msg_teleport_records_last_teleport(bot):
    from bot.models import PlayerInfo
    bot.players[5] = PlayerInfo(callsign="Foe", team=2, is_human=True)
    bot._on_teleport(MsgTeleport, struct.pack(">BHH", 5, 1, 6))
    lt = bot.players[5].last_teleport
    assert lt is not None
    assert lt[1] == 1 and lt[2] == 6


def test_compute_ricochet_aim_teleporter_hix_corner(bot):
    """Reproduktion der Live-Geometrie gegen hix.bin: Bot am z=0-Eck-Tor, Gegner auf der
    z=30-Plattform am verlinkten Tor. Der Schuss muss INS Tor zielen (Eintrittsrichtung ~45°),
    NICHT Richtung Gegner (direct_az ~ −135°). Vor dem Sweep-Fix lieferte _compute_ricochet_aim
    hier None (±RICO_AIM_MAX um die Gegner-Richtung deckt die Tor-Richtung nie ab)."""
    from conftest import load_map_fixture, make_player
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    bot.pos_x = 382.0; bot.pos_y = 382.0; bot.pos_z = 0.0
    bot.own_flag = ""
    bot._server_ricochet = False              # Treffer NUR via Tor (kein Abprall)
    bot.world_half = wm.world_half
    bot._world_map = wm
    bot._link_map = build_link_map(wm.links)
    make_player(bot, 2, pos=(370.0, 370.0, 30.0))   # z=30-Plattform, entlang der Austritts-Diagonale
    result = bot._compute_ricochet_aim(2, None)
    assert result is not None, "Cross-Floor-Teleporter-Schuss soll gefunden werden"
    az, via_tele = result
    assert via_tele is True
    direct_az = math.atan2(370.0 - 382.0, 370.0 - 382.0)        # ~ −135°
    assert math.radians(15) <= az <= math.radians(75)          # zeigt ins Tor (~45°)
    assert abs(math.degrees(az) - math.degrees(direct_az)) > 90  # weit weg von der Gegner-Richtung


def test_effective_shot_range_per_flag(bot):
    """_effective_shot_speed/_lifetime/_range bilden die AD-Multiplikatoren je Flagge ab
    (vel *= AdVel, lifetime *= AdLife; GM nur Lifetime). Sichert die Attribut-Zuordnung ab."""
    base_s, base_l = bot._shot_speed, bot._shot_lifetime
    bot.own_flag = ""
    assert bot._effective_shot_speed()    == pytest.approx(base_s)
    assert bot._effective_shot_lifetime() == pytest.approx(base_l)
    assert bot._effective_shot_range()    == pytest.approx(base_s * base_l)
    cases = {
        "L":  (base_s * bot._laser_ad_vel,      base_l * bot._laser_ad_life),
        "MG": (base_s * bot._mgun_ad_vel,       base_l * bot._mgun_ad_life),
        "F":  (base_s * bot._rfire_ad_vel,      base_l * bot._rfire_ad_life),
        "TH": (base_s * bot._thief_ad_shot_vel, base_l * bot._thief_ad_life),
        "GM": (base_s,                          base_l * bot._gm_ad_life),   # GM: Basis-Speed
    }
    for flag, (exp_s, exp_l) in cases.items():
        bot.own_flag = flag
        assert bot._effective_shot_speed()    == pytest.approx(exp_s), flag
        assert bot._effective_shot_lifetime() == pytest.approx(exp_l), flag
        assert bot._effective_shot_range()    == pytest.approx(exp_s * exp_l), flag


def test_compute_ricochet_aim_high_to_low_needs_z_tol(bot, monkeypatch):
    """Situation 1 (Bot oben z=30 → Gegner unten z=0): die Tor-Transform skaliert die relative
    Eintrittshöhe HOCH (≈1.85×), der Flachschuss tritt ~0.35u über dem Bodenpanzer aus. Nur mit
    TELE_AIM_Z_TOL findet _compute_ricochet_aim den Tor-Schuss; ohne Z-Spielraum bleibt es None."""
    from conftest import load_map_fixture, make_player
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    bot.pos_x = 382.0; bot.pos_y = 382.0; bot.pos_z = 30.0            # am z=30-Eck-Tor
    bot.own_flag = ""
    bot._server_ricochet = False              # Treffer NUR via Tor (kein Abprall)
    bot.world_half = wm.world_half
    bot._world_map = wm
    bot._link_map = build_link_map(wm.links)
    make_player(bot, 2, pos=(370.0, 370.0, 0.0))   # Bodengegner entlang der Austritts-Diagonale

    monkeypatch.setattr("bot.ai.shooting.TELE_AIM_Z_TOL", 0.0)
    assert bot._compute_ricochet_aim(2, None) is None, "ohne Z-Spielraum: Schuss tritt zu hoch aus"

    monkeypatch.setattr("bot.ai.shooting.TELE_AIM_Z_TOL", 1.0)
    result = bot._compute_ricochet_aim(2, None)
    assert result is not None and result[1] is True   # mit Z-Spielraum: Tor-Schuss gefunden


def test_indirect_shot_available(bot):
    """C1: _indirect_shot_available ist True bei verfügbarem Tor-Schuss (Cross-Floor-Geometrie),
    False ohne Teleporter UND ohne Abprall-Möglichkeit."""
    from conftest import load_map_fixture, make_player
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    bot.pos_x = 382.0; bot.pos_y = 382.0; bot.pos_z = 0.0
    bot.own_flag = ""
    bot._server_ricochet = False
    bot.world_half = wm.world_half
    bot._world_map = wm
    bot._link_map = build_link_map(wm.links)
    make_player(bot, 2, pos=(370.0, 370.0, 30.0))
    bot._rico_aim_cache = None
    assert bot._indirect_shot_available(2) is True

    bot._world_map = None                     # keine Tore + Normal-Flagge + kein Server-Ricochet
    bot._rico_aim_cache = None
    assert bot._indirect_shot_available(2) is False


def test_update_indirect_hold_caps_and_resets(bot):
    """C2: Zeit-Cap armt beim Eintritt, läuft nach INDIRECT_HOLD_S ab (ohne Re-Arm im selben Fall)
    und resettet beim Verlassen → armt beim nächsten Eintritt neu."""
    from bot.constants import INDIRECT_HOLD_S
    bot._indirect_hold_until = None
    assert bot._update_indirect_hold(100.0, True) is True            # Eintritt → armt bis 100+CAP
    assert bot._update_indirect_hold(100.0 + INDIRECT_HOLD_S - 0.1, True) is True
    assert bot._update_indirect_hold(100.0 + INDIRECT_HOLD_S + 0.1, True) is False  # abgelaufen
    assert bot._update_indirect_hold(100.0 + INDIRECT_HOLD_S + 0.2, True) is False  # kein Re-Arm
    assert bot._update_indirect_hold(200.0, False) is False          # Fall verlassen → Reset
    assert bot._indirect_hold_until is None
    assert bot._update_indirect_hold(300.0, True) is True            # neuer Eintritt → armt neu


def test_msg_teleport_tolerates_short_payload(bot):
    bot._on_teleport(MsgTeleport, b"\x00")   # darf nicht crashen


# ── P3-NAV-02: Fahr-Kollision, A*-Portal-Kanten, Positions-Sprung ─────────────

def _bot_world(bot, teles, lmap, boxes=None):
    """Verdrahtet eine minimale WorldMap (MagicMock) + link_map auf den Bot."""
    from unittest.mock import MagicMock
    wmap = MagicMock()
    wmap.boxes = boxes or []
    wmap.teleporters = teles
    bot._world_map = wmap
    bot._link_map = lmap
    bot.world_half = 400.0


def _tele_world(x0=-100.0, x1=100.0, bz1=0.0, half=200.0):
    teles = [_tele("t0", x0, 0.0), _tele("t1", x1, 0.0, bz=bz1)]
    return WorldMap(boxes=[], teleporters=teles,
                    links=[(0, 3), (3, 0), (1, 2), (2, 1)],
                    world_half=half, world_hash="")


def test_teleporter_solid_boxes_geometry():
    t = _tele("t", 0.0, 0.0, angle=0.0, hw=0.5, hd=5.0, height=10.0, border=1.0)
    boxes = teleporter_solid_boxes(t)
    assert len(boxes) == 3
    posts, cross = boxes[:2], boxes[2]
    # Posts: r=0.5, d=5-0.5=4.5 → bei (0,±4.5), Höhe bis crossbar_bottom=9
    assert sorted(p.cy for p in posts) == pytest.approx([-4.5, 4.5])
    for p in posts:
        assert p.cx == pytest.approx(0.0)
        assert p.half_w == pytest.approx(0.5) and p.half_d == pytest.approx(0.5)
        assert p.bottom_z == pytest.approx(0.0) and p.height == pytest.approx(9.0)
        assert not p.drive_through and not p.shoot_through
    # Crossbar: bottom_z=9, height=border=1, volle Breite/Tiefe
    assert cross.bottom_z == pytest.approx(9.0) and cross.height == pytest.approx(1.0)
    assert cross.half_w == pytest.approx(0.5) and cross.half_d == pytest.approx(5.0)


def test_teleporter_field_box_spans_gap():
    t = _tele("t", 0.0, 0.0, hw=0.5, hd=5.0, height=10.0, border=1.0)
    fb = teleporter_field_box(t)
    assert fb.half_w == pytest.approx(0.5)       # dünn in lokal-x
    assert fb.half_d == pytest.approx(4.0)       # half_d - border = passierbare Breite
    assert fb.height == pytest.approx(9.0)       # height - border


def test_navgraph_blocks_posts_field_but_field_not_floor():
    wm = _tele_world()
    nav = NavGraph(wm)
    ground = nav.layers[0]
    t0 = wm.teleporters[0]
    assert not ground.is_walkable_xy(t0.cx, t0.cy + 4.5)   # Post-Zelle gesperrt
    assert not ground.is_walkable_xy(t0.cx, t0.cy)         # Feld-Mitte gesperrt (Layer-Block)
    # ABER: das Feld ist KEINE Steh-/Lande-Fläche → get_floor_z bleibt 0
    assert nav.get_floor_z(t0.cx, t0.cy, 0.0) == pytest.approx(0.0)


def test_navgraph_teleport_edges_built():
    nav = NavGraph(_tele_world())
    assert nav._teleport_edges, "Portal-Kanten müssen vorberechnet sein"
    for entry, (exit_node, cost) in nav._teleport_edges.items():
        assert entry[0] == 0 and exit_node[0] == 0      # beide auf dem Boden-Layer
        assert cost > 0.0


def test_plan_path_uses_teleport_edge():
    nav = NavGraph(_tele_world(x0=-100.0, x1=100.0))
    g = nav.layers[0]
    entry, (exit_node, _c) = next(iter(nav._teleport_edges.items()))
    ex, ey = g.cell_to_world(entry[1], entry[2])
    qx, qy = g.cell_to_world(exit_node[1], exit_node[2])
    path = nav.plan_path(ex, ey, 0.0, qx, qy)
    assert path, "Pfad über Teleporter erwartet"
    # Der Portal-Hop (großer Sprung entry→exit, ~200u) muss als Wegpunktpaar erhalten sein
    assert any(math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1]) > 100.0
               for i in range(len(path) - 1)), "Portal-Hop fehlt im Pfad"
    assert math.hypot(path[-1][0] - qx, path[-1][1] - qy) < 4.0


def test_check_teleport_crossing_ground(bot):
    teles, lmap = _pair()                     # t0@(0,0), t1@(50,0)
    _bot_world(bot, teles, lmap)
    bot.own_flag = ""
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 3.0                  # nach der Querung (x>0)
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot._check_teleport_crossing((-2.0, 0.0, 3.0), 100.0)
    assert bot.pos_x == pytest.approx(50.0, abs=2.0)   # Sprung zum Ziel-Teleporter
    assert bot.pos_z == pytest.approx(3.0, abs=0.5)    # Z erhalten
    assert bot._teleporting_until == pytest.approx(101.0)
    sent = [c for c in bot.client.send.call_args_list if c.args[0] == MsgTeleport]
    assert sent, "MsgTeleport muss gesendet werden"
    _pid, src, dst = struct.unpack(">BHH", sent[-1].args[1])
    assert (src, dst) == (1, 2)               # t0:back → t1:front


def test_check_teleport_crossing_preserves_jump(bot):
    teles, lmap = _pair(bz1=20.0)             # t1 bottom_z=20
    _bot_world(bot, teles, lmap)
    bot.own_flag = ""
    bot._jumping = True
    state_before = bot._ai_state
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 5.0
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 10.0               # steigend
    bot._check_teleport_crossing((-2.0, 0.0, 5.0), 100.0)
    assert bot.pos_z == pytest.approx(25.0, abs=0.6)   # 20 (Exit-Boden) + 5 (rel. Höhe)
    assert bot.vel_z == pytest.approx(10.0)            # vz unverändert → Sprung läuft weiter
    assert bot._jumping is True                          # Sprung-State unangetastet
    assert bot._ai_state == state_before


def test_check_teleport_crossing_reverts_when_exit_blocked(bot):
    teles, lmap = _pair()
    blocker = BoxObstacle(cx=50.0, cy=0.0, bottom_z=0.0, angle=0.0,
                          half_w=5.0, half_d=5.0, height=10.0)
    _bot_world(bot, teles, lmap, boxes=[blocker])
    bot.own_flag = ""
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 3.0
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot._check_teleport_crossing((-2.0, 0.0, 3.0), 100.0)
    assert (bot.pos_x, bot.pos_y, bot.pos_z) == (-2.0, 0.0, 3.0)   # revert auf alte Position
    assert bot.vel_x == 0.0 and bot.vel_y == 0.0
    assert bot._teleporting_until == 0.0      # kein Teleport
    assert not [c for c in bot.client.send.call_args_list if c.args[0] == MsgTeleport]


def test_check_teleport_crossing_retrigger_guard(bot):
    teles, lmap = _pair()
    _bot_world(bot, teles, lmap)
    bot.own_flag = ""
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 3.0
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot._teleporting_until = 200.0
    bot._check_teleport_crossing((-2.0, 0.0, 3.0), 100.0)   # now < until → gesperrt
    assert (bot.pos_x, bot.pos_y, bot.pos_z) == (2.0, 0.0, 3.0)
    assert not [c for c in bot.client.send.call_args_list if c.args[0] == MsgTeleport]


def test_check_teleport_crossing_pz_skips(bot):
    teles, lmap = _pair()
    _bot_world(bot, teles, lmap)
    bot.own_flag = "PZ"                        # PhantomZone togglet zoned (P4-FLG-03), kein Sprung
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 3.0
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot._check_teleport_crossing((-2.0, 0.0, 3.0), 100.0)
    assert (bot.pos_x, bot.pos_y, bot.pos_z) == (2.0, 0.0, 3.0)


def test_update_movement_invokes_crossing_check(bot):
    """Zentraler Hook: _update_movement ruft den Crossing-Check auch dann, wenn _dispatch_movement
    früh zurückkehrt (Direktpfad/Sprung) — Teleport pathing-unabhängig."""
    teles, lmap = _pair()
    _bot_world(bot, teles, lmap)
    bot.own_flag = ""
    bot.pos_x = -2.0; bot.pos_y = 0.0; bot.pos_z = 3.0
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot._dispatch_movement = lambda dt, now, ai_tick=True: setattr(bot, "pos_x", 2.0)
    bot._update_movement(0.05, 100.0)
    assert bot.pos_x == pytest.approx(50.0, abs=2.0)   # zentral teleportiert


def test_resync_path_planned_teleport_keeps_exit_wp(bot):
    """Geplanter Teleport: Eintritts-WP liegt hinter uns → verworfen; Austritts-WP bleibt Ziel
    (kein Voll-Replan)."""
    bot.pos_x = 50.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._nav_path = [(-1.0, 0.0, 0.0), (52.0, 0.0, 0.0), (60.0, 0.0, 0.0)]
    bot.target_pos = (-1.0, 0.0)
    bot._resync_path_after_teleport(-2.0, 0.0, 50.0, 0.0)
    assert bot._nav_path[0] == (52.0, 0.0, 0.0)      # Eintritts-WP entfernt
    assert bot.target_pos == (52.0, 0.0)             # Austritts-WP ist neues Ziel


def test_resync_path_unplanned_teleport_clears(bot):
    """Ungeplanter Teleport: alle WPs liegen hinter uns → Pfad geleert (deferred Replan)."""
    bot.pos_x = 50.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._nav_path = [(-1.0, 0.0, 0.0), (-5.0, 0.0, 0.0)]
    bot.target_pos = (-1.0, 0.0)
    bot._resync_path_after_teleport(-2.0, 0.0, 50.0, 0.0)
    assert bot._nav_path == []
    assert bot.target_pos is None
    assert bot._nav_goal is None


def test_apply_obstacle_bounds_wall_slide_post(bot):
    from unittest.mock import MagicMock
    t = _tele("t", 0.0, 0.0, hw=0.5, hd=5.0, height=10.0, border=1.0)
    bot._world_map = MagicMock(); bot._world_map.boxes = []
    bot._tele_solid_boxes = teleporter_solid_boxes(t)
    bot.own_flag = ""
    bot.pos_x = -2.0; bot.pos_y = 4.5; bot.pos_z = 0.0                 # vor dem Post bei (0,4.5)
    bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0                 # fährt in den Post
    bot._apply_obstacle_bounds(0.05)
    assert bot.vel_x == pytest.approx(0.0)    # Wall-Slide: Vorwärts-Komponente gestoppt


def test_apply_obstacle_bounds_ceiling_crossbar(bot):
    from unittest.mock import MagicMock
    t = _tele("t", 0.0, 0.0, hw=0.5, hd=5.0, height=10.0, border=1.0)
    bot._world_map = MagicMock(); bot._world_map.boxes = []
    bot._tele_solid_boxes = teleporter_solid_boxes(t)
    bot._nav_graph = None
    bot.own_flag = ""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 7.5                  # Kopf knapp unter Crossbar (bottom_z=9)
    bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 5.0                  # steigend
    bot._apply_obstacle_bounds(0.05)
    assert bot.vel_z == pytest.approx(0.0)    # Decken-Stopp
    assert bot.pos_z == pytest.approx(9.0 - bot._tank_height, abs=0.01)


# ── Cross-Floor-Teleport: Bot bevorzugt das Tor statt zu springen ─────────────

def test_cross_floor_edge_exit_on_roof_layer():
    """Tor, das auf eine z=30-Plattform führt (HIX): die Portal-Kante landet auf der ROOF-Layer
    (nicht am Boden). Vor dem Fix snappte _build_teleport_edges den Exit fix auf Layer 0 → A* sah
    nie einen Tor-Weg nach z=30 und sprang stattdessen."""
    from conftest import load_map_fixture
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    nav = NavGraph(wm)
    dirs = set()
    for entry, (exit_node, _c) in nav._teleport_edges.items():
        dirs.add((round(nav.layers[entry[0]].z), round(nav.layers[exit_node[0]].z)))
    assert (0, 30) in dirs, "z=0→z=30-Tor-Kante fehlt (Bot kann nicht per Tor hoch)"
    assert (30, 0) in dirs, "z=30→z=0-Tor-Kante fehlt (reziproke HIX-Paare)"


def test_plan_path_cross_floor_uses_teleport_not_jump():
    """End-to-End (HIX): Bot am Boden nahe einem z=0-Eck-Tor, Ziel auf der z=30-Plattform. Der Pfad
    erreicht z=30 über die Tor-Kante (Aufstieg landet auf einem _tele_exit_wps), NICHT per Sprung."""
    from conftest import load_map_fixture
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    nav = NavGraph(wm)
    path = nav.plan_path(384.0, 384.0, 0.0, 388.0, 388.0, goal_z=30.0)
    assert path and path[-1][2] == pytest.approx(30.0), "kein Pfad auf die z=30-Plattform"
    ascents = [(path[i], path[i + 1]) for i in range(len(path) - 1)
               if path[i + 1][2] - path[i][2] > 1.5]
    assert ascents, "kein Aufstieg z=0→z=30 im Pfad"
    for _a, b in ascents:
        assert (round(b[0], 1), round(b[1], 1)) in nav._tele_exit_wps, \
            "Aufstieg ist ein Sprung statt ein Tor-Durchgang"


def test_advance_path_drives_through_teleport_exit_not_jump(bot):
    """Executor-Guard: ein Teleport-Exit-WP auf z=30 löst KEIN NAV_JUMP aus — der Bot fährt ihn an
    (durch das Tor), der reaktive Crossing-Check warpt ihn hoch."""
    from bot.models import AIState

    class _StubNav:
        def __init__(self, exits):
            self._tele_exit_wps = exits
            self._tele_cross_centers = {}
        def get_floor_z(self, x, y, z, overhang=0.0): return 0.0

    exit_wp = (50.0, 0.0, 30.0)
    bot._nav_graph = _StubNav({(50.0, 0.0)})
    bot.own_flag = ""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._ai_state = AIState.COMBAT
    bot._nav_path = [(0.0, 0.0, 0.0), exit_wp]   # [erreichter WP, Tor-Exit auf z=30]
    bot._advance_path()
    assert bot._ai_state == AIState.COMBAT       # kein NAV_JUMP / NAV_JUMP_ALIGN
    assert bot.target_pos == (50.0, 0.0)         # Exit-WP wird angefahren (durchs Tor)


def test_check_teleport_crossing_logs_usage(bot, caplog):
    """INFO ohne Details bei jeder Tor-Nutzung; DEBUG-Detail nur mit --debug-log-tele."""
    import logging
    teles, lmap = _pair()
    _bot_world(bot, teles, lmap)
    bot.own_flag = ""
    bot._debug_log_tele = False
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 3.0; bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    with caplog.at_level(logging.DEBUG, logger="bzbot"):
        bot._check_teleport_crossing((-2.0, 0.0, 3.0), 100.0)
    assert any("Teleporter genutzt" in r.message for r in caplog.records
               if r.levelno == logging.INFO)
    assert not any("Tele-Detail" in r.message for r in caplog.records)  # ohne Flag kein Detail

    caplog.clear()
    bot._debug_log_tele = True
    bot._teleporting_until = 0.0
    bot.pos_x = 2.0; bot.pos_y = 0.0; bot.pos_z = 3.0; bot.vel_x = 25.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    with caplog.at_level(logging.DEBUG, logger="bzbot"):
        bot._check_teleport_crossing((-2.0, 0.0, 3.0), 200.0)
    assert any("Tele-Detail" in r.message for r in caplog.records)     # mit Flag: Detail-DEBUG


def test_check_teleport_crossing_drive_through_lands_on_roof(bot):
    """Regression Fix 2 (HIX): FÄHRT der Bot am Boden (z=0) durch ein z=0→z=30-Tor, landet der
    Austritt exakt auf der Mauer-Oberkante (z=30). Vor dem Fix wertete _is_inside_obstacle diese
    bündige Lage als 'innen' → der Teleport wurde revertiert (Bot blieb auf z=0, vel=0 → 'steckt im
    Teleporter fest'). Nur der Sprung (Austritt z>30) kam durch. Jetzt wird die Fahr-Durchfahrt
    akzeptiert."""
    from conftest import load_map_fixture
    from bzflag.shot_physics import build_link_map, ray_teleporter_crossing
    from bzflag.world_map import teleporter_solid_boxes
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    lmap = build_link_map(wm.links)
    # z=0-Boden-Tor finden, dessen Link auf ein z≈30-Tor führt (Cross-Floor-Paar).
    gt = None
    for ti, t in enumerate(wm.teleporters):
        if t.bottom_z > 1.0:
            continue
        for face in (0, 1):
            tgt = lmap.get(2 * ti + face)
            if tgt is not None and wm.teleporters[tgt // 2].bottom_z > 25.0:
                gt = ti
                break
        if gt is not None:
            break
    assert gt is not None, "kein z=0→z=30-Boden-Tor in HIX gefunden (Testvoraussetzung)"

    t = wm.teleporters[gt]
    bot._world_map = wm
    bot._link_map = lmap
    bot.world_half = 400.0
    bot.own_flag = ""
    bot._tele_solid_boxes = [b for tele in wm.teleporters for b in teleporter_solid_boxes(tele)]
    c, s = math.cos(t.angle), math.sin(t.angle)
    # Segment AM BODEN (z=0, vz=0) mittenseitig durch die Feld-Ebene (von der Mitte zur Ecke).
    ox, oy, oz = t.cx - 4.0 * c, t.cy - 4.0 * s, 0.0
    nx, ny = t.cx + 4.0 * c, t.cy + 4.0 * s
    # Voraussetzung: dieses Boden-Segment quert das Feld wirklich.
    assert ray_teleporter_crossing(ox, oy, oz, nx - ox, ny - oy, 0.0, t) is not None
    bot.pos_x = nx; bot.pos_y = ny; bot.pos_z = oz
    bot.vel_x = c * 25.0; bot.vel_y = s * 25.0; bot.vel_z = 0.0
    bot._teleporting_until = 0.0
    bot._check_teleport_crossing((ox, oy, oz), 100.0)
    assert bot._teleporting_until == pytest.approx(101.0), \
        "Fahr-Durchfahrt revertiert (kein Teleport) — _is_inside_obstacle wertet z=30 als 'innen'?"
    assert bot.pos_z == pytest.approx(30.0, abs=0.6), \
        f"Fahr-Austritt nicht auf der z=30-Plattform: z={bot.pos_z:.1f}"


# ── P3-NAV-02 (NAV_TELE): direkter Endanflug in die Tor-Mitte ─────────────────

def _hix_ground_gate(wm, lmap):
    """Index eines z=0-Boden-Eck-Tors, dessen Link auf ein z≈30-Tor führt (Cross-Floor-Paar)."""
    for ti, t in enumerate(wm.teleporters):
        if t.bottom_z > 1.0:
            continue
        for face in (0, 1):
            tgt = lmap.get(2 * ti + face)
            if tgt is not None and wm.teleporters[tgt // 2].bottom_z > 25.0:
                return ti
    return None


def test_tele_cross_centers_caches_source_center():
    """Jeder Austritts-WP wird auf die Mitte des Quell-Tors abgebildet (NAV_TELE-Fahrtziel).
    Konkret HIX-Boden-Eck: mittenseitiger Austritts-WP (385.8,385.8) → Tor-Mitte (390,390)."""
    from conftest import load_map_fixture
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    nav = NavGraph(wm)
    assert nav._tele_cross_centers, "keine Tor-Mitten gecached"
    # Schlüsselmenge deckt sich mit den Austritts-WPs.
    assert set(nav._tele_cross_centers) == nav._tele_exit_wps
    # Alle Werte sind echte Teleporter-Zentren.
    centers = {(round(t.cx, 1), round(t.cy, 1)) for t in wm.teleporters}
    for cx, cy in nav._tele_cross_centers.values():
        assert (round(cx, 1), round(cy, 1)) in centers
    assert nav._tele_cross_centers.get((385.8, 385.8)) == pytest.approx((390.0, 390.0))


def test_advance_path_engages_nav_tele_at_tele_exit(bot):
    """Erreicht der Bot den Eingangs-WP und ist der nächste WP ein Tor-Austritt, wechselt
    _advance_path in NAV_TELE und zielt auf die Tor-Mitte (statt am mittenseitigen Exit-WP davor
    stehen zu bleiben)."""
    from bot.models import AIState

    class _StubNav:
        _tele_exit_wps = {(50.0, 0.0)}
        _tele_cross_centers = {(50.0, 0.0): (55.0, 0.0)}
        def get_floor_z(self, x, y, z, overhang=0.0): return 0.0

    bot._nav_graph = _StubNav()
    bot.own_flag = ""
    bot.pos_x = 50.0; bot.pos_y = 0.0; bot.pos_z = 0.0                       # ~5u vor der Tor-Mitte (55,0)
    bot._ai_state = AIState.COMBAT
    bot._nav_path = [(50.0, 0.0, 0.0), (50.0, 0.0, 30.0)]  # [erreicht, Tor-Exit z=30]
    bot._advance_path()
    assert bot._ai_state == AIState.NAV_TELE          # kein NAV_JUMP, kein bloßes WP-Anfahren
    assert bot._nav_tele_center == (55.0, 0.0)
    assert bot.target_pos == (55.0, 0.0)


def test_nav_tele_engage_guards(bot):
    """_try_engage_nav_tele engaged nur, wenn die Tor-Mitte nah genug UND nicht gesperrt ist."""
    import time as _t
    from bot.constants import NAV_TELE_ENGAGE_DIST
    from bot.models import AIState
    bot.own_flag = ""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0
    bot._ai_state = AIState.COMBAT
    # zu weit → False, kein State-Wechsel
    assert bot._try_engage_nav_tele((NAV_TELE_ENGAGE_DIST + 5.0, 0.0)) is False
    assert bot._ai_state == AIState.COMBAT
    # nah, aber Tor auf Cooldown → False
    bot._nav_tele_cooldowns[(3, 0)] = _t.monotonic() + 100.0
    assert bot._try_engage_nav_tele((3.0, 0.0)) is False
    assert bot._ai_state == AIState.COMBAT
    # nah und frei → True
    bot._nav_tele_cooldowns.clear()
    assert bot._try_engage_nav_tele((3.0, 0.0)) is True
    assert bot._ai_state == AIState.NAV_TELE


def test_nav_tele_drives_through_corner_gate(bot):
    """End-to-End (HIX): aus NAV_TELE fährt der Bot das letzte Stück direkt in die Tor-Mitte und
    QUERT das Tor (landet fahrend auf z=30) — kein Festkleben davor. Danach zurück auf Boden-State."""
    from conftest import load_map_fixture
    from bzflag.shot_physics import build_link_map
    from bzflag.world_map import teleporter_solid_boxes
    from bot.models import AIState
    wm = load_map_fixture("hix")
    if wm is None:
        pytest.skip("hix-Fixture fehlt")
    lmap = build_link_map(wm.links)
    gt = _hix_ground_gate(wm, lmap)
    assert gt is not None, "kein z=0→z=30-Boden-Tor in HIX gefunden (Testvoraussetzung)"
    t = wm.teleporters[gt]
    bot._world_map = wm
    bot._link_map = lmap
    bot._nav_graph = NavGraph(wm)
    bot.world_half = 400.0
    bot.own_flag = ""
    bot._tele_solid_boxes = [b for tele in wm.teleporters for b in teleporter_solid_boxes(tele)]
    bot._teleporting_until = 0.0
    c, s = math.cos(t.angle), math.sin(t.angle)
    # Bot mittenseitig ~5u vor der Tor-Mitte, ausgerichtet entlang der Querungsachse (zur Ecke).
    bot.pos_x = t.cx - 5.0 * c; bot.pos_y = t.cy - 5.0 * s; bot.pos_z = 0.0
    bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0
    bot.azimuth = math.atan2(s, c)
    now = 100.0
    assert bot._try_engage_nav_tele((t.cx, t.cy)) is True
    assert bot._ai_state == AIState.NAV_TELE
    dt = 1.0 / 60.0
    crossed = False
    for _ in range(120):                       # max 2s
        now += dt
        bot._update_movement(dt, now)
        if bot._teleporting_until > 0.0:
            crossed = True
            break
    assert crossed, "NAV_TELE hat das Tor nicht gequert (vor dem Tor stecken geblieben)"
    assert bot.pos_z == pytest.approx(30.0, abs=0.6), f"nicht auf z=30: {bot.pos_z:.1f}"
    now += dt
    bot._update_movement(dt, now)              # nächster Tick erkennt Querung → Boden-State
    assert bot._ai_state in (AIState.SEEKING, AIState.COMBAT, AIState.IDLE)


def test_nav_tele_timeout_aborts_and_cooldowns(bot):
    """Quert der Bot binnen NAV_TELE_TIMEOUT nicht (blockiert/Revert), bricht NAV_TELE ab: Cooldown
    aufs Tor, Pfad verworfen, zurück auf Boden-State zum Neuplanen."""
    from bot.constants import NAV_TELE_TIMEOUT
    from bot.models import AIState
    _bot_world(bot, [], {})                     # WorldMap ohne Teleporter → nie eine Querung
    bot.own_flag = ""
    bot.pos_x = 0.0; bot.pos_y = 0.0; bot.pos_z = 0.0; bot.vel_x = 0.0; bot.vel_y = 0.0; bot.vel_z = 0.0; bot.azimuth = 0.0
    bot._teleporting_until = 0.0
    bot._tele_solid_boxes = []
    bot._nav_path = [(0.0, 0.0, 0.0), (5.0, 0.0, 30.0)]
    bot._nav_goal = (5.0, 0.0)
    bot._ai_state = AIState.NAV_TELE
    bot._nav_tele_center = (5.0, 0.0)
    bot._nav_tele_return_state = AIState.SEEKING
    now = 100.0
    bot._nav_tele_start = now
    dt = 1.0 / 60.0
    for _ in range(int((NAV_TELE_TIMEOUT + 0.5) * 60)):
        now += dt
        bot._update_movement(dt, now)
        if bot._ai_state != AIState.NAV_TELE:
            break
    assert bot._ai_state == AIState.SEEKING
    assert bot._nav_path == []
    assert bot._nav_tele_cooldowns.get((5, 0), 0) > now
