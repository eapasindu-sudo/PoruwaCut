"""Wedding event presets. The panel lists these from the engine (/api/presets), so a new or tuned
event arrives with a normal PoruwaCut update - no plugin change needed.

grid:
  auto    - beats where music plays, phrase ends where people speak / chant, time grid otherwise
  beats   - every cut on a music beat (bar starts preferred)
  phrases - cuts only where a sentence / verse ends
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class EventPreset:
    id: str
    si: str                       # Sinhala label
    en: str                       # English label
    grid: str = "auto"
    min_shot: float = 2.5         # seconds
    max_shot: float = 9.0
    switch_cost: float = 0.8      # score-seconds paid per camera change
    stay_cost: float = 2.0        # per max_shot exceeded on one camera
    # what makes a good shot
    w_sharp: float = 1.0
    w_exposure: float = 0.8
    w_shake: float = 1.2
    w_pan: float = 0.6
    w_obstruct: float = 1.5
    w_face: float = 1.0
    w_couple: float = 1.0         # the couple framed in the centre
    w_couple_id: float = 0.7      # the bride / groom recognised in the shot (face recognition)
    w_smile: float = 0.35         # smiling faces
    w_wide: float = 0.0           # dance / song items: keep on the widest camera (the whole item in view)
    shake_ref: float = 0.010      # jitter that costs 1 point
    pan_limit: float = 0.30       # frame widths / s before a move counts as a whip
    rhythm_forgive: float = 0.0   # 0..1 handheld movement that follows the music is fine
    good_level: float = 0.0
    good_slope: float = 0.3
    dead_threshold: float = -1.2
    dead_min_sec: float = 2.0
    smooth_sec: float = 0.6
    analysis_fps: float = 5.0     # frames analysed per second of footage (lower = faster)
    cam_bias: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# dance / song items: mostly the wide shot (the whole item, continuous), smooth moving (FPV-like)
# shots are fine, jerky ones are not; few reaction close-ups (faces / couple / smiles count little)
_DANCE = dict(grid="beats", min_shot=1.5, max_shot=8.0, switch_cost=0.5, stay_cost=1.0,
              w_shake=1.4, w_pan=0.1, shake_ref=0.016, pan_limit=1.5, rhythm_forgive=0.6,
              w_face=0.25, w_couple=0.3, w_couple_id=0.15, w_smile=0.1, w_wide=1.2,
              smooth_sec=0.4, dead_threshold=-1.6, dead_min_sec=1.5)
_CALM = dict(grid="phrases", analysis_fps=4.0, min_shot=3.0, max_shot=10.0, switch_cost=0.9, stay_cost=2.0,
             w_shake=1.4, w_face=1.2, w_couple=1.4)

PRESETS: dict[str, EventPreset] = {p.id: p for p in [
    EventPreset("auto", "Auto (AI)", "Auto (AI)", grid="auto", w_couple=1.2,
                rhythm_forgive=0.6),
    EventPreset("groom_welcome", "Groom Welcome", "Groom welcome", grid="auto", min_shot=1.5,
                max_shot=6.0, rhythm_forgive=0.7, w_couple=0.4, w_face=1.2, switch_cost=0.5),
    EventPreset("bride_welcome", "Bride Welcome", "Bride welcome", grid="auto", min_shot=1.5,
                max_shot=6.0, rhythm_forgive=0.7, w_couple=0.4, w_face=1.2, switch_cost=0.5),
    EventPreset("poruwa", "පෝරුව", "Poruwa ceremony", **_CALM),
    EventPreset("pahana", "පහන / කිරි කළය", "Oil lamp / milk pot", **{**_CALM, "min_shot": 2.0,
                                                                        "max_shot": 8.0}),
    EventPreset("first_dance", "First Dance", "First dance", **{**_DANCE, "min_shot": 2.0, "max_shot": 8.0,
                                                                "switch_cost": 0.6, "w_couple": 1.4,
                                                                "w_couple_id": 0.5, "w_wide": 0.8}),
    EventPreset("table_visit", "Table Visit", "Table visit", grid="auto", min_shot=2.0, max_shot=7.0,
                w_couple=0.8, w_face=1.4),
    EventPreset("surprise_dance", "Surprise Dance", "Surprise dance", **_DANCE),
    EventPreset("dance_floor", "Dance Floor", "Dance floor", **_DANCE),
    EventPreset("speeches", "Speeches", "Speeches", **{**_CALM, "w_couple": 0.3, "w_face": 1.5,
                                                      "max_shot": 14.0, "min_shot": 4.0}),
    EventPreset("church_mass", "Church Mass", "Church mass", **{**_CALM, "max_shot": 14.0,
                                                               "min_shot": 4.0, "switch_cost": 1.1}),
]}


def get(preset_id: str | None, overrides: dict | None = None) -> EventPreset:
    base = (PRESETS.get(preset_id or "auto") or PRESETS["auto"]).to_dict()
    for k, v in (overrides or {}).items():
        if v is not None and k in base:
            base[k] = v if isinstance(base[k], list) else type(base[k])(v)
    return EventPreset(**base)


def listing() -> list[dict]:
    return [{"id": p.id, "si": p.si, "en": p.en, "grid": p.grid} for p in PRESETS.values()]
