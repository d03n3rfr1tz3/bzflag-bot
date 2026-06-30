import sys
import os
import time
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch


def load_map_fixture(name: str):
    """Lädt einen Binary-Karten-Dump aus tests/fixtures/<name>.bin + .meta.

    Gibt None zurück wenn das Fixture fehlt (Test wird dann übersprungen).
    Fixtures erzeugen: python bzbot.py --host <server> --dump-raw tests/fixtures/<name>
    """
    base = Path(__file__).parent / "fixtures" / name
    bin_path = base.with_suffix(".bin")
    meta_path = base.with_suffix(".meta")
    if not bin_path.exists() or not meta_path.exists():
        return None
    meta = {}
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip()] = v.strip()
    world_half = float(meta.get("world_half", 200.0))
    from bzflag.world_parser import parse_world
    return parse_world(bin_path.read_bytes(), world_half=world_half, world_hash="")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "perf: Performance-/Timing-Messung (kein Assert); Lauf via 'pytest -m perf -s'")


def pytest_collection_modifyitems(config, items):
    """perf-Tests im Normallauf überspringen (heavy Loops) — nur mit '-m perf' aktiv."""
    if "perf" in (config.getoption("markexpr") or ""):
        return
    skip_perf = pytest.mark.skip(reason="perf-Test: nur mit 'pytest -m perf -s'")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip_perf)


@pytest.fixture
def bot():
    with patch("bzbot.BZFlagClient"):
        from bzbot import BZBot
        b = BZBot(host="localhost", port=5154, callsign="TestBot")
    b.client = MagicMock()
    b.client.udp_active = True
    b.player_id = 1
    b.alive = True
    b.pos = [0.0, 0.0, 0.0]
    b.vel = [0.0, 0.0, 0.0]
    b.own_flag = ""
    b.human_count = 1
    b._server_jumping = True   # Standard-Testkontext: Server erlaubt Springen (-j)
    return b


def make_player(bot, pid, pos=(50.0, 0.0, 0.0), is_human=True, flag="", alive=True):
    from bzbot import PlayerInfo
    info = PlayerInfo(callsign=f"Player{pid}", team=2, is_human=is_human)
    info.pos = list(pos)
    info.flag = flag
    info.alive = alive
    info.last_seen = time.monotonic()
    bot.players[pid] = info
    return info


def make_shot(bot, shooter_id=2, shot_id=1, pos=(200.0, 0.0, 0.0), vel=(-100.0, 0.0, 0.0),
              lifetime=3.5, flag_abbr=b"\x00\x00", is_sw=False, is_gm=False,
              is_laser=False, is_thief=False, gm_target_pid=None, fire_time=None):
    from bzbot import Shot
    ft = fire_time if fire_time is not None else time.monotonic()
    if flag_abbr == b"TH" and not is_thief:
        is_thief = True
    s = Shot(
        shooter_id=shooter_id, shot_id=shot_id,
        pos=list(pos), vel=list(vel),
        fire_time=ft, lifetime=lifetime, team=2,
        is_sw=is_sw, is_gm=is_gm, is_laser=is_laser, is_thief=is_thief,
        flag_abbr=flag_abbr,
        gm_target_pid=gm_target_pid,
    )
    with bot._shots_lock:
        bot._shots[(shooter_id, shot_id)] = s
    return s


def make_th_shot_payload(shooter_id=2, shot_id=1, pos=(1.0, 0.0, 1.025),
                         vel=(-100.0, 0.0, 0.0), team=2, lifetime=0.05):
    """Erstellt ein gültiges MsgShotBegin-Payload für einen TH-Schuss."""
    import struct
    import time as _time
    return (
        struct.pack(">f",  _time.monotonic())
        + struct.pack(">B", shooter_id)
        + struct.pack(">H", shot_id)
        + struct.pack(">fff", *pos)
        + struct.pack(">fff", *vel)
        + struct.pack(">f",  0.0)
        + struct.pack(">h",  team)
        + b"TH"
        + struct.pack(">f",  lifetime)
    )
