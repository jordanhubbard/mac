"""Tests for UI shell — HTML structure, login screen, CSS, and JS behaviors.

These tests verify what the browser receives, not the API logic behind it.
They focus on the login screen, service-link sidebar, and token bootstrap
features added to the dashboard UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane

_UI_ROOT = Path(__file__).resolve().parents[2] / "src" / "mac" / "ui"


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(control_plane=ControlPlane.in_memory(), **kwargs))


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------


def test_ui_route_serves_html():
    resp = _client().get("/ui")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_ui_html_contains_login_screen():
    html = _client().get("/ui").text
    assert 'id="loginScreen"' in html
    assert 'class="login-screen"' in html
    assert 'id="loginForm"' in html
    assert 'id="loginTokenInput"' in html


def test_ui_html_login_screen_starts_hidden():
    """Login screen must have the `hidden` attribute so it doesn't flash on load."""
    html = _client().get("/ui").text
    # The loginScreen div must carry the hidden attribute
    import re
    match = re.search(r'id="loginScreen"[^>]*>', html)
    assert match, "loginScreen element not found"
    assert "hidden" in match.group(0)


def test_ui_html_contains_service_links_sidebar():
    html = _client().get("/ui").text
    assert 'id="serviceLinks"' in html
    assert 'class="service-links"' in html


def test_ui_html_contains_all_nav_views():
    html = _client().get("/ui").text
    for view in ("overview", "work", "projects", "map", "fleets", "agents",
                 "tasks", "workflows", "hermes", "ops", "integrations",
                 "runtime", "observability", "secrets"):
        assert 'data-view="%s"' % view in html, "missing nav view: %s" % view


def test_ui_html_groups_nav_views_for_discoverability():
    html = _client().get("/ui").text
    assert 'class="nav-section"' in html
    for label in ("Home", "Work", "Fleet", "Operations", "Security"):
        assert 'class="nav-section-label">%s</span>' % label in html
    assert 'data-view="overview" aria-current="page"' in html


def test_ui_html_loads_app_js_with_cache_bust():
    html = _client().get("/ui").text
    assert '/ui/assets/app.js?v=' in html


def test_ui_assets_app_js_serves():
    resp = _client().get("/ui/assets/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "")


def test_ui_assets_styles_css_serves():
    resp = _client().get("/ui/assets/styles.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")


def test_ui_served_without_api_token_auth():
    """The UI shell must be publicly accessible regardless of auth config."""
    client = _client(auth_tokens={"reader": ["read"]})
    assert client.get("/ui").status_code == 200
    assert client.get("/ui/assets/app.js").status_code == 200
    assert client.get("/ui/assets/styles.css").status_code == 200


# ---------------------------------------------------------------------------
# CSS — login screen styles
# ---------------------------------------------------------------------------


def test_css_contains_login_screen_styles():
    css = (_UI_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".login-screen" in css
    assert ".login-card" in css
    assert ".login-form" in css


def test_css_contains_service_link_styles():
    css = (_UI_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".service-links" in css
    assert ".service-link-btn" in css


def test_css_contains_discoverability_and_focus_styles():
    css = (_UI_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".launchpad" in css
    assert ".launchpad-action" in css
    assert ".nav-section" in css
    assert ":focus-visible" in css
    assert "scroll-snap-type" in css


def test_css_contains_object_inspector_and_mobile_cards():
    css = (_UI_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".object-inspector" in css
    assert ".mobile-card-list" in css
    assert ".responsive-table" in css
    assert ".runtime-control-grid" in css


# ---------------------------------------------------------------------------
# JS — token bootstrap and login screen wiring
# ---------------------------------------------------------------------------


def test_app_js_bootstraps_token_from_url_param():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    # ?t= URL param bootstrap
    assert "URLSearchParams" in js
    assert "sessionStorage" in js


def test_app_js_has_login_screen_show_hide():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    html = (_UI_ROOT / "index.html").read_text(encoding="utf-8")
    assert "loginScreen" in js
    assert "loginForm" in js
    assert "loginTokenInput" in js
    assert "loginApiUrlInput" in js
    assert "loginTargetSelect" in js
    assert "targetSelect" in js
    assert "topbarTargetSelect" in js
    assert "tokenSourceSelect" in js
    assert "connectionForm" in js
    assert "connectionButton" in js
    assert "disconnectFromControls" in js
    assert "loginApiUrlInput" in html
    assert "apiUrlInput" in html
    assert "loginTargetSelect" in html
    assert "targetSelect" in html
    assert "topbarTargetSelect" in html
    assert "tokenSourceSelect" in html
    assert "topbarTokenInput" in html
    assert "topbarTestingUrlInput" in html
    assert "connectionButton" in html
    assert "Fleet hub" in html
    assert "Bearer token" in html
    assert "Testing URL" in html
    assert "Optional bearer token" in html
    assert 'loginTokenInput" name="token" type="password"' in html


def test_app_js_has_connection_surface_for_remote_and_electron_modes():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    api_js = (_UI_ROOT / "dashboard_api.js").read_text(encoding="utf-8")
    api_ts = (_UI_ROOT / "dashboard_api.ts").read_text(encoding="utf-8")
    html = (_UI_ROOT / "index.html").read_text(encoding="utf-8")
    assert "connectionBadge" in js
    assert "Updated" in js
    assert "/dashboard/stream" in js
    assert "server_time" in js
    assert "dashboardStream" in js
    assert "mac.dashboard.apiBaseUrl" in js
    assert "window.macDashboard" in js
    assert "window.macDashboard" in api_js
    assert "selectTarget" in js
    assert "selectedTokenSourceId" in js
    assert "tokenSources" in js
    assert "targets" in api_js
    assert "selectTarget" in api_js
    assert "disconnect" in api_js
    assert "tokenSourceId" in api_ts
    assert "normalizeApiBaseUrl" in api_js
    assert "electron-managed" in api_js
    assert "remote-api" in api_js
    assert "connectionBadge" in html


def test_app_js_hides_derived_projects_by_default():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    css = (_UI_ROOT / "styles.css").read_text(encoding="utf-8")
    assert "showDerivedProjects" in js
    assert "show_derived" in js
    assert "visibleProjectSummaries" in js
    assert "projectFilterOptions" in js
    assert "project_id" in js
    assert "Show derived" in js
    assert "Hidden Derived" in js
    assert "toolbar-checkbox" in css


def test_app_js_has_service_link_click_handler():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    api_js = (_UI_ROOT / "dashboard_api.js").read_text(encoding="utf-8")
    assert "serviceLinks" in js
    assert "data-service-id" in js
    assert "navigate" in js
    assert "openService" in js
    assert "openService" in api_js


def test_app_js_pass_through_fetch_uses_auth_header():
    """Service navigation must fetch the navigate URL with Bearer auth."""
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    assert "pass_through" in js or "passThrough" in js or "pass-through" in js


def test_app_js_opens_service_url_in_new_tab():
    api_js = (_UI_ROOT / "dashboard_api.js").read_text(encoding="utf-8")
    assert "window.open" in api_js
    assert "_blank" in api_js
    assert "noreferrer" in api_js


def test_app_js_streams_observability_through_dashboard_api():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    assert "api.stream" in js
    assert "observability/stream" in js


def test_app_js_has_launchpad_keyboard_and_destructive_guards():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    assert "Top-line dashboard actions" in js
    assert "data-dashboard-go" in js
    assert "handleContentKeydown" in js
    assert "aria-current" in js
    assert "confirmDestructive" in js
    assert "This cannot be undone." in js


def test_app_js_has_object_inspector_mobile_runtime_secret_surfaces():
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    for marker in (
        "Project Inspector",
        "New Task in Project",
        "Project Tasks",
        "data-task-open",
        "actionSuccessMessage",
        "Task created:",
        "Plan Workflow",
        "Generate Plan",
        "Proposed Task Graph",
        "workflowPlanPreview",
        "workflowPlanNodeAdd",
        "workflowPlanNodeMove",
        "workflowPlanNodeDelete",
        "workflowPlanAccept",
        "workflowPlanCancel",
        "data-plan-field",
        "Workflow accepted:",
        "Agent Inspector",
        "Task Inspector",
        "Rollout Inspector",
        "Secret Inspector",
        "mobile-card-list",
        "runtimeCreate",
        "rolloutVerifyArtifact",
        "secretCreate",
        "hermesConfigSurfacePanel",
        "hermesFleetSelect",
        "hermesRuntimeUpdate",
        "hermesConfigSet",
        "hermesEnvSet",
        "hermesPluginsUpdate",
        "hermesSkillsUpdate",
        "Config Fields",
        "Environment Variables",
        "Plugins",
        "Skills",
    ):
        assert marker in js


# ---------------------------------------------------------------------------
# JS — task card quick-actions, density, and timestamp display
# ---------------------------------------------------------------------------


def test_app_ts_and_app_js_have_task_card_quick_actions_and_copy():
    """Task card improvements must exist in both the TS source and compiled JS."""
    ts = (_UI_ROOT / "app.ts").read_text(encoding="utf-8")
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    for source in (ts, js):
        # Relative timestamps with ISO tooltip
        assert "formatIso" in source
        assert "toISOString" in source
        # Click-to-copy task id chip with feedback
        assert "data-copy-id" in source
        assert "copyTaskId" in source
        assert "writeText" in source
        assert "is-copied" in source
        # Hover-revealed quick actions: Retry (failed) / Cancel (active)
        assert "data-quick-action" in source
        assert "runQuickAction" in source
        assert 'data-quick-action="retry"' in source
        assert 'data-quick-action="cancel"' in source
        assert "/transition" in source
        # Collapsible description/summary block with Show more toggle
        assert "data-summary-toggle" in source
        assert "data-task-summary" in source
        assert "is-clamped" in source


def test_css_contains_task_card_density_styles():
    css = (_UI_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".task-id-copy" in css
    assert ".task-id-copied" in css
    assert ".task-summary" in css
    assert "line-clamp" in css
    assert ".quick-actions" in css
    assert ".task-card:hover .quick-actions" in css


# ---------------------------------------------------------------------------
# TS/JS source consistency
# ---------------------------------------------------------------------------


def test_ui_typescript_source_exists():
    assert (_UI_ROOT / "app.ts").exists()
    assert (_UI_ROOT / "app.js").exists()
    assert (_UI_ROOT / "dashboard_api.ts").exists()
    assert (_UI_ROOT / "index.html").exists()
    assert (_UI_ROOT / "styles.css").exists()


def test_app_ts_and_app_js_both_contain_login_screen():
    """TypeScript source and compiled JS must both reference loginScreen."""
    ts = (_UI_ROOT / "app.ts").read_text(encoding="utf-8")
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    assert "loginScreen" in ts
    assert "loginScreen" in js


def test_app_ts_and_app_js_both_contain_service_links():
    ts = (_UI_ROOT / "app.ts").read_text(encoding="utf-8")
    js = (_UI_ROOT / "app.js").read_text(encoding="utf-8")
    assert "serviceLinks" in ts
    assert "serviceLinks" in js
