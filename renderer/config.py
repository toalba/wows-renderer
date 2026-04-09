from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from renderer.gamedata_cache import VersionedGamedata


@dataclass
class RenderConfig:
    """Configuration for the minimap renderer."""

    # Layout
    minimap_size: int = 760
    panel_width: int = 220  # default for both panels; overridden by left/right if set
    left_panel_width: int | None = None   # None = use panel_width
    right_panel_width: int | None = None  # None = use panel_width

    # Video
    fps: int = 20
    speed: float = 10.0
    start_time: float = 0.0
    end_time: float | None = None
    codec: str = "libx264"
    crf: int = 23

    # Paths
    gamedata_path: Path = Path(".")
    versioned_gamedata: VersionedGamedata | None = None  # takes priority over gamedata_path

    # Rendering
    trail_length: float = 30.0
    team_colors: dict[int, tuple[float, float, float, float]] = field(default_factory=lambda: {
        0: (0.36, 0.90, 0.51, 1.0),  # #5DE682 (ally) — luminance-distinct for colorblind
        1: (1.00, 0.42, 0.42, 1.0),  # #FF6B6B (enemy) — colorblind-safe
    })
    self_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)  # White (own ship)
    division_color: tuple[float, float, float, float] = (1.0, 0.84, 0.0, 1.0)  # Gold yellow (division mates)

    hud_height: int = 24  # score bar above minimap

    def __post_init__(self) -> None:
        if self.minimap_size <= 0:
            raise ValueError(f"minimap_size must be > 0, got {self.minimap_size}")
        if self.panel_width < 0:
            raise ValueError(f"panel_width must be >= 0, got {self.panel_width}")
        if self.fps <= 0:
            raise ValueError(f"fps must be > 0, got {self.fps}")
        if self.speed <= 0:
            raise ValueError(f"speed must be > 0, got {self.speed}")
        if not (0 <= self.crf <= 51):
            raise ValueError(f"crf must be 0-51, got {self.crf}")
        if self.start_time < 0:
            raise ValueError(f"start_time must be >= 0, got {self.start_time}")
        if self.end_time is not None and self.end_time < 0:
            raise ValueError(f"end_time must be >= 0, got {self.end_time}")
        if self.trail_length < 0:
            raise ValueError(f"trail_length must be >= 0, got {self.trail_length}")
        if not isinstance(self.gamedata_path, Path):
            self.gamedata_path = Path(self.gamedata_path)

    @property
    def effective_gamedata_path(self) -> Path:
        """Path for file-based assets (icons, minimaps, .mo).

        Uses ``versioned_gamedata.version_dir / "data"`` if available
        (cache layout keeps the ``data/`` prefix from the git repo),
        else falls back to ``gamedata_path``.
        """
        if self.versioned_gamedata is not None:
            return self.versioned_gamedata.version_dir / "data"
        return self.gamedata_path

    @property
    def left_panel(self) -> int:
        return self.left_panel_width if self.left_panel_width is not None else self.panel_width

    @property
    def right_panel(self) -> int:
        return self.right_panel_width if self.right_panel_width is not None else self.panel_width

    @property
    def total_width(self) -> int:
        return self.left_panel + self.minimap_size + self.right_panel

    @property
    def total_height(self) -> int:
        return self.minimap_size + self.hud_height

