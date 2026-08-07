"""Particle-sphere orb — the assistant's visual presence.

A cloud of points distributed over a sphere, rotated in 3D and projected
to the screen. What the orb is doing reads at a glance: it drifts when
idle, leans in when listening, churns when thinking, and ripples
outward while speaking, so a reply is visible as motion before a single
word is heard.

The geometry is deterministic — the same state and clock always produce
the same frame — so the shapes it takes can be asserted in tests instead
of eyeballed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QRadialGradient
from PyQt6.QtWidgets import QWidget

from .face_widget import JarvisState, get_jarvis_state
from .themes import COLORS

# Golden angle: successive points land maximally out of phase with each
# other, which is what stops a Fibonacci sphere showing seams or clumps.
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

# Point count. Dense enough to read as a solid surface at the sizes the
# face panel uses, cheap enough to repaint at 30fps on a laptop CPU.
PARTICLE_COUNT = 620

FRAME_INTERVAL_MS = 33  # ~30fps


@dataclass(frozen=True)
class OrbMotion:
    """How the orb behaves in one state.

    Kept as data rather than branches so a state's character is legible
    in one place, and so tests can assert the contrasts between states
    (speaking moves more than idle, asleep is dimmest) without rendering.
    """

    spin: float           # radians/second about the vertical axis
    breathe: float        # amplitude of the slow in-out swell, as a fraction of radius
    ripple: float         # amplitude of the travelling wave that reads as "talking"
    ripple_speed: float   # how fast that wave crosses the surface
    turbulence: float     # per-point jitter, reads as "working on it"
    brightness: float     # overall opacity multiplier


# Every state differs from IDLE in exactly the dimensions that carry its
# meaning, so the orb never changes character for decoration's sake.
MOTION: dict = {
    JarvisState.ASLEEP: OrbMotion(0.05, 0.010, 0.0, 0.0, 0.0, 0.28),
    JarvisState.IDLE: OrbMotion(0.18, 0.028, 0.0, 0.0, 0.0, 0.70),
    JarvisState.LISTENING: OrbMotion(0.26, 0.055, 0.0, 0.0, 0.02, 0.92),
    JarvisState.THINKING: OrbMotion(0.85, 0.030, 0.0, 0.0, 0.10, 0.85),
    JarvisState.SPEAKING: OrbMotion(0.34, 0.030, 0.115, 3.1, 0.015, 1.00),
    JarvisState.DICTATING: OrbMotion(0.26, 0.070, 0.0, 0.0, 0.03, 0.95),
    JarvisState.DICTATION_PROCESSING: OrbMotion(0.85, 0.030, 0.0, 0.0, 0.10, 0.85),
}


def unit_sphere_points(count: int = PARTICLE_COUNT) -> List[Tuple[float, float, float]]:
    """Distribute ``count`` points evenly over the unit sphere.

    Fibonacci spiral: walk the sphere from pole to pole in equal steps of
    height while advancing the angle by the golden angle each time. Equal
    height steps give equal area bands, so the points come out evenly
    spread rather than bunched at the poles the way naive lat/long does.
    """
    if count < 1:
        return []
    points: List[Tuple[float, float, float]] = []
    for i in range(count):
        # y walks from +1 (north pole) to -1 (south pole).
        y = 1.0 - (2.0 * i / (count - 1)) if count > 1 else 0.0
        ring_radius = math.sqrt(max(0.0, 1.0 - y * y))
        theta = _GOLDEN_ANGLE * i
        points.append((math.cos(theta) * ring_radius, y, math.sin(theta) * ring_radius))
    return points


def _pseudo_jitter(index: int, t: float) -> float:
    """Deterministic per-point wobble in roughly [-1, 1].

    A hash-free stand-in for noise: two incommensurable sine waves, so
    neighbouring points never move in lockstep and the pattern does not
    visibly repeat. Deterministic in ``index`` and ``t`` so a frame is
    reproducible.
    """
    return math.sin(index * 12.9898 + t * 1.7) * math.cos(index * 4.1414 + t * 0.9)


def project(
    points: List[Tuple[float, float, float]],
    *,
    motion: OrbMotion,
    t: float,
    radius: float,
) -> List[Tuple[float, float, float]]:
    """Animate and flatten the sphere to ``(x, y, depth)`` screen offsets.

    ``depth`` comes back in 0..1 (0 = far side, 1 = nearest the viewer);
    the caller turns it into size and opacity so the far surface reads as
    behind rather than in front. Offsets are relative to the orb centre,
    so the caller decides where it sits.
    """
    swell = 1.0 + motion.breathe * math.sin(t * 1.9)
    spun = motion.spin * t
    cos_s, sin_s = math.cos(spun), math.sin(spun)

    out: List[Tuple[float, float, float]] = []
    for i, (x, y, z) in enumerate(points):
        r = swell

        if motion.ripple:
            # A wave travelling pole-to-pole. Displacing along the radius
            # (rather than sideways) makes the whole surface breathe out
            # in bands — the shape people read as a voice.
            r += motion.ripple * math.sin(y * 3.4 - t * motion.ripple_speed)

        if motion.turbulence:
            r += motion.turbulence * _pseudo_jitter(i, t)

        # Spin about the vertical axis: y is untouched, x/z rotate.
        rx = (x * cos_s - z * sin_s) * r
        rz = (x * sin_s + z * cos_s) * r
        ry = y * r

        depth = (rz + 1.0) / 2.0
        out.append((rx * radius, -ry * radius, max(0.0, min(1.0, depth))))
    return out


class ParticleOrbWidget(QWidget):
    """The orb as a Qt widget, following the global Jarvis state."""

    BG_COLOR = QColor(COLORS["bg_primary"])
    CORE_COLOR = QColor(COLORS["accent_secondary"])   # #fbbf24, near side
    EDGE_COLOR = QColor(COLORS["accent_muted"])       # #92400e, far side

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self._points = unit_sphere_points()
        self._elapsed = 0.0
        self._state = JarvisState.ASLEEP

        state_manager = get_jarvis_state()
        self._state = state_manager.state
        state_manager.state_changed.connect(self._on_state_changed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(FRAME_INTERVAL_MS)

    def _on_state_changed(self, state_value: str) -> None:
        try:
            self._state = JarvisState(state_value)
        except ValueError:
            self._state = JarvisState.IDLE
        self.update()

    @property
    def motion(self) -> OrbMotion:
        return MOTION.get(self._state, MOTION[JarvisState.IDLE])

    def _tick(self) -> None:
        self._elapsed += FRAME_INTERVAL_MS / 1000.0
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, self.BG_COLOR)

        cx, cy = w / 2.0, h / 2.0
        radius = min(w, h) * 0.34
        motion = self.motion

        self._draw_halo(painter, cx, cy, radius, motion)

        projected = project(
            self._points, motion=motion, t=self._elapsed, radius=radius,
        )
        # Far side first, so near points land on top of it.
        projected.sort(key=lambda p: p[2])

        for x, y, depth in projected:
            self._draw_particle(painter, cx + x, cy + y, depth, motion)

        painter.end()

    def _draw_halo(self, painter, cx, cy, radius, motion: OrbMotion) -> None:
        """Soft glow behind the sphere, so it sits in the dark rather than on it."""
        # Tight and faint: enough to seat the orb in the dark, not so much
        # that it fogs the gaps between particles into a brown wash. The
        # separation between points is what makes it read as a surface.
        glow = QRadialGradient(cx, cy, radius * 1.45)
        inner = QColor(self.CORE_COLOR)
        inner.setAlpha(int(30 * motion.brightness))
        mid = QColor(self.CORE_COLOR)
        mid.setAlpha(int(10 * motion.brightness))
        outer = QColor(self.CORE_COLOR)
        outer.setAlpha(0)
        glow.setColorAt(0.0, inner)
        glow.setColorAt(0.55, mid)
        glow.setColorAt(1.0, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(
            int(cx - radius * 1.45), int(cy - radius * 1.45),
            int(radius * 2.9), int(radius * 2.9),
        )

    def _draw_particle(self, painter, x: float, y: float, depth: float, motion: OrbMotion) -> None:
        # Depth drives size and opacity together — the two cues that make
        # a flat point cloud read as a sphere. The steep exponent keeps the
        # far hemisphere as faint speckle so the near surface stays crisp;
        # a gentler curve turns the whole thing into an even haze.
        size = 0.9 + 2.9 * (depth ** 1.3)
        alpha = int((10 + 245 * (depth ** 2.2)) * motion.brightness)
        alpha = max(0, min(255, alpha))

        colour = QColor(
            int(self.EDGE_COLOR.red() + (self.CORE_COLOR.red() - self.EDGE_COLOR.red()) * depth),
            int(self.EDGE_COLOR.green() + (self.CORE_COLOR.green() - self.EDGE_COLOR.green()) * depth),
            int(self.EDGE_COLOR.blue() + (self.CORE_COLOR.blue() - self.EDGE_COLOR.blue()) * depth),
            alpha,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(int(x - size / 2), int(y - size / 2), int(size), int(size))
