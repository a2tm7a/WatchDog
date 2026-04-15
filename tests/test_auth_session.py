"""
Tests for AuthSession (Phase 2) — session refresh without live allen.in traffic.
"""

from unittest.mock import MagicMock, patch

import pytest

from auth_session import AuthSession


@pytest.fixture
def session_with_mock_page():
    mock_ctx = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_ctx.new_page.return_value = mock_page

    with patch("auth_session._load_credentials", return_value={"form_id": "x", "password": "y"}):
        s = AuthSession(mock_ctx)
    s.page = mock_page
    s._logged_in = True
    return s, mock_page


class TestEnsureSession:
    def test_relogin_when_body_contains_please_log_in(self, session_with_mock_page):
        session, mock_page = session_with_mock_page
        mock_page.url = "https://allen.in/app"
        mock_page.inner_text.return_value = "Welcome back. Please log in to continue."

        with patch.object(session, "login") as mock_login:
            session._ensure_session()

        mock_login.assert_called_once()
        assert session._logged_in is False  # cleared before login()

    def test_relogin_when_body_contains_session_expired(self, session_with_mock_page):
        session, mock_page = session_with_mock_page
        mock_page.url = "https://allen.in/dashboard"
        mock_page.inner_text.return_value = "Your session expired. Please sign in again."

        with patch.object(session, "login") as mock_login:
            session._ensure_session()

        mock_login.assert_called_once()

    def test_no_relogin_when_clean_session(self, session_with_mock_page):
        session, mock_page = session_with_mock_page
        mock_page.url = "https://allen.in/dashboard"
        mock_page.inner_text.return_value = "Course catalog"

        with patch.object(session, "login") as mock_login:
            session._ensure_session()

        mock_login.assert_not_called()

    def test_relogin_when_page_is_none(self):
        mock_ctx = MagicMock()
        with patch("auth_session._load_credentials", return_value={"form_id": "x", "password": "y"}):
            session = AuthSession(mock_ctx)
        session.page = None
        session._logged_in = True

        with patch.object(session, "login") as mock_login:
            session._ensure_session()

        mock_login.assert_called_once()
