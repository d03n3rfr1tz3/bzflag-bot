"""
Tests für _find_target_player.

RADAR_RANGE = WORLD_HALF_DEFAULT = 400u  (richtungsunabhängig)
SHOT_RANGE  = 350u  (maximale Schussweite, begrenzt FOV-Check)
TARGET_FOV  = 75°   (±37.5° um aktuelle Blickrichtung)

Da RADAR_RANGE (400u) inzwischen die volle Schussreichweite (350u) abdeckt, sind radar-sichtbare
Gegner (alles außer ST) innerhalb der Reichweite stets per Radar targetbar — ein „nur im FOV,
außerhalb Radar"-Regime gibt es nur noch jenseits von RADAR_RANGE (und damit jenseits SHOT_RANGE).

Stealth (ST): nicht auf Radar sichtbar → nur per FOV targetbar
Cloaking (CL): nicht optisch sichtbar  → nur per Radar targetbar
"""
import math
import time
import pytest
from conftest import make_player


# ── Passivmodus ───────────────────────────────────────────────────────────────

def test_passive_mode_returns_none(bot):
    """Kein Mensch anwesend (_has_presence False) → kein Ziel, auch bei zielbarem Gegner."""
    make_player(bot, pid=2, pos=(50.0, 0.0, 0.0))
    bot._has_presence = lambda: False
    result = bot._find_target_player()
    assert result is None


# ── Normaler Spieler ──────────────────────────────────────────────────────────

def test_normal_player_in_radar(bot):
    """Normaler Spieler 80u entfernt → in Radar (150u) → als Ziel gewählt."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi  # blickt nach links (weg vom Spieler)
    make_player(bot, pid=2, pos=(80.0, 0.0, 0.0))
    assert bot._find_target_player() == 2


def test_normal_player_in_fov(bot):
    """Spieler 200u entfernt, direkt voraus → in FOV und in Radar (400u) → Ziel."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0   # blickt in +x Richtung
    make_player(bot, pid=2, pos=(200.0, 0.0, 0.0))
    assert bot._find_target_player() == 2


def test_player_outside_both(bot):
    """Spieler jenseits RADAR_RANGE, genau rückwärts → weder Radar noch FOV."""
    from bzbot import RADAR_RANGE
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0   # blickt in +x
    make_player(bot, pid=2, pos=(-(RADAR_RANGE + 10.0), 0.0, 0.0))   # hinter dem Bot, außer Reichweite
    assert bot._find_target_player() is None


def test_dead_player_not_targeted(bot):
    make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), alive=False)
    assert bot._find_target_player() is None


def test_human_preferred_over_bot(bot):
    """Gleiche Distanz: Mensch wird bevorzugt (score * 0.8)."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi  # blickt weg, beide per Radar erreichbar
    make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), is_human=True,  flag="")
    make_player(bot, pid=3, pos=(50.0, 1.0, 0.0), is_human=False, flag="")
    assert bot._find_target_player() == 2


# ── Stealth (ST) ──────────────────────────────────────────────────────────────

def test_stealth_in_radar_range_not_targetable(bot):
    """ST Spieler 80u entfernt, Bot blickt weg → Radar blockiert durch ST → kein Ziel."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi   # blickt von Spieler weg → nicht in FOV
    make_player(bot, pid=2, pos=(80.0, 0.0, 0.0), flag="ST")
    assert bot._find_target_player() is None


def test_stealth_in_fov_is_targetable(bot):
    """ST Spieler 200u entfernt, direkt im FOV → optisch sichtbar → als Ziel erkannt."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0   # blickt in +x
    make_player(bot, pid=2, pos=(200.0, 0.0, 0.0), flag="ST")
    assert bot._find_target_player() == 2


def test_stealth_outside_fov_and_in_radar_not_targetable(bot):
    """ST Spieler in Radar-Reichweite, aber außerhalb FOV → nicht targetbar."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0   # blickt in +x
    # Spieler 80u hinter dem Bot
    make_player(bot, pid=2, pos=(-80.0, 0.0, 0.0), flag="ST")
    assert bot._find_target_player() is None


# ── Cloaking (CL) ─────────────────────────────────────────────────────────────

def test_cloaking_in_radar_is_targetable(bot):
    """CL Spieler 80u entfernt, Bot blickt weg → Radar sichtbar trotz CL → Ziel."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi   # blickt von Spieler weg
    make_player(bot, pid=2, pos=(80.0, 0.0, 0.0), flag="CL")
    assert bot._find_target_player() == 2


def test_cloaking_outside_radar_not_targetable(bot):
    """CL Spieler jenseits RADAR_RANGE, direkt im Blickfeld → Radar reicht nicht; CL blockiert die
    Fenster-Sicht → kein Ziel. (Das frühere 'nur im FOV, außerhalb Radar'-Regime existiert für
    radar-sichtbare Gegner nicht mehr, da RADAR_RANGE ≥ SHOT_RANGE.)"""
    from bzbot import RADAR_RANGE
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0   # blickt in +x, direkt auf den Gegner
    make_player(bot, pid=2, pos=(RADAR_RANGE + 10.0, 0.0, 0.0), flag="CL")
    assert bot._find_target_player() is None


def test_cloaking_in_both_targetable_via_radar(bot):
    """CL Spieler 80u entfernt, auch im FOV → Radar funktioniert → Ziel."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = 0.0
    make_player(bot, pid=2, pos=(80.0, 0.0, 0.0), flag="CL")
    assert bot._find_target_player() == 2


# ── Näherster Spieler wird gewählt ────────────────────────────────────────────

def test_closest_player_selected(bot):
    """Von mehreren Spielern im Radar wird der nächste gewählt."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi   # beide per Radar erreichbar
    make_player(bot, pid=2, pos=(50.0, 0.0, 0.0))    # 50u
    make_player(bot, pid=3, pos=(100.0, 0.0, 0.0))   # 100u
    assert bot._find_target_player() == 2


# ── Staleness: kein Re-Lock auf veraltete Geister (COMBAT-Freeze-Fix) ──────────

def test_stale_target_not_reacquired(bot):
    """Gegner >10s nicht wahrgenommen (CL, in Radar-Reichweite) → NICHT als Ziel gewählt.
    Sonst lockt der Bot auf einen eingefrorenen Geist, den _get_enemy_pos längst verworfen hat."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi   # blickt weg → nur Radar
    info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="CL")
    info.last_seen = time.monotonic() - 11.0   # > ENEMY_STALE_S
    assert bot._find_target_player() is None


def test_combat_drops_stale_target(bot):
    """COMBAT mit veraltetem target_player → _validate_and_find_target räumt es ab (→ SEEKING),
    statt es über die rohe info.pos sofort wieder zu re-akquirieren."""
    bot.pos     = [0.0, 0.0, 0.0]
    bot.azimuth = math.pi
    info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="CL")
    info.last_seen = time.monotonic() - 11.0
    bot.target_player = 2
    bot._validate_and_find_target()
    assert bot.target_player is None


# ── Phase 2 ──────────────────────────────────────────────────────────────────

class TestTargetRetention:
    """Schritt 3B: Gegner bleibt als Ziel auch wenn Bot 80° seitlich schaut."""

    def test_keep_target_at_80deg_off_axis(self, bot):
        """Bot az 80° weg vom Gegner → target_player bleibt gesetzt (90° Retention)."""
        bot.pos = [0.0, 0.0, 0.0]
        bot.vel = [0.0, 0.0, 0.0]
        bot.alive = True
        bot.human_count = 1
        # Gegner bei (80, 0) = Sichtweite, aber 80° seitlich
        info = make_player(bot, 99, pos=(80.0, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.is_airborne = False
        bot.target_player = 99
        # Bot schaut 80° weg → früher (37.5°) wäre target_player auf None gesetzt worden
        bot.azimuth = math.radians(80)  # 80° von Gegnerrichtung (0°) weg
        bot._next_shoot = float("inf")
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot.target_player == 99  # Ziel behalten (neu: 90° Retention)

    def test_keep_stealth_target_off_axis_after_dodge(self, bot):
        """EVADING-Regression: nach einem Dodge schaut der Bot ~60-80° vom (jetzt aus dem engen
        FoV gefallenen) Gegner weg. Auch ein ST-Gegner (kein Radar-Blip) bleibt Ziel, weil das
        Halten über die distanzbasierte Radar-Reichweite läuft, nicht über den FoV."""
        bot.pos = [0.0, 0.0, 0.0]
        bot.alive = True
        bot.human_count = 1
        info = make_player(bot, 99, pos=(80.0, 0.0, 0.0), flag="ST")
        info.last_seen = time.monotonic()   # frisch (Dodge dauert ≪ 10s)
        bot.target_player = 99
        bot.azimuth = math.radians(80)      # 80° seitlich → außerhalb ±37.5° Sicht-FoV
        bot._validate_and_find_target()
        assert bot.target_player == 99      # Ziel gehalten trotz FoV-Verlust (kein ST-Re-Lock nötig)

    def test_drop_target_beyond_180deg(self, bot):
        """Gegner hinter Bot (>90°) und außerhalb Radar → target_player = None.
        Bot muss in SEEKING sein damit _validate_and_find_target läuft."""
        from bzbot_ai import AIState
        bot.pos = [0.0, 0.0, 0.0]
        bot.vel = [0.0, 0.0, 0.0]
        bot.alive = True
        bot.human_count = 1
        from bzbot import RADAR_RANGE
        # Gegner außerhalb Radar (210u > 200u) UND außerhalb Sichtfeld (Bot schaut weg)
        info = make_player(bot, 99, pos=(RADAR_RANGE + 10, 0.0, 0.0))
        info.vel = [0.0, 0.0, 0.0]
        info.is_airborne = False
        bot.target_player = 99
        bot.azimuth = math.pi  # Bot schaut WEG vom Gegner (Gegner ist 180° hinter Bot)
        bot._ai_state = AIState.SEEKING  # Validation läuft in _tick_seeking
        bot._next_shoot = float("inf")
        bot._update_movement(0.02, time.monotonic(), ai_tick=True)
        assert bot.target_player is None


# ── Sichtbarkeits-Prädikate (Radar / Fenster) ─────────────────────────────────

def _block_los(bot):
    """Setzt einen NavGraph mit einer hohen Box auf der Linie Bot(0,0)→(50,0)."""
    from bzflag.nav_graph import NavGraph
    from bzflag.world_map import BoxObstacle, WorldMap
    box = BoxObstacle(cx=25.0, cy=0.0, bottom_z=0.0, angle=0.0,
                      half_w=4.0, half_d=4.0, height=10.0)
    wm = WorldMap(boxes=[box], teleporters=[], links=[], world_half=100.0, world_hash="los")
    bot._nav_graph = NavGraph(wm, max_jump_h=18.4)


class TestVisibilityPredicates:
    def test_normal_visible_both(self, bot):
        info = make_player(bot, pid=2, flag="")
        assert bot._enemy_visible_radar(info) is True
        assert bot._enemy_visible_window(info) is True

    def test_stealth_not_on_radar(self, bot):
        info = make_player(bot, pid=2, flag="ST")
        assert bot._enemy_visible_radar(info) is False
        assert bot._enemy_visible_window(info) is True

    def test_cloak_not_in_window(self, bot):
        info = make_player(bot, pid=2, flag="CL")
        assert bot._enemy_visible_radar(info) is True
        assert bot._enemy_visible_window(info) is False

    def test_seer_sees_all(self, bot):
        bot.own_flag = "SE"
        for f in ("ST", "CL", "MQ"):
            info = make_player(bot, pid=2, flag=f)
            assert bot._enemy_visible_radar(info) is True
            assert bot._enemy_visible_window(info) is True

    def test_jamming_kills_radar(self, bot):
        bot.own_flag = "JM"
        info = make_player(bot, pid=2, flag="")
        assert bot._enemy_visible_radar(info) is False
        assert bot._enemy_visible_window(info) is True

    def test_blind_kills_window(self, bot):
        bot.own_flag = "B"
        info = make_player(bot, pid=2, flag="")
        assert bot._enemy_visible_radar(info) is True
        assert bot._enemy_visible_window(info) is False


# ── _should_update_player: Fenster-Sicht, ST-LoS, Radar-Aufmerksamkeit ─────────

class TestShouldUpdatePlayer:
    def test_stealth_in_window_updates(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
        info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="ST")
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, time.monotonic()) is True

    def test_stealth_occluded_no_update(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
        _block_los(bot)
        info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="ST")
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, time.monotonic()) is False

    def test_stealth_out_of_fov_no_update(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = math.pi   # weggedreht
        info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="ST")
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, time.monotonic()) is False

    def test_jamming_normal_no_radar(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = math.pi; bot.own_flag = "JM"
        info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="")
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, time.monotonic()) is False

    def test_radar_attention_thresholds(self, bot, monkeypatch):
        import bzbot_ai
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = math.pi   # Normal außer FoV → Radar-Pfad
        normal = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="")
        cl     = make_player(bot, pid=3, pos=(50.0, 0.0, 0.0), flag="CL")
        now = time.monotonic()
        monkeypatch.setattr(bzbot_ai.random, "random", lambda: 0.9)   # hoher Wert → Update
        normal.radar_blind_until = 0.0; cl.radar_blind_until = 0.0
        assert bot._should_update_player(normal, 50.0, 0.0, 0.0, now) is True
        assert bot._should_update_player(cl,     50.0, 0.0, 0.0, now) is True
        monkeypatch.setattr(bzbot_ai.random, "random", lambda: 0.5)   # zwischen 0.33 und 0.66
        normal.radar_blind_until = 0.0; cl.radar_blind_until = 0.0
        assert bot._should_update_player(normal, 50.0, 0.0, 0.0, now) is True    # < 0.33 → Update
        assert bot._should_update_player(cl,     50.0, 0.0, 0.0, now) is False   # < 0.66 → Skip

    def test_radar_cooldown_blocks_until_expiry(self, bot, monkeypatch):
        import bzbot_ai
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = math.pi
        info = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="")
        now = time.monotonic()
        monkeypatch.setattr(bzbot_ai.random, "random", lambda: 0.1)   # niedrig → Skip + Cooldown
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, now) is False
        assert info.radar_blind_until > now
        monkeypatch.setattr(bzbot_ai.random, "random", lambda: 0.9)   # guter Würfel...
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, now + 0.1) is False  # ...aber Cooldown
        assert bot._should_update_player(info, 50.0, 0.0, 0.0, now + 0.6) is True   # nach Ablauf


# ── ST-Akquise braucht LoS ────────────────────────────────────────────────────

class TestStealthAcquisitionLoS:
    def test_stealth_in_fov_clear_targetable(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
        make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="ST")
        assert bot._find_target_player() == 2

    def test_stealth_in_fov_occluded_not_targetable(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = 0.0
        _block_los(bot)
        make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="ST")
        assert bot._find_target_player() is None


# ── Schuss verrät Schützen-Position ───────────────────────────────────────────

class TestShotRevealsShooter:
    def test_shot_visible_predicates(self, bot):
        normal = make_player(bot, pid=2, flag="")
        ib     = make_player(bot, pid=3, flag="IB")
        cs     = make_player(bot, pid=4, flag="CS")
        assert bot._shot_visible_radar(normal) is True
        assert bot._shot_visible_radar(ib)     is False   # IB nicht auf Radar
        assert bot._shot_visible_radar(cs)     is True    # CS auf Radar sichtbar
        assert bot._shot_visible_window(cs)    is False   # CS out-the-window unsichtbar
        assert bot._shot_visible_window(ib)    is True

    def test_normal_shot_reveals_regardless_of_los(self, bot):
        bot.pos = [0.0, 0.0, 0.0]; bot.azimuth = math.pi   # weggedreht
        _block_los(bot)
        sh = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="")
        assert bot._shot_reveals_shooter(sh, 50.0, 0.0, 1.0) is True   # Radar-Schuss

    def test_ib_shot_only_reveals_in_window(self, bot):
        bot.pos = [0.0, 0.0, 0.0]
        sh = make_player(bot, pid=2, pos=(50.0, 0.0, 0.0), flag="IB")
        bot.azimuth = 0.0
        assert bot._shot_reveals_shooter(sh, 50.0, 0.0, 1.0) is True    # im FoV, frei
        bot.azimuth = math.pi
        assert bot._shot_reveals_shooter(sh, 50.0, 0.0, 1.0) is False   # IB außer FoV → kein Reveal

    def test_shot_begin_jumps_occluded_stealth_shooter(self, bot):
        import struct
        bot.pos = [0.0, 0.0, 0.0]; bot.player_id = 1; bot.azimuth = math.pi
        sh = make_player(bot, pid=7, pos=(10.0, 10.0, 0.0), flag="ST")   # alte Position
        sh.radar_blind_until = time.monotonic() + 5.0
        payload = (struct.pack(">f", time.monotonic()) + struct.pack(">B", 7)
                   + struct.pack(">H", 1)
                   + struct.pack(">fff", 90.0, 5.0, 1.0) + struct.pack(">fff", -100.0, 0.0, 0.0)
                   + struct.pack(">f", 0.0) + struct.pack(">h", 2) + b"\x00\x00"
                   + struct.pack(">f", 3.5))
        bot._on_shot_begin(0, payload)
        assert sh.pos[0] == 90.0 and sh.pos[1] == 5.0          # auf Schuss-Ursprung gesprungen
        assert sh.radar_blind_until == 0.0                     # Cooldown freigegeben
