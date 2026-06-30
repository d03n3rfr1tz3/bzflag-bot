"""
Tests für bzflag/world_parser.py und bzflag/world_map.py.

Synthetische Binärdaten werden gemäß BZFlag 2.4-Protokoll (WorldBuilder.cxx,
ObstacleMgr.cxx) aufgebaut und durch parse_world() geparst.
"""

import math
import struct
import zlib
import pytest

from bzflag.world_parser import parse_world
from bzflag.world_map import BoxObstacle, TeleporterObstacle, WorldMap


# ---------------------------------------------------------------------------
# Hilfsfunktionen zum Aufbauen synthetischer Weltdaten
# ---------------------------------------------------------------------------

def _pack_uint8(v: int)   -> bytes: return struct.pack(">B", v)
def _pack_uint16(v: int)  -> bytes: return struct.pack(">H", v)
def _pack_uint32(v: int)  -> bytes: return struct.pack(">I", v)
def _pack_float(v: float) -> bytes: return struct.pack(">f", v)
def _pack_vec3(x, y, z)   -> bytes: return struct.pack(">fff", x, y, z)
def _pack_nbo_string(s: str) -> bytes:
    enc = s.encode("utf-8")
    return _pack_uint32(len(enc)) + enc


def _pack_box(cx, cy, cz, angle, hw, hd, h, state=0) -> bytes:
    return (_pack_vec3(cx, cy, cz) + _pack_float(angle)
            + _pack_vec3(hw, hd, h) + _pack_uint8(state))


def _pack_pyramid(cx, cy, cz, angle, hw, hd, h, state=0) -> bytes:
    return _pack_box(cx, cy, cz, angle, hw, hd, h, state)


def _pack_base(team: int, cx, cy, cz, angle, hw, hd, h, state=0) -> bytes:
    return _pack_uint16(team) + _pack_box(cx, cy, cz, angle, hw, hd, h, state)


def _pack_teleporter(name, cx, cy, cz, angle, hw, hd, h, border, horizontal=0, state=0) -> bytes:
    return (_pack_nbo_string(name)
            + _pack_vec3(cx, cy, cz) + _pack_float(angle)
            + _pack_vec3(hw, hd, h) + _pack_float(border)
            + _pack_uint8(horizontal) + _pack_uint8(state))


def _pack_wall(cx, cy, cz, angle, breadth, height, state=0) -> bytes:
    """WallObstacle: pos[3] + angle + size[1] + size[2] + state = 25 Bytes."""
    return (_pack_vec3(cx, cy, cz) + _pack_float(angle)
            + _pack_float(breadth) + _pack_float(height) + _pack_uint8(state))


def _build_world_data(
    walls=None, boxes=None, pyramids=None, bases=None, teleporters=None,
    links=None,
) -> bytes:
    """
    Baut vollständige MsgGetWorld-Weltdaten auf (Header + zlib + End).
    Preamble (5 Manager) = je uint32(0).
    """
    walls       = walls       or []
    boxes       = boxes       or []
    pyramids    = pyramids    or []
    bases       = bases       or []
    teleporters = teleporters or []
    links       = links       or []

    # Decompressed content
    decompressed = b""

    # Preamble: 5 × uint32(0)
    decompressed += _pack_uint32(0) * 5

    # GroupDefinitionMgr: world GroupDefinition
    # name "" → uint32(0)
    decompressed += _pack_nbo_string("")

    obstacle_types = [walls, boxes, pyramids, bases, teleporters]
    packs = [_pack_wall, _pack_box, _pack_pyramid, None, _pack_teleporter]

    # Type 0 (wall)
    decompressed += _pack_uint32(len(walls))
    for w in walls:
        decompressed += _pack_wall(*w)

    # Type 1 (box)
    decompressed += _pack_uint32(len(boxes))
    for b in boxes:
        decompressed += _pack_box(*b)

    # Type 2 (pyramid)
    decompressed += _pack_uint32(len(pyramids))
    for p in pyramids:
        decompressed += _pack_pyramid(*p)

    # Type 3 (base)
    decompressed += _pack_uint32(len(bases))
    for b in bases:
        decompressed += _pack_base(*b)

    # Type 4 (teleporter)
    decompressed += _pack_uint32(len(teleporters))
    for t in teleporters:
        decompressed += _pack_teleporter(*t)

    # Types 5–9 (mesh..tetra): count=0
    for _ in range(5):
        decompressed += _pack_uint32(0)

    # GroupInstances: 0
    decompressed += _pack_uint32(0)

    # Named groups: 0
    decompressed += _pack_uint32(0)

    # TeleporterLinks (LinkManager::unpack): nboStdString-Paare, Namen "/tN:f|b".
    # link-Endpunkte dürfen int (Face-Index → Auto-Name) oder roher String sein.
    def _link_name(v):
        if isinstance(v, str):
            return v
        return f"/t{v // 2}:{'f' if v % 2 == 0 else 'b'}"
    decompressed += _pack_uint32(len(links))
    for src, dst in links:
        decompressed += _pack_nbo_string(_link_name(src))
        decompressed += _pack_nbo_string(_link_name(dst))

    # waterLevel
    decompressed += _pack_float(-1.0)

    # weapons + entry zones: 0 each
    decompressed += _pack_uint32(0) + _pack_uint32(0)

    # zlib compress
    compressed = zlib.compress(decompressed)

    # Outer header
    header_payload = (
        _pack_uint16(1)                   # mapVersion
        + _pack_uint32(len(decompressed)) # uncompressedSize
        + _pack_uint32(len(compressed))   # compressedSize
    )
    outer = (
        _pack_uint16(len(header_payload)) # len = 10
        + _pack_uint16(0x6865)            # WorldCodeHeader
        + header_payload
        + compressed
        + _pack_uint16(0)                 # WorldCodeEnd len = 0
        + _pack_uint16(0x6564)            # WorldCodeEnd code
    )
    return outer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParseEmpty:
    def test_empty_world_returns_worldmap(self):
        data = _build_world_data()
        wm = parse_world(data, world_half=200.0, world_hash="test")
        assert wm is not None
        assert isinstance(wm, WorldMap)
        assert wm.boxes == []
        assert wm.teleporters == []
        assert wm.links == []
        assert wm.world_half == 200.0
        assert wm.world_hash == "test"

    def test_invalid_header_returns_none(self):
        wm = parse_world(b"\x00" * 20)
        assert wm is None

    def test_empty_bytes_returns_none(self):
        wm = parse_world(b"")
        assert wm is None

    def test_wrong_header_code_returns_none(self):
        bad = _pack_uint16(10) + _pack_uint16(0xDEAD) + b"\x00" * 10
        wm = parse_world(bad)
        assert wm is None


class TestParseBoxes:
    def test_single_box(self):
        data = _build_world_data(boxes=[
            (50.0, 30.0, 0.0, 0.0, 10.0, 5.0, 8.0, 0)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.boxes) == 1
        b = wm.boxes[0]
        assert b.cx == pytest.approx(50.0, abs=0.01)
        assert b.cy == pytest.approx(30.0, abs=0.01)
        assert b.bottom_z == pytest.approx(0.0, abs=0.01)
        assert b.half_w  == pytest.approx(10.0, abs=0.01)
        assert b.half_d  == pytest.approx(5.0,  abs=0.01)
        assert b.height  == pytest.approx(8.0,  abs=0.01)
        assert b.angle   == pytest.approx(0.0,  abs=0.01)
        assert not b.drive_through
        assert not b.shoot_through

    def test_drive_through_box_excluded(self):
        DRIVE_THRU = 0x01
        data = _build_world_data(boxes=[
            (0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0, DRIVE_THRU)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert wm.boxes == []   # drive-through Boxes werden nicht ins Pathfinding aufgenommen

    def test_shoot_through_box_included(self):
        SHOOT_THRU = 0x02
        data = _build_world_data(boxes=[
            (0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0, SHOOT_THRU)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.boxes) == 1
        assert wm.boxes[0].shoot_through

    def test_multiple_boxes(self):
        data = _build_world_data(boxes=[
            (10.0, 0.0, 0.0, 0.0, 5.0, 5.0, 10.0, 0),
            (-20.0, 15.0, 0.0, math.pi / 4, 8.0, 4.0, 6.0, 0),
        ])
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.boxes) == 2
        assert wm.boxes[1].angle == pytest.approx(math.pi / 4, abs=0.001)

    def test_rotated_box_angle_preserved(self):
        angle = math.radians(37.5)
        data = _build_world_data(boxes=[
            (0.0, 0.0, 0.0, angle, 6.0, 3.0, 5.0, 0)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert wm.boxes[0].angle == pytest.approx(angle, abs=0.001)


class TestParsePyramids:
    def test_pyramid_treated_as_box(self):
        data = _build_world_data(pyramids=[
            (0.0, 0.0, 0.0, 0.0, 7.0, 7.0, 15.0, 0)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.boxes) == 1  # Pyramiden landen in boxes[]
        b = wm.boxes[0]
        assert b.half_w == pytest.approx(7.0, abs=0.01)
        assert b.height == pytest.approx(15.0, abs=0.01)

    def test_drive_through_pyramid_excluded(self):
        data = _build_world_data(pyramids=[
            (0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 0x01)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert wm.boxes == []


class TestParseBases:
    def test_base_treated_as_box(self):
        data = _build_world_data(bases=[
            (1, 100.0, 100.0, 0.0, 0.0, 15.0, 15.0, 1.0, 0)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.boxes) == 1
        b = wm.boxes[0]
        assert b.cx == pytest.approx(100.0, abs=0.01)


class TestParseTeleporters:
    def test_single_teleporter(self):
        data = _build_world_data(teleporters=[
            ("tp1", 0.0, 50.0, 0.0, 0.0, 1.0, 4.75, 9.5, 0.9, 0, 0)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.teleporters) == 1
        t = wm.teleporters[0]
        assert t.name   == "tp1"
        assert t.cx     == pytest.approx(0.0,  abs=0.01)
        assert t.cy     == pytest.approx(50.0, abs=0.01)
        # Parser repliziert Teleporter::finalize() (vertikal): half_d=origSize+2*border,
        # height=origSize+border. Serialisiert wurden 4.75/9.5/border=0.9.
        assert t.half_d == pytest.approx(4.75 + 2 * 0.9, abs=0.01)   # 6.55
        assert t.height == pytest.approx(9.5 + 0.9,      abs=0.01)   # 10.40
        assert t.border == pytest.approx(0.9,  abs=0.01)
        assert not t.horizontal

    def test_teleporter_link(self):
        data = _build_world_data(
            teleporters=[
                ("tp0", 0.0, 0.0, 0.0, 0.0, 1.0, 4.75, 9.5, 0.9),
                ("tp1", 50.0, 0.0, 0.0, 0.0, 1.0, 4.75, 9.5, 0.9),
            ],
            links=[(0, 3), (1, 2)],
        )
        wm = parse_world(data)
        assert wm is not None
        assert len(wm.links) == 2
        assert wm.links[0] == (0, 3)
        assert wm.links[1] == (1, 2)

    def test_empty_teleporter_name(self):
        data = _build_world_data(teleporters=[
            ("", 10.0, 10.0, 0.0, 0.0, 1.0, 4.75, 9.5, 0.9)
        ])
        wm = parse_world(data)
        assert wm is not None
        assert wm.teleporters[0].name == ""


class TestParseWalls:
    def test_walls_not_in_boxes(self):
        data = _build_world_data(walls=[
            (0.0, 200.0, 0.0, 0.0, 400.0, 50.0, 0),  # Nordwand
            (0.0, -200.0, 0.0, 0.0, 400.0, 50.0, 0), # Südwand
        ])
        wm = parse_world(data)
        assert wm is not None
        assert wm.boxes == []      # Wände erscheinen nicht in boxes[]


class TestParseMixed:
    def test_mixed_obstacles(self):
        data = _build_world_data(
            boxes=[
                (50.0, 50.0, 0.0, 0.0, 10.0, 10.0, 8.0),
                (-30.0, 20.0, 0.0, 0.5, 5.0, 7.0, 12.0),
            ],
            pyramids=[
                (0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 10.0),
            ],
            teleporters=[
                ("main", 100.0, 0.0, 0.0, 0.0, 1.0, 4.75, 9.5, 0.9),
            ],
            links=[(0, 1)],
        )
        wm = parse_world(data, world_half=200.0, world_hash="abc123")
        assert wm is not None
        assert len(wm.boxes) == 3        # 2 boxes + 1 pyramid
        assert len(wm.teleporters) == 1
        assert len(wm.links) == 1
        assert wm.world_hash == "abc123"

    def test_graceful_degradation_on_corrupt_data(self):
        data = _build_world_data(boxes=[(10.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0)])
        # Daten kürzen → parse-Fehler
        wm = parse_world(data[:-50])
        assert wm is None


class TestWorldMapVersion:
    def test_wrong_map_version_returns_none(self):
        data = _build_world_data()
        # mapVersion-Byte auf 99 setzen (offset 4+2 = 6 in outer packet = Byte 4 nach Framing)
        # [uint16 len=10][uint16 0x6865] → bytes 0-3; mapVersion → bytes 4-5
        ba = bytearray(data)
        struct.pack_into(">H", ba, 4, 99)
        wm = parse_world(bytes(ba))
        assert wm is None
