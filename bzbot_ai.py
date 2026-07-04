"""Kompat-Shim (Track 4/W4): Die KI-Logik lebt jetzt im Paket bot/.

Konstanten → bot/constants.py, AIState → bot/models.py, Winkel-Helfer →
bot/util.py, BZBotAI + Mixins → bot/ai/. Dieses Modul hält nur noch den
alten Namespace für bestehende Importe (bzbot.py + Tests) stabil und
entfällt mit der Test-Migration (W12).
"""

import random  # noqa: F401 — Tests patchen das globale random-Modul über bzbot_ai.random

from bot.constants import *  # noqa: F401,F403
from bot.util import _angle_diff, _wrap  # noqa: F401
from bot.models import AIState  # noqa: F401
from bot.ai import (BZBotAI, CapabilityMixin, PhysicsMixin, StateMachineMixin,  # noqa: F401
                    CombatMixin, NavigationMixin, PerceptionMixin,
                    TargetingMixin, TacticsMixin, ShootingMixin)
