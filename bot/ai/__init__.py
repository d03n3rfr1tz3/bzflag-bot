"""KI-Schicht des Bots: Mixin-Module, aus denen sich BZBotAI zusammensetzt (Track 4/W4).

Die Methoden sind disjunkt auf die Mixins verteilt (jede Methode lebt in genau
einem Modul) — die MRO-Reihenfolge ist daher verhaltensneutral und folgt der
Ziel-Struktur aus FABLE-PLAN Teil 3.
"""

from bot.ai.capabilities import CapabilityMixin
from bot.ai.physics import PhysicsMixin
from bot.ai.states import StateMachineMixin
from bot.ai.combat import CombatMixin
from bot.ai.navigation import NavigationMixin
from bot.ai.perception import PerceptionMixin
from bot.ai.targeting import TargetingMixin
from bot.ai.tactics import TacticsMixin
from bot.ai.shooting import ShootingMixin


class BZBotAI(CapabilityMixin, PhysicsMixin, StateMachineMixin, CombatMixin,
              NavigationMixin, PerceptionMixin, TargetingMixin, TacticsMixin,
              ShootingMixin):
    """Mixin: Bewegungs- und KI-Logik für BZBot.

    Erlaubte Übergänge:
        DEAD         → IDLE / SEEKING      (Spawn-Event, via bzbot.py)
        IDLE         → SEEKING             (_has_presence: Mensch oder Observer da)
        IDLE         → EVADING             (_handle_threat: Bedrohung erkannt)
        IDLE         → DODGE_JUMP          (_handle_threat: Dodge nicht machbar)
        SEEKING      → IDLE                (not _has_presence: kein Mensch, kein Observer)
        SEEKING      → COMBAT              (Ziel vorhanden)
        SEEKING      → EVADING             (_handle_threat: Bedrohung erkannt)
        SEEKING      → DODGE_JUMP          (_handle_threat: Dodge nicht machbar)
        COMBAT       → SEEKING             (Ziel verloren)
        COMBAT       → EVADING             (_handle_threat: Bedrohung erkannt)
        COMBAT       → JUMP_WINDUP         (taktischer Übersprung, Wind-Up)
        COMBAT       → DODGE_JUMP          (_handle_threat: Dodge nicht machbar)
        COMBAT       → LANDING_SHOT        (Gegner springt, Fenster offen)
        COMBAT       → Z_ATTACK            (_check_z_attack_jump: Höhenangriff)
        EVADING      → COMBAT / SEEKING / IDLE  (Schuss vorbei oder dodge_until abgelaufen)
        JUMP_WINDUP  → JUMPING             (Wind-Up abgelaufen → _execute_jump)
        JUMPING      → COMBAT / SEEKING / IDLE  (_is_landed())
        Z_ATTACK     → COMBAT              (_is_landed() — immer COMBAT)
        DODGE_JUMP   → COMBAT / SEEKING / IDLE  (_is_landed())
        LANDING_SHOT → COMBAT              (Schuss abgefeuert / Fenster zu)
        LANDING_SHOT → EVADING             (Bedrohung von anderem Gegner)
        NAV_JUMP     → ANY                 (_is_landed() → _nav_jump_return_state)
        NAV_JUMP_ALIGN → NAV_JUMP          (Azimuth ≤5° ausgerichtet → _initiate_nav_jump)
        NAV_JUMP_ALIGN → ANY               (Timeout 5s → return_state + replan)
        ANY          → NAV_JUMP            (_advance_path: nächster WP auf anderer Etage)
        ANY          → NAV_JUMP_ALIGN      (_advance_path: Geometrie OK, Azimuth zu weit)
        ANY          → NAV_TELE            (_advance_path: Eingangs-WP erreicht, nächster WP = Tor-Austritt)
        NAV_TELE     → ANY                 (Querung ausgeführt, oder Timeout/Revert → Replan)
        ANY          → JUMPING             (BY-Flag-Bounce, via _run_physics)
        ANY          → DEAD                (Tod-Event, via bzbot.py)
    """
