"""Faehigkeiten und effektive Werte: _can_*-Gates und _effective_*-Ableitungen aus Flagge + Server-Variablen (W4, FABLE-PLAN Teil 3)."""

import math

from bot.constants import (
    TANK_WIDTH,
    TANK_RADIUS_FACTOR,
    _TINY_FACTOR,
    _NARROW_HW,
    OPTIMAL_RANGE,
    OPTIMAL_RANGE_MG,
    OPTIMAL_RANGE_SW,
    OPTIMAL_RANGE_GM,
    JUMP_COOLDOWN,
    MOMENTUM_LIN_ACC_FACTOR,
    FLAG_GRAB_RADIUS,
    FLAG_GRAB_MARGIN,
)
from bot.util import _angle_diff, _wrap


from mypy_extensions import trait
from bot._bot_base import BZBotBase


@trait
class CapabilityMixin(BZBotBase):
    """Mixin für BZBot — Methoden unverändert aus bzbot_ai.py verschoben (Track 4/W4)."""

    def _effective_half_width(self) -> float:
        """Aktuelle Tank-Halbbreite je nach Flagge (T/N verkleinern den Tank)."""
        if self.own_flag == "T":
            return (TANK_WIDTH / 2.0) * _TINY_FACTOR
        if self.own_flag == "N":
            return _NARROW_HW
        return TANK_WIDTH / 2.0

    def _effective_hit_radius(self) -> float:
        """Trefferradius gemäß BZFlag: 0.99 * tankRadius * scale. N-Flagge → OBB, gibt 0.0 zurück."""
        if self.own_flag == "N":
            return 0.0  # N-Flagge: OBB-Check in _resolve_incoming_shots; diese Funktion nicht verwendet
        if self.own_flag == "O":   scale = self._obese_factor
        elif self.own_flag == "T": scale = _TINY_FACTOR
        else:                       scale = 1.0
        return self._effective_tank_radius() * scale * 0.99

    def _effective_tank_radius(self) -> float:
        """Aktueller Tank-Radius aus der nachgeführten Server-Variable (_tankRadius = 0.72 * _tankLength)."""
        return TANK_RADIUS_FACTOR * self._tank_length

    def _flag_grab_radius(self) -> float:
        """Grab-Anfahrtsradius: Parität zum bisherigen FLAG_GRAB_RADIUS auf Default-Servern,
        skaliert mit _tankLength/_flagRadius nach oben; max() → nie schlechter als heute.
        Weit unter der bzfs-Toleranz (tankSpeed+tankRadius+flagRadius, Lag-Puffer) —
        bewusst NICHT ausgereizt (menschlich-plausibel bleiben)."""
        return max(FLAG_GRAB_RADIUS,
                   self._effective_tank_radius() + self._flag_radius + FLAG_GRAB_MARGIN)

    def _effective_optimal_range(self) -> float:
        """Optimale Kampfdistanz je nach eigener Flagge UND Gegner-Flagge.
        Eingegrabener BU-Gegner ist immun gegen normale Schüsse (nur GM/SW treffen) und von
        jedem Tank überrollbar → ohne eigenes GM/SW auf Kontaktdistanz rammen (wie SR).
        MG und SW profitieren von Nahkampf: MG wegen kurzer Schuss-Reichweite,
        SW wegen Donut-Killzone (zu weit → treffen nur noch die äußere Grenze)."""
        tgt = self.players.get(self.target_player) if self.target_player is not None else None
        if (tgt is not None and tgt.flag == "BU" and tgt.pos[2] < 0.0
                and self.own_flag not in ("GM", "SW")):
            return self._effective_tank_radius() / 2 * self._sr_radius_mult   # eingegrabener Gegner (z<0): Ramm-Kontaktdistanz
        if self.own_flag == "MG":
            return OPTIMAL_RANGE_MG
        if self.own_flag == "SW":
            return OPTIMAL_RANGE_SW
        if self.own_flag == "SR":
            return self._effective_tank_radius() / 2 * self._sr_radius_mult  # Kontaktdistanz für Ramm-Kill
        if self.own_flag == "GM":
            return OPTIMAL_RANGE_GM
        return OPTIMAL_RANGE

    def _can_shoot(self) -> bool:
        """Basis-Voraussetzungen für Schuss: Netzwerk aktiv, eingeloggt, Debug-Flag."""
        if self._debug_no_shoot: return False
        if not self.client.udp_active:              return False
        if self.player_id is None:                  return False
        return True

    def _can_jump(self, now: float) -> bool:
        """Prüft alle Sprung-Voraussetzungen: physikalisch, Flagge, Cooldown, Debug-Flag."""
        if self._debug_no_jump:   return False
        if self._dodging:                             return False
        if self._jumping:
            return self._can_air_jump()
        if not self._is_landed():                     return False
        if self.own_flag in ("NJ", "BU"):            return False
        if not self._server_jumping and self.own_flag not in ("WG", "BY", "JP"):
            return False
        if now - self._last_jump_at < JUMP_COOLDOWN: return False
        return True

    def _can_air_jump(self) -> bool:
        """WG-Luftsprung (Extra-Flap) verfügbar? Bewusst OHNE das _dodging-Gate von _can_jump:
        der EVADING-Notausweg (P4-MOV-03c) läuft mitten im Dodge — _setup_dodge setzt _dodging,
        der Reset kommt erst im Boden-Pfad von _tick_committed, also wäre der Extra-Flap über
        _can_jump nie erreichbar. Kein JUMP_COOLDOWN airborne (deckungsgleich zum bisherigen
        _jumping-Zweig von _can_jump; doJump zählt airborne nur wingsFlapCount)."""
        if self._debug_no_jump:   return False
        if not self._jumping:     return False
        if self.own_flag != "WG": return False
        return self._wings_jumps_used < self._wings_jump_count - 1

    def _can_move_forward(self)  -> bool: return self.own_flag != "RO"

    def _can_move_backward(self) -> bool: return self.own_flag != "FO"

    def _can_turn_left(self)     -> bool: return self.own_flag != "RT"

    def _can_turn_right(self)    -> bool: return self.own_flag != "LT"

    def _reload_time_for_flag(self, flag: str) -> float:
        """Reload-Zeit für ein gegebenes Flag (MG/F beschleunigen). Für die eigene Flagge UND für
        Gegner-Schüsse (P4-TAC-05, Schützen-Flag aus dem Shot-Payload). Hinweis: _laser_ad_rate wird
        als Server-Var getrackt, aber — wie schon im bisherigen _effective_reload_time — bewusst NICHT
        in die Reload-Zeit einbezogen (vorbestehende Lücke, s. DEVELOPER.md)."""
        if flag == "MG":
            return self._reload_time / max(self._mgun_ad_rate, 1.0)
        if flag == "F":
            return self._reload_time / max(self._rfire_ad_rate, 1.0)
        return self._reload_time

    def _effective_reload_time(self) -> float:
        """Reload-Zeit je nach aktiver eigener Flagge."""
        return self._reload_time_for_flag(self.own_flag)

    def _enemy_slots_empty(self, info, now: float) -> bool:
        """True, wenn ALLE Schuss-Slots des Gegners im Cooldown sind (leergeschossen). Ein nie
        gesehener/fehlender Slot (len < _max_shots) gilt als geladen → False (P4-TAC-05, konservativ)."""
        slots = info.slot_reload_at
        n = max(self._max_shots, 1)
        if len(slots) < n:
            return False
        return all(t > now for t in slots[:n])

    def _enemy_next_slot_ready_in(self, info, now: float) -> float:
        """Sekunden, bis der erste Gegner-Slot wieder bereit ist (0.0, wenn bereits einer frei bzw.
        ein Slot nie gesehen wurde) — das Ausbruchs-/Peek-Fenster für COVER_HOLD (P4-TAC-05)."""
        slots = info.slot_reload_at
        n = max(self._max_shots, 1)
        if len(slots) < n:
            return 0.0
        return max(0.0, min(slots[:n]) - now)

    def _own_flag_bytes(self) -> bytes:
        """2-Byte-Wire-Encoding der eigenen Flagge (Protokoll-FlagAbbr mit
        Null-Padding; keine Flagge → b'\\x00\\x00'). F9: einzige Quelle —
        vorher 6× inline dupliziert. PZ nur wenn gezoned: der echte Client
        nullt das Flag im Schuss sonst (ShotPath.cxx:46) — das Wire-Flag „PZ"
        BEDEUTET „Schütze war beim Feuern gezoned" (P4-FLG-03)."""
        if self.own_flag == "PZ" and not self.is_phantom_zoned:
            return b'\x00\x00'
        return (self.own_flag.encode('ascii') + b'\x00\x00')[:2]

    def _effective_shot_speed(self) -> float:
        """Schussgeschwindigkeit (u/s) der aktiven Flagge (BZFlag: vel *= AdVel)."""
        f = self.own_flag
        if f == "L":  return self._shot_speed * self._laser_ad_vel
        if f == "MG": return self._shot_speed * self._mgun_ad_vel
        if f == "F":  return self._shot_speed * self._rfire_ad_vel
        if f == "TH": return self._shot_speed * self._thief_ad_shot_vel
        return self._shot_speed                       # GM + Normal: Basis-Geschwindigkeit

    def _effective_shot_lifetime(self) -> float:
        """Schuss-Lebensdauer (s) der aktiven Flagge (BZFlag: lifetime *= AdLife)."""
        f = self.own_flag
        if f == "L":  return self._shot_lifetime * self._laser_ad_life
        if f == "MG": return self._shot_lifetime * self._mgun_ad_life
        if f == "F":  return self._shot_lifetime * self._rfire_ad_life
        if f == "TH": return self._shot_lifetime * self._thief_ad_life
        if f == "GM": return self._shot_lifetime * self._gm_ad_life
        return self._shot_lifetime

    def _effective_shot_range(self) -> float:
        """Maximale Schuss-Reichweite (u) der aktiven Flagge = eff_speed · eff_lifetime."""
        return self._effective_shot_speed() * self._effective_shot_lifetime()

    def _effective_tank_speed(self) -> float:
        # M (Momentum) ist Inertie, nicht Top-Speed: die Beschleunigungs-Klemme (lin≤20·
        # _momentumLinAcc, ang≤_momentumAngAcc) steckt in _accel_limits() → _ramp_* (P4-MOV-02),
        # NICHT hier. Der Top-Speed bleibt unter M unverändert. M wird weiter nach ~shakeTimeout
        # abgeworfen (bad flag) — die Modellierung dient Protokoll-Konformität + Dodge-Korrektheit.
        if self.own_flag == "V":   return self._tank_speed * self._velocity_ad
        if self.own_flag == "TH":  return self._tank_speed * self._thief_vel_ad
        if self.own_flag == "A" and math.hypot(self.vel_x, self.vel_y) < 1.0:
            return self._tank_speed * self._agility_ad_vel
        if self.own_flag == "BU" and self.pos_z < 0.0:  # Malus nur eingegraben (am Boden), nicht auf Dächern
            return self._tank_speed * self._burrow_speed_ad
        return self._tank_speed

    def _travel_tank_speed(self) -> float:
        """Nachhaltige Vorwärts-Reisegeschwindigkeit für Sprung-Planung UND -Ausführung.

        Nur dauerhaft während der Fahrt wirkende Flaggen (V/TH); A (nur Stillstand) und BU (nur
        eingegraben) werden bewusst ignoriert — beim Sprung-Anlauf fährt der Tank (vel>1) am Boden
        (z≥0), dort liefern sie ohnehin Basisgeschwindigkeit. Stabil (keine transienten Sprünge im
        Wert) → Planer (nav.plan_path(tank_speed=…)) und reaktiver Executor (needed_hspeed,
        _nav_jump_feasible/_geometry_ok) rechnen deckungsgleich: der Bot plant keinen Sprung, den er
        dann zu langsam ausführt. Siehe _effective_tank_speed für den (transienten) Live-Wert."""
        if self.own_flag == "V":  return self._tank_speed * self._velocity_ad
        if self.own_flag == "TH": return self._tank_speed * self._thief_vel_ad
        return self._tank_speed

    def _effective_turn_rate(self) -> float:
        # M ist Inertie, nicht Drehrate — die angulare Klemme steckt in _accel_limits()/_ramp_*
        # (P4-MOV-02), nicht hier. Siehe _effective_tank_speed.
        if self.own_flag == "QT":  return self._tank_turn_rate * self._angular_ad
        if self.own_flag == "BU" and self.pos_z < 0.0:  # Malus nur eingegraben (am Boden)
            return self._tank_turn_rate * self._burrow_ang_ad
        return self._tank_turn_rate

    def _effective_gravity(self) -> float:
        # WG: Wings nutzen im Flug _wingsGravity (BZFlag LocalPlayer.cxx). _wings_gravity is None
        # → kein Server-Override → BZDB-Default ist der Ausdruck "_gravity".
        if self.own_flag == "WG" and self._wings_gravity is not None:
            return self._wings_gravity
        # LG ist neutral (sofort gedroppt) → dieser Zweig ist praktisch toter Code; Formel
        # nicht gegen BZFlag verifiziert (LG im Quellcode nicht vorhanden). Siehe BUGS LG-01.
        if self.own_flag == "LG":
            return self._gravity * (self._lg_gravity / 100.0)
        return self._gravity

    def _effective_jump_velocity(self) -> float:
        # WG: Wings springen mit _wingsJumpVelocity (BZFlag doJump). _wings_jump_velocity is None
        # → kein Server-Override → BZDB-Default ist der Ausdruck "_jumpVelocity".
        if self.own_flag == "WG" and self._wings_jump_velocity is not None:
            return self._wings_jump_velocity
        return self._jump_velocity

    def _effective_jump_height(self) -> float:
        """Maximale Höhe eines Einzelsprungs unter der aktuell wirksamen Schwerkraft/Jump-Velocity
        (WG/LG-bewusst). Eine Quelle der Wahrheit für alle Sprunghöhen-Checks."""
        v = self._effective_jump_velocity()
        return v * v / (2.0 * abs(self._effective_gravity()))

    def _wings_air_control_active(self) -> bool:
        """True, wenn WG-Luftsteuerung modelliert wird: WG getragen und _wingsSlideTime == 0
        (Slide-Physik/doSlideMotion bewusst nicht modelliert → dann faithful Ballistik wie bisher)."""
        return self.own_flag == "WG" and self._wings_slide_time <= 0.0

    def _wings_air_steer(self, dt: float, target_az: float, speed: float) -> None:
        """Ein Luft-Steuer-Tick faithful zu doUpdateMotion (Wings-Zweig): ang_vel instant Richtung
        Ziel (Klemme _effective_turn_rate, KEINE Accel-Rampe — doMomentum läuft nicht airborne),
        Halbschritt-Winkelintegration, Horizontal-vel = speed ENTLANG der neuen Blickrichtung
        (signiert: speed < 0 = Rückwärtsflug). Bewegung ist damit strikt an ±azimuth gekoppelt —
        Drehen krümmt die Flugbahn, seitliches Driften ist unmöglich (P4-MOV-03a-Invariante)."""
        speed = max(-0.5 * self._effective_tank_speed(),
                    min(speed, self._effective_tank_speed()))   # Rückwärts 0,5× wie am Boden
        diff = _angle_diff(target_az, self.azimuth)
        if not self._can_turn_left()  and diff > 0: diff = 0.0
        if not self._can_turn_right() and diff < 0: diff = 0.0
        max_turn = self._effective_turn_rate()
        self.ang_vel = math.copysign(min(abs(diff / max(dt, 1e-6)), max_turn), diff)
        angle = self.azimuth + 0.5 * dt * self.ang_vel        # Halbschritt wie der echte Client
        self.azimuth = _wrap(self.azimuth + self.ang_vel * dt)
        self.vel_x = math.cos(angle) * speed
        self.vel_y = math.sin(angle) * speed

    def _next_slot_ready(self, now: float) -> bool:
        """True wenn der nächste Slot (Zyklus-Reihenfolge) seinen Reload abgewartet hat."""
        while len(self._slot_reload_at) < self._max_shots:
            self._slot_reload_at.append(0.0)
        return now >= self._slot_reload_at[(self._shot_slot + 1) % self._max_shots]

    def _apply_movement_caps(self, speed: float, ang_vel: float):
        """Wendet Fahrt- und Drehbeschränkungen durch Flaggen an (FO/RO/LT/RT)."""
        if not self._can_move_forward():  speed   = min(0.0, speed)
        if not self._can_move_backward(): speed   = max(0.0, speed)
        if not self._can_turn_left():     ang_vel = min(0.0, ang_vel)
        if not self._can_turn_right():    ang_vel = max(0.0, ang_vel)
        return speed, ang_vel

    # ── Trägheitsmodell (P4-MOV-02a/b): Beschleunigungsgrenzen (LocalPlayer::doMomentum) ──
    # Der echte Client klemmt pro Physik-Tick die ÄNDERUNG der Vorwärtsgeschwindigkeit auf
    # 20×linearAcc und der Drehrate auf angularAcc — jeweils gegen den Vorframe-Wert (lastSpeed/
    # oldAngVel), nur am Boden (Airborne-States rufen _navigate_wp/_execute_combat_move ohnehin nie
    # auf). Die Grenzen liefert _accel_limits(): normal die -a-Server-Werte, bei getragenem M die
    # _momentumLinAcc/_momentumAngAcc. Sind beide 0.0 → Rampe aus → exaktes Alt-Verhalten.

    def _accel_limits(self):
        """Effektive Bodenfahrt-Beschleunigungsgrenzen (lin, ang). Bei getragenem M ERSETZEN
        _momentumLinAcc/_momentumAngAcc die -a-Server-Werte (ternäre Auswahl in doMomentum,
        verifiziert LocalPlayer.cxx — kein Max/keine Addition). (0.0, 0.0) = keine Begrenzung
        (weder -a noch M aktiv). M ist mit BZDB-Default 1.0/1.0 ~50× träger als -a 50 38."""
        if self.own_flag == "M":
            return (self._momentum_lin_acc, self._momentum_ang_acc)
        return (self._linear_acceleration, self._angular_acceleration)

    def _eff_linear_accel(self) -> float:
        """Effektive lineare Beschleunigungsgrenze (bereits mit MOMENTUM_LIN_ACC_FACTOR gefaltet,
        0.0 = unbegrenzt) — dieselbe Klemme wie _ramp_linear_speed. Für die Sprung-Anlauf-
        Längenplanung (P4-MOV-02c, nav.plan_path(lin_accel_eff=…))."""
        lin_acc = self._accel_limits()[0]
        if lin_acc <= 0.0:
            return 0.0
        return MOMENTUM_LIN_ACC_FACTOR * lin_acc

    def _ramp_toward(self, current: float, target: float, max_delta: float) -> float:
        """Klemmt target auf [current-max_delta, current+max_delta]."""
        if target > current + max_delta:
            return current + max_delta
        if target < current - max_delta:
            return current - max_delta
        return target

    def _ramp_linear_speed(self, target_speed: float, dt: float) -> float:
        """Klemmt die Änderung der SKALAREN Vorwärtsgeschwindigkeit auf 20×linAcc·dt.

        Der echte doMomentum klemmt die skalare Geschwindigkeit gegen den Vorframe-Wert lastSpeed;
        die Fahrtrichtung folgt dem Heading instantan (kein Vektor-Momentum). prev = (vel_x,vel_y)
        projiziert auf den bereits gedrehten Azimuth rekonstruiert genau diesen signierten Skalar
        aus dem Vektor-Zustand (ohne ein Extra-Attribut) — da die Drehung pro 60-Hz-Tick winzig ist
        (<1°), gilt prev ≈ lastSpeed. lin=0 (weder -a noch M) → unverändert."""
        lin_acc = self._accel_limits()[0]
        if lin_acc <= 0.0:
            return target_speed
        prev = self.vel_x * math.cos(self.azimuth) + self.vel_y * math.sin(self.azimuth)
        max_delta = MOMENTUM_LIN_ACC_FACTOR * lin_acc * dt
        return self._ramp_toward(prev, target_speed, max_delta)

    def _ramp_azimuth_step(self, diff: float, dt: float, max_turn_rate: float) -> None:
        """Setzt ang_vel Richtung Ziel (geklemmt auf max_turn_rate) und dreht azimuth entsprechend.

        Ersetzt das bisherige Dreh-Snippet aus _navigate_wp/_turn_toward 1:1 und ergänzt die
        angulare Beschleunigungsklemme (angAcc·dt gegen die Vorframe-ang_vel, Faktor 1 — nicht 20×
        wie linear). Der Überschwing-Cap min(…, |diff|) verhindert Drehen über das Ziel hinaus
        (erhält das Alt-Verhalten). ang=0 (weder -a noch M) → identisch zum bisherigen Verhalten.

        F3: ang_vel wird nach der Ramp-Klemme auf die tatsächlich ausgeführte Drehrate
        geschnappt — die Accel-Klemme (Turn-In) bleibt vollständig, das Settle erfolgt ohne
        Überschwingen (bewusste Abweichung: der echte Client überschwingt, wir nicht -
        P4-MOV-01-Glattheit). Ohne Angular-Limit feuert der Snap nie (dort gilt
        |target|·dt ≤ |diff| konstruktionsbedingt)."""
        target = math.copysign(min(abs(diff / max(dt, 1e-6)), max_turn_rate), diff)
        ang_acc = self._accel_limits()[1]
        if ang_acc > 0.0:
            target = self._ramp_toward(self.ang_vel, target, ang_acc * dt)
        if abs(target) * dt > abs(diff):
            target = diff / max(dt, 1e-6)
        self.ang_vel = target
        self.azimuth = _wrap(
            self.azimuth + math.copysign(min(abs(target) * dt, abs(diff)), diff))

    def _momentum_ramp_time(self, cycles: float) -> float:
        """Zeit für `cycles` volle Anfahr-Rampen (0→eff. Speed) bei aktivem linearem Limit, sonst
        0.0. cycles=1.0: einmaliges Anfahren (Dodge/NAV_TELE); MOMENTUM_TIMEOUT_CYCLES: WP-Timeout/
        Stuck (Anfahren + Kehre). Für die nachgeführte Stuck-/Timeout-Erkennung (P4-MOV-02a)."""
        lin_acc = self._accel_limits()[0]
        if lin_acc <= 0.0:
            return 0.0
        return cycles * self._effective_tank_speed() / max(
            MOMENTUM_LIN_ACC_FACTOR * lin_acc, 1e-6)

    def _has_presence(self) -> bool:
        """True, wenn mindestens ein MENSCH (Mitspieler ODER Zuschauer) anwesend ist.

        Wird bei 60 Hz mehrfach pro Tick abgefragt (u.a. _maybe_shoot) — daher nur noch ein
        Read des event-getrieben gepflegten Caches (_recompute_presence bei Add/Remove/
        Callsign-Listen-Update), kein Scan der Spielerliste mehr pro Aufruf. Ein einzelner
        bool-Attribut-Read ist unter der GIL atomar, ein Race mit dem Recv-Thread (der
        _presence schreibt) liefert also höchstens einen für einen Tick veralteten Wert."""
        return self._presence

    def _can_drive_through_obstacles(self) -> bool:
        """True wenn Bot mit aktueller Flagge durch Hindernisse fahren darf: OO — oder gezoned
        (PZ + P4-FLG-03): der gezonte Tank phast durch Gebäude; die Navigation nutzt dann den
        Direktziel-Pfad (_plan_path) statt A*. Entzont wird ausschließlich an Teleporter-Feldern
        (nie im Gebäudeinneren), ein Drop ist solange zoned gesperrt — kein Feststecken."""
        return self.own_flag == "OO" or (self.own_flag == "PZ" and self.is_phantom_zoned)

    def _has_teleporters(self) -> bool:
        """True wenn die Karte Teleporter hat (→ indirekte Schüsse auch ohne Ricochet möglich)."""
        wm = self._world_map
        return bool(wm and wm.teleporters)
