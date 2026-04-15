"""
auth.profile — profile change flow for allen.in.

Drives the /profile → Change modal sequence:
  stream → class → board (Classes610 only) → Save

Public API:
  run_profile_change_flow(page, stream) — called by AuthSession.switch_profile()

Constants consumed by scraper and discover_auth_selectors:
  PROFILE_PAGE_URL, PROFILE_STREAM_LABELS, PROFILE_CHANGE_BUTTON
"""

import logging
import os
import re
import time
from typing import Optional

from playwright.sync_api import Locator, Page

from auth.debug import (
    _excerpt_one_line,
    _log_profile_change_context,
    _popup_is_change_your_preference,
    _write_profile_debug_bundle,
)
from auth.login import _dismiss_optional_overlays, _goto_spa_no_networkidle

# ---------------------------------------------------------------------------
# Selectors / constants
# ---------------------------------------------------------------------------

PROFILE_PAGE_URL = "https://allen.in/profile"

# Exact pill labels in the *Change your preference* modal (stream row).
PROFILE_STREAM_LABELS: dict[str, str] = {
    "JEE": "JEE",
    "NEET": "NEET",
    "Classes610": "Class 6-10",
}

# Entry control for the profile editor (tune via discover on /profile).
PROFILE_CHANGE_BUTTON = (
    "button:has-text('Change'), a:has-text('Change'), [role='button']:has-text('Change')"
)

_PREF_MODAL_TITLE = "Change your preference"


def _profile_change_dialog_budget_ms() -> int:
    return int(os.environ.get("WATCHDOG_PROFILE_DIALOG_MS", "25000"))


def _pref_modal_title_visible(page: Page, timeout_ms: int = 2_000) -> bool:
    """True when the 'Change your preference' heading is rendered on screen."""
    try:
        return page.get_by_text(_PREF_MODAL_TITLE, exact=False).first.is_visible(
            timeout=timeout_ms
        )
    except Exception:
        return False


def _active_profile_dialog(page: Page) -> Locator:
    """
    Return a Locator that scopes to the *Change your preference* modal.

    allen.in renders the modal as a portal wrapper ``div[role="dialog"]`` that
    Playwright does **not** consider "visible" (zero-size transparent container),
    even though its children — the pill rows — are clearly on screen.

    Strategy (three tiers, polled):

    1. ``[role="dialog"]`` / ``[role="alertdialog"]`` with a Playwright-visible
       wrapper — ideal; works when the wrapper has dimensions.
    2. ``[role="dialog"]`` wrapper whose **content** ("Change your preference"
       title) is visible — the allen.in portal case.
    3. ``body`` fallback — title is visible on page but no dialog role found.
    """
    budget_ms = _profile_change_dialog_budget_ms()
    deadline = time.time() + budget_ms / 1000.0
    last_err: Optional[Exception] = None

    while time.time() < deadline:
        # ── Tier 1: role-based + wrapper itself visible ──────────────────────
        for sel in ('[role="dialog"]', '[role="alertdialog"]'):
            try:
                d = page.locator(sel)
                if d.count() == 0:
                    continue
                tail = d.last
                tail.wait_for(state="visible", timeout=2_000)
                logging.debug("[AUTH][profile] dialog found via %r (visible wrapper)", sel)
                return tail
            except Exception as exc:
                last_err = exc
                continue

        # ── Tier 2: portal wrapper not visible but content is ────────────────
        # allen.in uses a transparent <div role="dialog"> as a React-portal
        # mount point; its CSS makes Playwright consider it hidden.  Accept it
        # when the modal TITLE is visible anywhere inside the wrapper.
        for sel in ('[role="dialog"]', '[role="alertdialog"]'):
            try:
                d = page.locator(sel)
                if d.count() == 0:
                    continue
                tail = d.last
                title_in_dialog = tail.get_by_text(_PREF_MODAL_TITLE, exact=False)
                if (
                    title_in_dialog.count() > 0
                    and title_in_dialog.first.is_visible(timeout=1_000)
                ):
                    logging.debug(
                        "[AUTH][profile] dialog found via %r (portal: title visible inside)",
                        sel,
                    )
                    return tail
            except Exception as exc:
                last_err = exc
                continue

        # ── Tier 3: title visible on page, no dialog role ────────────────────
        try:
            if _pref_modal_title_visible(page, timeout_ms=1_000):
                logging.debug(
                    "[AUTH][profile] dialog not found by role — using body as scope "
                    "(title '%s' is visible on page)",
                    _PREF_MODAL_TITLE,
                )
                return page.locator("body")
        except Exception as exc:
            last_err = exc

        time.sleep(0.2)

    raise RuntimeError(
        f"[AUTH] No visible profile Change layer within {budget_ms}ms: {last_err!r}"
    )


def _open_profile_change_modal(page: Page) -> None:
    """
    Click the profile **Change** control. Prefer ``main`` so we do not hit a
    duplicate Change elsewhere on the page.
    """
    groups = (
        page.locator("main").get_by_role("button", name=re.compile(r"^\s*Change\s*$", re.I)),
        page.locator("main").locator("a").filter(has_text=re.compile(r"^\s*Change\s*$", re.I)),
        page.locator(PROFILE_CHANGE_BUTTON),
    )
    for g in groups:
        try:
            n = g.count()
        except Exception:
            n = 0
        for i in range(min(n, 16)):
            cell = g.nth(i)
            try:
                if not cell.is_visible(timeout=1_200):
                    continue
                cell.scroll_into_view_if_needed(timeout=5_000)
                cell.click(timeout=12_000)
                time.sleep(0.6)
                return
            except Exception:
                continue
    raise RuntimeError("[AUTH] No visible Change control on profile page.")


def _click_preference_modal_pill(popup: Locator, label: str) -> None:
    """
    Click a pill in the *Change your preference* modal by exact text match.

    Tries three paths in order:
      1. ``get_by_text(exact=True)`` scoped to the dialog
      2. ``role=radio`` scoped to the dialog
      3. Page-level ``get_by_text(exact=True)`` (when dialog scope is body)

    Set ``WATCHDOG_PROFILE_DEBUG=1`` for verbose logs + ``reports/profile-debug-*``
    on failure.
    """
    from auth.debug import _locator_page  # local import avoids circular reference at module level

    pg = _locator_page(popup)
    opt_ms = int(os.environ.get("WATCHDOG_PROFILE_OPTION_MS", "15000"))
    errors: list[str] = []

    # 1. Exact text inside dialog scope
    try:
        hit = popup.get_by_text(label, exact=True).first
        hit.wait_for(state="visible", timeout=opt_ms)
        hit.scroll_into_view_if_needed(timeout=5_000)
        hit.click(timeout=min(10_000, opt_ms))
        logging.debug("[AUTH][profile] pill click ok (exact text) label=%r", label)
        return
    except Exception as exc:
        errors.append(f"exact_text:{exc!r}")

    # 2. Radio role inside dialog scope
    try:
        popup.get_by_role(
            "radio", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
        ).first.click(timeout=min(10_000, opt_ms))
        logging.debug("[AUTH][profile] pill click ok (radio role) label=%r", label)
        return
    except Exception as exc:
        errors.append(f"radio:{exc!r}")

    # 3. Page-level fallback (dialog scope is body or portal wrapper)
    if pg is not None:
        try:
            pg.get_by_text(label, exact=True).first.click(timeout=min(10_000, opt_ms))
            logging.debug("[AUTH][profile] pill click ok (page-level) label=%r", label)
            return
        except Exception as exc:
            errors.append(f"page_level:{exc!r}")

    merged = "; ".join(errors)
    if pg is not None:
        _log_profile_change_context(
            pg, popup, phase="pill_failed", label=label, exc=RuntimeError(merged)
        )
        _write_profile_debug_bundle(
            pg, popup,
            f"pill-fail-{re.sub(r'[^a-zA-Z0-9_-]+', '_', label)[:40]}",
            RuntimeError(merged),
        )
    raise RuntimeError(f"[AUTH] Could not click preference pill {label!r} ({merged})")


def _select_stream_in_change_flow(page: Page, stream: str) -> None:
    """Click the stream pill in the *Change your preference* modal."""
    label = PROFILE_STREAM_LABELS[stream]
    popup: Optional[Locator] = None
    try:
        popup = _active_profile_dialog(page)
        _click_preference_modal_pill(popup, label)
    except Exception as exc:
        try:
            tail = _active_profile_dialog(page)
            excerpt = _excerpt_one_line(tail.inner_text(timeout=5_000) or "", 900)
            logging.error(
                "[AUTH][profile] stream_select failed stream=%r label=%r "
                "dialog=%r exc=%r",
                stream, label, excerpt, exc,
            )
        except Exception:
            logging.error(
                "[AUTH][profile] stream_select failed stream=%r label=%r "
                "exc=%r (dialog text unavailable)",
                stream, label, exc,
            )
        _write_profile_debug_bundle(page, popup, f"stream-fail-{stream}", exc)
        raise RuntimeError(
            f"[AUTH] Could not select stream {stream!r} (label={label!r}): {exc!r}"
        ) from exc


def _wait_for_board_pills_after_class_change(page: Page, board_fragment: str) -> None:
    """
    After picking **Class**, the *Board* row is already visible on the
    *Change your preference* modal (allen.in renders all three rows at once).
    This function waits until a pill matching ``board_fragment`` is visible so
    we do not click a stale node.
    """
    frag = board_fragment.strip()
    if not frag:
        return
    settle_s = float(os.environ.get("WATCHDOG_PROFILE_AFTER_CLASS_S", "0.35"))
    time.sleep(settle_s)
    budget_ms = int(os.environ.get("WATCHDOG_PROFILE_BOARD_READY_MS", "8000"))
    deadline = time.time() + budget_ms / 1000.0
    pattern = re.compile(re.escape(frag), re.I)
    while time.time() < deadline:
        try:
            popup = _active_profile_dialog(page)
            if not _popup_is_change_your_preference(popup):
                return
            try:
                pill = popup.get_by_text(frag, exact=True).first
                if pill.is_visible(timeout=500):
                    return
            except Exception:
                pass
            cell = popup.locator(
                "button, a, div[role='button'], span, [role='radio'], label"
            ).filter(has_text=pattern).first
            if cell.is_visible(timeout=500):
                return
        except Exception:
            pass
        time.sleep(0.1)
    logging.warning(
        "[AUTH][profile] Board pill %r may not be visible after class change; "
        "continuing — increase WATCHDOG_PROFILE_BOARD_READY_MS if clicks miss.",
        frag,
    )


def _click_profile_wizard_save(page: Page) -> None:
    popup = _active_profile_dialog(page)
    for name in ("Save", "Update", "Confirm", "Apply", "Submit", "Done", "Continue"):
        try:
            b = popup.get_by_role(
                "button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
            )
            if b.count() == 0:
                continue
            cell = b.first
            if not cell.is_visible(timeout=800):
                continue
            cell.click(timeout=5_000)
            return
        except Exception:
            continue
    try:
        popup.locator("button[type='submit']").first.click(timeout=5_000)
        return
    except Exception:
        pass
    raise RuntimeError("[AUTH] Could not find Save/Confirm on profile Change dialog.")


def _wait_for_class_pills_after_stream_change(page: Page, class_fragment: str) -> None:
    """
    After choosing **Stream**, the *Class* row is rebuilt (6–10 vs 11/12, etc.).
    Wait until a matching class pill exists so we do not click stale DOM.
    """
    frag = class_fragment.strip()
    if not frag:
        return
    settle_s = float(os.environ.get("WATCHDOG_PROFILE_AFTER_STREAM_S", "0.55"))
    time.sleep(settle_s)
    try:
        popup = _active_profile_dialog(page)
    except Exception:
        return
    if not _popup_is_change_your_preference(popup):
        return
    budget_ms = int(os.environ.get("WATCHDOG_PROFILE_CLASS_READY_MS", "12000"))
    deadline = time.time() + budget_ms / 1000.0
    pattern = re.compile(re.escape(frag), re.I)
    while time.time() < deadline:
        try:
            popup = _active_profile_dialog(page)
            if not _popup_is_change_your_preference(popup):
                return
            try:
                pill = popup.get_by_text(frag, exact=True).first
                if pill.is_visible(timeout=600):
                    return
            except Exception:
                pass
            cell = popup.locator(
                "button, a, div[role='button'], span, [role='radio'], label"
            ).filter(has_text=pattern).first
            if cell.is_visible(timeout=600):
                return
        except Exception:
            pass
        time.sleep(0.12)
    logging.warning(
        "[AUTH] Class pills may not have appeared after stream change (fragment=%r); "
        "continuing — increase WATCHDOG_PROFILE_CLASS_READY_MS if clicks miss.",
        frag,
    )


def run_profile_change_flow(page: Page, stream: str) -> None:
    """
    Run the *Change your preference* modal flow **in strict order**:

        1. Navigate to ``https://allen.in/profile``
        2. Click **Change**
        3. Select **Stream** pill  ← modal opens with current stream pre-selected
        4. Wait for **Class** row to reflect the new stream
        5. Select **Class** pill (``WATCHDOG_PROFILE_CLASS``, required for all streams)
        6. Wait for **Board** row (Classes 6-10 only)
        7. Select **Board** pill (Classes 6-10 only; ``WATCHDOG_PROFILE_BOARD``
           or **CBSE** by default)
        8. Click **Save** — only after all selections are confirmed

    Board is **only** selected when ``stream == "Classes610"``, matching the UI
    rule stated by the user: "board only if stream is Class 6-10."

    Env vars:
        WATCHDOG_PROFILE_CLASS           Text of the Class pill (e.g. "11th", "8th")
        WATCHDOG_PROFILE_BOARD           Board pill text; default "CBSE" for Classes610
        WATCHDOG_PROFILE_AFTER_STREAM_S  Settle pause after stream click (default 0.55 s)
        WATCHDOG_PROFILE_CLASS_READY_MS  Poll budget for class pill after stream (default 12000)
        WATCHDOG_PROFILE_AFTER_CLASS_S   Settle pause after class click (default 0.35 s)
        WATCHDOG_PROFILE_BOARD_READY_MS  Poll budget for board pill after class (default 8000)
    """
    if stream not in PROFILE_STREAM_LABELS:
        raise ValueError(
            f"Unknown stream '{stream}'. Valid options: {list(PROFILE_STREAM_LABELS)}"
        )

    # ── Step 1 + 2: navigate to /profile and open Change modal ──────────────
    logging.debug("[AUTH][profile] Starting change flow: stream=%s", stream)
    _goto_spa_no_networkidle(page, PROFILE_PAGE_URL)
    time.sleep(0.3)
    _dismiss_optional_overlays(page)
    _open_profile_change_modal(page)

    # ── Step 3: select stream ────────────────────────────────────────────────
    logging.debug("[AUTH][profile] Step 3 — selecting stream: %s", stream)
    _select_stream_in_change_flow(page, stream)
    logging.debug("[AUTH][profile] Stream selected: %s", stream)

    # ── Step 4 + 5: wait for class pills, then select class ──────────────────
    cls = os.environ.get("WATCHDOG_PROFILE_CLASS", "").strip()
    if cls:
        logging.debug("[AUTH][profile] Step 4 — waiting for class pills...")
        _wait_for_class_pills_after_stream_change(page, cls)
        logging.debug("[AUTH][profile] Step 5 — selecting class: %s", cls)
        _click_preference_modal_pill(_active_profile_dialog(page), cls)
        logging.debug("[AUTH][profile] Class selected: %s", cls)
    else:
        logging.warning(
            "[AUTH][profile] WATCHDOG_PROFILE_CLASS not set — skipping class selection"
        )
        time.sleep(0.35)

    # ── Step 6 + 7: board (Classes610 only) ─────────────────────────────────
    if stream == "Classes610":
        brd = os.environ.get("WATCHDOG_PROFILE_BOARD", "CBSE").strip()
        logging.debug("[AUTH][profile] Step 6 — waiting for board pills (board=%s)...", brd)
        _wait_for_board_pills_after_class_change(page, brd)
        logging.debug("[AUTH][profile] Step 7 — selecting board: %s", brd)
        try:
            _click_preference_modal_pill(_active_profile_dialog(page), brd)
            logging.debug("[AUTH][profile] Board selected: %s", brd)
        except Exception as exc:
            logging.warning(
                "[AUTH][profile] Board selection %r failed (continuing to Save): %s",
                brd, exc,
            )
    else:
        logging.debug(
            "[AUTH][profile] Board step skipped (only applies to Classes610, got %s)",
            stream,
        )

    # ── Step 8: Save ─────────────────────────────────────────────────────────
    logging.debug("[AUTH][profile] Step 8 — clicking Save")
    _click_profile_wizard_save(page)
    try:
        page.wait_for_load_state("load", timeout=30_000)
    except Exception:
        pass
    time.sleep(0.4)
    logging.debug(
        "[AUTH][profile] Change flow complete: stream=%s class=%r board=%r url=%s",
        stream,
        cls or "(not set)",
        os.environ.get("WATCHDOG_PROFILE_BOARD", "CBSE") if stream == "Classes610" else "(N/A)",
        page.url,
    )
