"""
auth — WatchDog authentication package.

Public interface (mirrors the old flat auth_session module):

    from auth import AuthSession
    from auth import run_profile_change_flow, PROFILE_STREAM_LABELS

Internal submodules:
    auth.login    — login UI mechanics, selectors, timing constants
    auth.profile  — /profile Change modal flow (stream / class / board)
    auth.debug    — diagnostic helpers, debug bundles, screenshots
    auth.session  — AuthSession class + credential loading
"""

from auth.login import (
    FORM_ID_FIELD_SELECTORS,
    LOGGED_IN_POSITIVE_SELECTORS,
    PASSWORD_INNER,
    POST_LOAD_LATE_POPUP_SEC,
    SESSION_EXPIRY_INDICATORS,
    _dismiss_optional_overlays,
    click_first_visible_submit_in_scope,
    click_visible_form_id_flow_button,
    fill_first_visible_in_scope,
    login_credentials_panel_locator,
    login_drawer_locator,
)
from auth.profile import (
    PROFILE_CHANGE_BUTTON,
    PROFILE_PAGE_URL,
    PROFILE_STREAM_LABELS,
    run_profile_change_flow,
)
from auth.session import AuthSession, _load_credentials

__all__ = [
    "AuthSession",
    "FORM_ID_FIELD_SELECTORS",
    "LOGGED_IN_POSITIVE_SELECTORS",
    "PASSWORD_INNER",
    "POST_LOAD_LATE_POPUP_SEC",
    "PROFILE_CHANGE_BUTTON",
    "PROFILE_PAGE_URL",
    "PROFILE_STREAM_LABELS",
    "SESSION_EXPIRY_INDICATORS",
    "_dismiss_optional_overlays",
    "_load_credentials",
    "click_first_visible_submit_in_scope",
    "click_visible_form_id_flow_button",
    "fill_first_visible_in_scope",
    "login_credentials_panel_locator",
    "login_drawer_locator",
    "run_profile_change_flow",
]
