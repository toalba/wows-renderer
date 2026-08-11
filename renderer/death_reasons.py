"""DEATH_REASON enum from battle.xml → (label, icon_frag filename).

Shared by the killfeed layer (which needs the icon) and the statistics
board (which needs only the label). Kept in one place so a patch that
shifts the enum is a single-file fix.
"""
from __future__ import annotations

DEATH_REASON: dict[int, tuple[str, str]] = {
    0: ("", ""),                                  # NONE
    1: ("ARTILLERY", "icon_frag_main_caliber"),  # ARTILLERY (generic)
    2: ("SEC", "icon_frag_atba"),                # ATBA
    3: ("TORP", "icon_frag_torpedo"),            # TORPEDO
    4: ("BOMB", "icon_frag_bomb"),               # BOMB
    5: ("TORP", "icon_frag_torpedo"),            # TBOMB (torpedo bomber)
    6: ("FIRE", "icon_frag_burning"),            # BURNING
    7: ("RAM", "icon_frag_ram"),                 # RAM
    8: ("TERRAIN", ""),                          # TERRAIN
    9: ("FLOOD", "icon_frag_flood"),             # FLOOD
    10: ("MIRROR", ""),                          # MIRROR
    11: ("MINE", "icon_frag_naval_mine"),        # SEA_MINE
    12: ("", ""),                                # SPECIAL
    13: ("DBOMB", "icon_frag_depthbomb"),        # DBOMB
    14: ("ROCKET", "icon_frag_rocket"),          # ROCKET
    15: ("DETONATE", "icon_frag_detonate"),      # DETONATE
    16: ("", ""),                                # HEALTH
    17: ("AP", "icon_frag_main_caliber"),        # AP_SHELL
    18: ("HE", "icon_frag_main_caliber"),        # HE_SHELL
    19: ("SAP", "icon_frag_main_caliber"),       # CS_SHELL
    20: ("FEL", "icon_frag_fel"),                # FEL
    21: ("PORTAL", "icon_frag_portal"),          # PORTAL
    22: ("SKIP", "icon_frag_skip"),              # SKIP_BOMB
    23: ("WAVE", "icon_frag_wave"),              # SECTOR_WAVE
    24: ("ACID", "icon_frag_acid"),              # ACID
    25: ("LASER", "icon_frag_laser"),            # LASER
    26: ("MATCH", "icon_frag_octagon"),          # MATCH
    28: ("DBOMB", "icon_frag_depthbomb"),        # ADBOMB
    35: ("MISSILE", "icon_frag_missile"),        # MISSILE
}


def death_reason_label(reason: int) -> str:
    """Short label for a death/weapon id. Empty string for unknown ids."""
    return DEATH_REASON.get(reason, ("", ""))[0]
