"""Resolve a target described in words ("Save button", "File > Save As") to an element.

Tier 1 scores the target window's UI Automation elements (``uia_source`` +
``scoring``). Tier 2, only when explicitly enabled, asks a vision model to pick a
numbered box. Below ``WINDOWS_MCP_RESOLVER_MIN_CONFIDENCE`` nothing is acted on and
the top candidates are returned instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from time import perf_counter, sleep
from typing import Any

from windows_mcp.resolver import config, scoring, uia_source, vision
from windows_mcp.resolver.scoring import Candidate

logger = logging.getLogger(__name__)

PATH_STEP_TIMEOUT = 2.0
PATH_POLL_INTERVAL = 0.1
SPARSE_WINDOW_ELEMENTS = 3
VISION_CONFIDENCE = 0.8
VISION_MAX_BOXES = 30


class ResolveError(ValueError):
    """The target could not be resolved confidently; nothing (more) was clicked."""


@dataclass
class Resolution:
    """The outcome of resolving one target (one step of a path)."""

    target: str
    tier: str
    best: Candidate | None
    confidence: float
    candidates: list[Candidate]
    latency_ms: float
    min_confidence: float
    window_title: str = ""
    note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.best is not None and self.confidence >= self.min_confidence

    def summary(self) -> str:
        if self.best is None:
            return f'unresolved: "{self.target}" via {self.tier} in {self.latency_ms:.0f} ms'
        return (
            f'resolved: "{self.best.name}" ({self.best.type_label}) via {self.tier} '
            f"in {self.latency_ms:.0f} ms, confidence {self.confidence:.2f}"
        )

    def top(self, count: int = 3) -> list[dict]:
        return [cand.to_dict() for cand in self.candidates[:count]]

    def candidates_text(self, count: int = 3) -> str:
        if not self.candidates:
            return "  (no candidates)"
        lines = []
        for i, cand in enumerate(self.candidates[:count], 1):
            x, y = cand.center
            state = "" if cand.enabled else ", disabled"
            lines.append(
                f'  {i}. "{cand.name}" ({cand.type_label}{state}) at [{x}, {y}] '
                f"score {cand.score:.2f}"
            )
        return "\n".join(lines)

    def refusal(self) -> str:
        reason = self.note or "no confident match"
        best = f"{self.confidence:.2f}" if self.best is not None else "none"
        return (
            f'Not clicked: no confident match for "{self.target}" '
            f"(best {best} < min {self.min_confidence:.2f}; {reason}; "
            f"tier {self.tier}, {self.latency_ms:.0f} ms). Top candidates:\n"
            f"{self.candidates_text()}\n"
            "Retry with a more specific target, control_type, nth, or Click(loc=[x, y])."
        )


class Resolver:
    """Stateless apart from the desktop it may screenshot for tier 2."""

    def __init__(self, desktop: Any = None, *, source: Any = None, env: dict | None = None):
        self.desktop = desktop
        self.source = source or uia_source
        self.env = env

    # ------------------------------------------------------------------ windows
    def _windows(self, window: str | None) -> list[uia_source.TargetWindow]:
        if window:
            root = self.source.find_window(window)
            if root is None:
                raise ResolveError(f'No visible window matches "{window}".')
        else:
            root = self.source.foreground_window()
            if root is None:
                raise ResolveError("No foreground window to search.")
        # Popups (open menus, owned dialogs) sit on top of the root; search them first.
        return self.source.popup_windows(root) + [root]

    def _collect(
        self, windows: list[uia_source.TargetWindow], secondary: bool, notes: list[str]
    ) -> list[Candidate]:
        from windows_mcp.uia import is_timeout_exception

        out: list[Candidate] = []
        for win in windows:
            try:
                out.extend(self.source.collect(win, secondary=secondary))
            except Exception as exc:
                if is_timeout_exception(exc):
                    notes.append(f'window "{win.title}" is not responding to UI Automation')
                else:
                    notes.append(f'could not read window "{win.title}": {exc}')
                logger.warning("[Resolver] collect failed for %r: %s", win.title, exc)
        return out

    def _on_screen(self, candidates: list[Candidate]) -> list[Candidate]:
        left, top, width, height = self.source.virtual_screen()
        right, bottom = left + width - 1, top + height - 1
        return [
            cand
            for cand in candidates
            if left <= cand.center[0] <= right and top <= cand.center[1] <= bottom
        ]

    # ------------------------------------------------------------------ tiers
    def _vision(
        self,
        target: str,
        root: uia_source.TargetWindow,
        ranked: list[Candidate],
        element_count: int,
        notes: list[str],
    ) -> Candidate | None:
        try:
            provider = vision.get_provider(self.env)
        except vision.VisionUnavailable as exc:
            logger.debug("[Resolver] tier 2 skipped: %s", exc)
            return None
        if self.desktop is None:
            return None
        try:
            rect = self._window_rect(root)
            if element_count < SPARSE_WINDOW_ELEMENTS or not ranked:
                boxes = vision.grid_boxes(*rect)
                regions: list[Candidate | None] = [None] * len(boxes)
            else:
                pool = ranked[:VISION_MAX_BOXES]
                boxes = [
                    vision.Box(i, c.left, c.top, c.right, c.bottom) for i, c in enumerate(pool)
                ]
                regions = list(pool)
            box = vision.pick_box(
                self.desktop,
                target,
                boxes,
                rect,
                provider=provider,
                timeout=config.vision_timeout(self.env),
            )
        except vision.VisionUnavailable as exc:
            notes.append(f"vision fallback unavailable: {exc}")
            return None
        except Exception as exc:  # never let tier 2 break tier 1 results
            notes.append(f"vision fallback failed: {exc}")
            return None
        if box is None:
            notes.append("vision model found no matching box")
            return None
        picked = regions[box.number]
        if picked is not None:
            picked.score = max(picked.score, VISION_CONFIDENCE)
            return picked
        return Candidate(
            name=f"region {box.number}",
            control_type="Region",
            left=box.left,
            top=box.top,
            right=box.right,
            bottom=box.bottom,
            score=VISION_CONFIDENCE,
            window_title=root.title,
            window_handle=root.handle,
        )

    def _window_rect(self, root: uia_source.TargetWindow) -> tuple[int, int, int, int]:
        import win32gui

        left, top, right, bottom = win32gui.GetWindowRect(root.handle)
        v_left, v_top, v_width, v_height = self.source.virtual_screen()
        return (
            max(left, v_left),
            max(top, v_top),
            min(right, v_left + v_width),
            min(bottom, v_top + v_height),
        )

    # ------------------------------------------------------------------ public
    def resolve(
        self,
        target: str,
        *,
        control_type: str | None = None,
        window: str | None = None,
        nth: int | None = None,
        prefer_types: frozenset[str] = frozenset(),
        windows: list[uia_source.TargetWindow] | None = None,
    ) -> Resolution:
        """Resolve one target (no path splitting)."""
        started = perf_counter()
        min_conf = config.min_confidence(self.env)
        query = scoring.parse_target(target, control_type)
        windows = windows if windows is not None else self._windows(window)
        root = windows[-1]
        notes: list[str] = []
        screen_height = self.source.virtual_screen()[3]

        def run(cands: list[Candidate]) -> scoring.Ranking:
            return scoring.rank(
                query,
                self._on_screen(cands),
                nth=nth,
                prefer_types=prefer_types,
                min_confidence=min_conf,
                screen_height=screen_height,
            )

        primary = self._collect(windows, False, notes)
        ranking = run(primary)
        element_count = len(primary)
        if ranking.best is None or ranking.confidence < min_conf:
            secondary = self._collect(windows, True, notes)
            element_count += len(secondary)
            ranking = run(primary + secondary)

        tier = "uia"
        best, confidence, note = ranking.best, ranking.confidence, ranking.note
        # Tier 2 only when tier 1 is not confident: a certain accessibility match must
        # never be replaced by a guess, nor cause a screenshot to leave the machine.
        if best is None or confidence < min_conf:
            picked = self._vision(target, root, ranking.candidates, element_count, notes)
            if picked is not None:
                tier, best, confidence, note = "vision", picked, picked.score, ""
        result = Resolution(
            target=target,
            tier=tier,
            best=best,
            confidence=confidence,
            candidates=ranking.candidates,
            latency_ms=(perf_counter() - started) * 1000,
            min_confidence=min_conf,
            window_title=root.title,
            note="; ".join(filter(None, [note, *notes])),
            notes=notes,
        )
        logger.info(
            "[Resolver] target=%r tier=%s latency_ms=%.1f confidence=%.3f best=%r elements=%d",
            target,
            result.tier,
            result.latency_ms,
            result.confidence,
            best.name if best else None,
            element_count,
        )
        return result

    def resolve_waiting(
        self, target: str, timeout: float, windows_fn: Callable[[], list], **kwargs: Any
    ) -> Resolution:
        """Poll :meth:`resolve` until it is confident or ``timeout`` elapses."""
        started = perf_counter()
        deadline = started + timeout
        while True:
            result = self.resolve(target, windows=windows_fn(), **kwargs)
            if result.ok or perf_counter() >= deadline:
                result.latency_ms = (perf_counter() - started) * 1000
                return result
            sleep(PATH_POLL_INTERVAL)

    def act(
        self,
        target: str,
        action: Callable[[Candidate], None],
        *,
        control_type: str | None = None,
        window: str | None = None,
        nth: int | None = None,
        prefer_types: frozenset[str] = frozenset(),
        before_first_action: Callable[[uia_source.TargetWindow], None] | None = None,
    ) -> list[Resolution]:
        """Resolve ``target`` (a single name or an ``A > B`` path) and act on each step.

        ``control_type``, ``nth`` and ``prefer_types`` apply to the last step only.
        Raises :class:`ResolveError` (with what was already clicked) on a weak match.
        """
        steps = scoring.split_path(target)
        if not steps:
            raise ResolveError("target must not be empty")
        done: list[Resolution] = []
        windows = self._windows(window)
        root = windows[-1]
        if before_first_action is not None:
            before_first_action(root)

        def current_windows() -> list[uia_source.TargetWindow]:
            found = self.source.popup_windows(root) + [root]
            foreground = self.source.foreground_window()
            if foreground is not None and all(w.handle != foreground.handle for w in found):
                found.insert(0, foreground)
            return found

        for index, step in enumerate(steps):
            last = index == len(steps) - 1
            kwargs = {
                "control_type": control_type if last else None,
                "nth": nth if last else None,
                "prefer_types": prefer_types if last else frozenset(),
            }
            if index == 0:
                result = self.resolve(step, windows=windows, **kwargs)
            else:
                result = self.resolve_waiting(step, PATH_STEP_TIMEOUT, current_windows, **kwargs)
            if not result.ok:
                prefix = ""
                if done:
                    clicked = " > ".join(f'"{r.best.name}"' for r in done if r.best)
                    prefix = f"Already clicked {clicked} (path step {index} of {len(steps)}). "
                raise ResolveError(prefix + result.refusal())
            action(result.best)
            done.append(result)
        return done
