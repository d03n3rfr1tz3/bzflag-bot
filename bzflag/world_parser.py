"""
Parser für BZFlag 2.4 MsgGetWorld-Weltdaten.

Protokollstruktur der assemblierten MsgGetWorld-Chunks (aus WorldBuilder.cxx):

  [uint16 len=10][uint16 0x6865 WorldCodeHeader]
  [uint16 mapVersion=1]
  [uint32 uncompressedSize]
  [uint32 compressedSize]
  [zlib-Blob, compressedSize Bytes]
  [uint16 len=0][uint16 0x6564 WorldCodeEnd]

Nach zlib.decompress (aus WorldBuilder::unpack):
  5 × uint32 Manager-Counts (DynColor, TexMatrix, Material, PhysDrv, Transform)
  GroupDefinitionMgr (world + benannte Gruppen)
  TeleporterLinks
  float waterLevel
  weapons + entry zones

Unterstützt Standard-BZW-Maps (alle 5 Preamble-Counts = 0).
Gibt bei unbekanntem Format eine leere WorldMap zurück (graceful degradation).

Binärformate (big-endian IEEE 754, verifiziert gegen BZFlag-Quellen):
  WallObstacle (type 0):   pos[3]+angle+size[1]+size[2]+state  = 25 B
  BoxBuilding  (type 1):   pos[3]+angle+size[3]+state          = 29 B
  PyramidBldg  (type 2):   pos[3]+angle+size[3]+state          = 29 B
  BaseBuilding (type 3):   uint16 team + BoxBuilding            = 31 B
  Teleporter   (type 4):   nboStdString(name)+pos[3]+angle+
                           size[3]+border+horizontal+state  = var
  Mesh..Tetra  (5–9):      komplex – nicht unterstützt (count=0 erwartet)
"""

import struct
import zlib
import logging
from typing import Optional

from .world_map import BoxObstacle, TeleporterObstacle, WorldMap

logger = logging.getLogger(__name__)

# --- Protokoll-Konstanten (aus include/Protocol.h) --------------------------
_WORLD_CODE_HEADER = 0x6865   # 'he'
_WORLD_CODE_END    = 0x6564   # 'ed'
_MAP_VERSION       = 1

# Obstacle-State-Bits (aus include/global.h)
_DRIVE_THRU = 0x01
_SHOOT_THRU = 0x02
_FLIP_Z     = 0x04   # invertierte Pyramide (Spitze unten)
_RICOCHET   = 0x08   # Schüsse prallen von diesem Obstacle ab

# Obstacle-Typ-Indices in GroupDefinition (aus include/ObstacleMgr.h)
_TYPE_WALL  = 0
_TYPE_BOX   = 1
_TYPE_PYR   = 2
_TYPE_BASE  = 3
_TYPE_TELE  = 4
_TYPE_COUNT = 10   # ObstacleTypeCount


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def parse_world(data: bytes, world_half: float = 200.0,
                world_hash: str = "") -> Optional[WorldMap]:
    """
    Parst assemblierten MsgGetWorld-Puffer → WorldMap.
    Gibt None zurück wenn das Format unbekannt ist (graceful degradation).
    """
    try:
        return _parse(data, world_half, world_hash)
    except _ParseError as perr:
        # Getrennte Variablennamen pro except-Zweig: mypyc typisiert die as-Variable
        # funktionsweit mit dem ERSTEN Handler-Typ — ein gemeinsames "exc" ließe den
        # Exception-Zweig im Kompilat mit TypeError sterben statt graceful zu degradieren.
        logger.warning("[PTH] Weltparse-Fehler: %s — kein Karten-Wissen", perr)
        return None
    except Exception as exc:
        logger.warning("[PTH] Weltparse-Ausnahme: %s — kein Karten-Wissen", exc)
        return None


# ---------------------------------------------------------------------------
# Interne Implementierung
# ---------------------------------------------------------------------------

class _ParseError(Exception):
    pass


class _Reader:
    """Cursor-basierter Reader für big-endian Binärdaten."""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos  = 0

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def uint8(self) -> int:
        v = struct.unpack_from(">B", self._data, self._pos)[0]
        self._pos += 1
        return v

    def uint16(self) -> int:
        v = struct.unpack_from(">H", self._data, self._pos)[0]
        self._pos += 2
        return v

    def uint32(self) -> int:
        v = struct.unpack_from(">I", self._data, self._pos)[0]
        self._pos += 4
        return v

    def float32(self) -> float:
        v = struct.unpack_from(">f", self._data, self._pos)[0]
        self._pos += 4
        return v

    def vec3(self):
        v = struct.unpack_from(">fff", self._data, self._pos)
        self._pos += 12
        return v

    def nbo_string(self) -> str:
        """nboStdString: uint32 length + length bytes."""
        length = self.uint32()
        raw = self._data[self._pos: self._pos + length]
        self._pos += length
        return raw.decode("utf-8", errors="replace")

    def skip(self, n: int) -> None:
        self._pos += n


def _parse(data: bytes, world_half: float, world_hash: str) -> WorldMap:
    r = _Reader(data)

    # ---- Äußerer Header ---------------------------------------------------
    hdr_len  = r.uint16()
    hdr_code = r.uint16()
    if hdr_code != _WORLD_CODE_HEADER:
        raise _ParseError(f"Kein WorldCodeHeader (got 0x{hdr_code:04x})")
    if hdr_len != 10:
        raise _ParseError(f"WorldCodeHeader.len={hdr_len} erwartet 10")

    map_version        = r.uint16()
    uncompressed_size  = r.uint32()
    compressed_size    = r.uint32()

    if map_version != _MAP_VERSION:
        raise _ParseError(f"mapVersion={map_version} erwartet {_MAP_VERSION}")

    # ---- zlib-Dekompression -----------------------------------------------
    compressed_blob = data[r._pos: r._pos + compressed_size]
    if len(compressed_blob) < compressed_size:
        raise _ParseError("Zu kurz für compressedSize")
    r.skip(compressed_size)

    try:
        raw = zlib.decompress(compressed_blob)
    except zlib.error as exc:
        raise _ParseError(f"zlib: {exc}") from exc

    if len(raw) != uncompressed_size:
        raise _ParseError(
            f"Unkomprimierte Größe: erwartet {uncompressed_size}, got {len(raw)}")

    # ---- WorldCodeEnd prüfen (nach dem komprimierten Block) ---------------
    end_len  = r.uint16()
    end_code = r.uint16()
    if end_code != _WORLD_CODE_END or end_len != 0:
        raise _ParseError(f"Kein WorldCodeEnd (code=0x{end_code:04x} len={end_len})")

    # ---- Unkomprimierten Block parsen ------------------------------------
    d = _Reader(raw)
    return _parse_decompressed(d, world_half, world_hash)


def _resolve_link_face(s: str, name_to_idx: dict) -> Optional[int]:
    """
    Link-Namens-String → numerischer Face-Index (tele*2 + 0=front/1=back).
    Semantik aus LinkManager::findTelesByName: führendes ':' (absolute Links)
    strippen, Suffix ':f'/':b' (case-insensitiv) als Face, Rest = Teleporter-Name
    bzw. Auto-Name '/tN'. Liefert None wenn unauflösbar.
    ponytail: nur ':f'/':b'-Faces, keine Glob-Wildcards — reale Maps globben Links nie.
    """
    s = s.lstrip(":")
    low = s.lower()
    if low.endswith(":f"):
        face, base = 0, s[:-2]
    elif low.endswith(":b"):
        face, base = 1, s[:-2]
    else:
        return None
    if base in name_to_idx:
        idx = name_to_idx[base]
    elif base.startswith("/t") and base[2:].isdigit():
        idx = int(base[2:])
    else:
        return None
    return idx * 2 + face


def _parse_decompressed(d: _Reader, world_half: float,
                         world_hash: str) -> WorldMap:
    # Preamble: 5 Manager (DynColor, TexMatrix, Material, PhysDrv, Transform)
    # Standard-Maps: je uint32 count = 0
    preamble_names = (
        "DynamicColor", "TextureMatrix", "Material",
        "PhysicsDriver", "ObstacleTransform",
    )
    for name in preamble_names:
        count = d.uint32()
        if count != 0:
            raise _ParseError(
                f"{name}-Manager hat count={count} ≠ 0 "
                f"— Nicht-Standard-Map nicht unterstützt"
            )

    # GroupDefinitionMgr
    boxes, teleporters = _parse_group_def_mgr(d)

    # TeleporterLinks (LinkManager::unpack): uint32 count, dann pro Link zwei
    # nboStdString-Namen wie "/t0:f" / "/t1:b" (NICHT uint32-Paare!). Die Namen
    # werden zu numerischen Face-Indizes aufgelöst (face = tele*2 + 0=front/1=back).
    name_to_idx = {(t.name or f"/t{i}"): i for i, t in enumerate(teleporters)}
    link_count = d.uint32()
    links = []
    for _ in range(link_count):
        src_name = d.nbo_string()
        dst_name = d.nbo_string()
        s = _resolve_link_face(src_name, name_to_idx)
        t = _resolve_link_face(dst_name, name_to_idx)
        if s is not None and t is not None:
            links.append((s, t))
        else:
            logger.debug("[PTH] Link unauflösbar: %r → %r", src_name, dst_name)

    wm = WorldMap(
        boxes=boxes,
        teleporters=teleporters,
        links=links,
        world_half=world_half,
        world_hash=world_hash,
    )
    logger.info(
        "[PTH] Karte geparst: %d Boxen/Pyramiden, %d Teleporter, "
        "%d Links — world_half=%.0fu",
        len(boxes), len(teleporters), len(links), world_half,
    )
    return wm


def _parse_group_def_mgr(d: _Reader):
    """GroupDefinitionMgr::unpack → (boxes, teleporters)."""
    # Welt-GroupDefinition
    boxes, teleporters = _parse_group_def(d, is_world=True)

    # Benannte GroupDefinitions (für maps mit define/group — Standard: 0)
    named_count = d.uint32()
    for _ in range(named_count):
        _parse_group_def(d, is_world=False)

    return boxes, teleporters


def _parse_group_def(d: _Reader, is_world: bool):
    """GroupDefinition::unpack → (boxes, teleporters)."""
    _name = d.nbo_string()   # "" für Welt-Group, sonst Gruppenname

    boxes:       list = []
    teleporters: list = []

    for type_idx in range(_TYPE_COUNT):
        count = d.uint32()
        if count == 0:
            continue

        if type_idx == _TYPE_WALL:
            for _ in range(count):
                _skip_wall(d)

        elif type_idx == _TYPE_BOX:
            for _ in range(count):
                b = _read_box(d)
                # Nur Objekte der Welt-Ebene sammeln (keine benannten Gruppen-Inhalte)
                # drive_through-Gebäude sind für Fahrzeuge durchfahrbar → kein Kollisions-Hindernis
                if is_world and not b.drive_through:
                    boxes.append(b)

        elif type_idx == _TYPE_PYR:
            for _ in range(count):
                b = _read_pyramid(d)
                # Pyramiden werden wie Boxen behandelt (worst-case AABB)
                if is_world and not b.drive_through:
                    boxes.append(b)

        elif type_idx == _TYPE_BASE:
            for _ in range(count):
                b = _read_base(d)
                # CTF-Basen können befahrbar sein (Flaggen-Pickup-Zone)
                if is_world and not b.drive_through:
                    boxes.append(b)

        elif type_idx == _TYPE_TELE:
            for _ in range(count):
                t = _read_teleporter(d)
                # Teleporter immer sammeln, unabhängig von driveThrough
                if is_world:
                    teleporters.append(t)

        else:
            # Mesh, Arc, Cone, Sphere, Tetra — nicht unterstützt
            raise _ParseError(
                f"Obstacle-Typ {type_idx} mit count={count} nicht unterstützt "
                f"— Nicht-Standard-Map"
            )

    # GroupInstances (Standard-Maps: 0)
    gi_count = d.uint32()
    if gi_count > 0:
        raise _ParseError(
            f"GroupInstance count={gi_count} nicht unterstützt — Nicht-Standard-Map"
        )

    return boxes, teleporters


# ---------------------------------------------------------------------------
# Einzel-Obstacle-Parser (Formate aus BZFlag-Quellen verifiziert)
# ---------------------------------------------------------------------------

def _read_box(d: _Reader) -> BoxObstacle:
    """BoxBuilding::unpack — 29 Bytes."""
    cx, cy, bz = d.vec3()
    angle       = d.float32()
    hw, hd, h   = d.vec3()
    state       = d.uint8()
    return BoxObstacle(
        cx=cx, cy=cy, bottom_z=bz,
        angle=angle, half_w=hw, half_d=hd, height=h,
        drive_through=bool(state & _DRIVE_THRU),
        shoot_through=bool(state & _SHOOT_THRU),
        is_pyramid=False,
        z_flip=False,
        ricochet=bool(state & _RICOCHET),
    )


def _read_pyramid(d: _Reader) -> BoxObstacle:
    """PyramidBuilding::unpack — 29 Bytes."""
    cx, cy, bz = d.vec3()
    angle       = d.float32()
    hw, hd, h   = d.vec3()
    state       = d.uint8()
    return BoxObstacle(
        cx=cx, cy=cy, bottom_z=bz,
        angle=angle, half_w=hw, half_d=hd, height=h,
        drive_through=bool(state & _DRIVE_THRU),
        shoot_through=bool(state & _SHOOT_THRU),
        is_pyramid=True,
        z_flip=bool(state & _FLIP_Z),
        ricochet=bool(state & _RICOCHET),
    )


def _read_base(d: _Reader) -> BoxObstacle:
    """BaseBuilding::unpack — uint16 team + BoxBuilding (31 Bytes total)."""
    _team = d.uint16()
    return _read_box(d)


def _read_teleporter(d: _Reader) -> TeleporterObstacle:
    """Teleporter::unpack — nboStdString(name) + pos+angle+size+border+flags."""
    name        = d.nbo_string()
    cx, cy, bz  = d.vec3()
    angle       = d.float32()
    hw, hd, h   = d.vec3()
    border      = d.float32()
    horizontal  = bool(d.uint8())
    _state      = d.uint8()   # driveThrough/shootThrough/ricochet
    # BZFlag Teleporter::finalize(): vertikale Teleporter haben eine größere aktive
    # Fläche als die serialisierte origSize. getBreadth()=hd+2*border, getHeight()=h+border.
    # Ohne diese Korrektur ist das Querungsfeld zu schmal (HIX: ±3.36 statt ±5.60u) →
    # Schüsse durch den Randstreifen werden nicht teleportiert.
    # ponytail: horizontale Teleporter (selten) bleiben unverändert — der Querungstest
    # nimmt ohnehin eine vertikale Feld-Ebene an.
    if not horizontal:
        hd = hd + 2.0 * border
        h  = h  + border
    return TeleporterObstacle(
        name=name, cx=cx, cy=cy, bottom_z=bz,
        angle=angle, half_w=hw, half_d=hd, height=h,
        border=border, horizontal=horizontal,
    )


def _skip_wall(d: _Reader) -> None:
    """WallObstacle::unpack — pos[3]+angle+size[1]+size[2]+state = 25 Bytes."""
    d.skip(25)


