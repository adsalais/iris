from __future__ import annotations

from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
from iris.shell.contributions import (
    Contributions,
    NavEntry,
    NavGroup,
    TabIntent,
)
from iris.shell.nav_render import render_nav


def _caps(
    *,
    is_admin: bool = False,
    can_create_database: bool = False,
    db_admin: tuple[str, ...] = (),
    db_writer: tuple[str, ...] = (),
    db_reader: tuple[str, ...] = (),
) -> Capabilities:
    return Capabilities(
        is_admin=is_admin,
        can_create_database=can_create_database,
        db_admin=frozenset(db_admin),
        db_writer=frozenset(db_writer),
        db_reader=frozenset(db_reader),
    )


def test_render_empty_contributions_yields_empty_nav():
    html = render_nav(Contributions(), EMPTY_CAPABILITIES)
    assert '<nav class="iris-nav">' in html
    assert '</nav>' in html
    assert 'iris-nav-group' not in html


def test_render_group_with_one_entry():
    c = Contributions()
    c.nav.add(NavGroup(label="Authorization", entries=(NavEntry("My access"),)))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "Authorization" in html
    assert "My access" in html


def test_invisible_group_is_omitted():
    c = Contributions()
    c.nav.add(NavGroup(
        label="Org admin",
        visible=lambda caps: caps.is_admin,
        entries=(NavEntry("All users"),),
    ))
    html = render_nav(c, _caps(is_admin=False))
    assert "Org admin" not in html
    html2 = render_nav(c, _caps(is_admin=True))
    assert "Org admin" in html2


def test_invisible_entry_is_omitted():
    c = Contributions()
    c.nav.add(NavGroup(
        label="Auth",
        entries=(
            NavEntry("Always"),
            NavEntry("Admin only", visible=lambda caps: caps.is_admin),
        ),
    ))
    html = render_nav(c, _caps(is_admin=False))
    assert "Always" in html
    assert "Admin only" not in html


def test_entry_with_on_click_emits_post_to_api_tabs():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=(
        NavEntry("My access", on_click=TabIntent("auth", "my_access")),
    )))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "@post" in html
    assert "/api/tabs" in html
    assert "auth" in html
    assert "my_access" in html


def test_entry_with_params_encodes_params_in_post_body():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=(
        NavEntry("Manage marketing",
                 on_click=TabIntent("auth", "manage", {"database": "marketing"})),
    )))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "marketing" in html


def test_badge_renders_when_predicate_returns_string():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=(
        NavEntry(
            "DBs I admin",
            badge=lambda caps: str(len(caps.db_admin)) if caps.db_admin else None,
        ),
    )))
    html_no_badge = render_nav(c, _caps(db_admin=()))
    assert "iris-nav-badge" not in html_no_badge

    html_with_badge = render_nav(c, _caps(db_admin=("a", "b", "c")))
    assert "iris-nav-badge" in html_with_badge
    assert ">3<" in html_with_badge


def test_dynamic_children_render_inline_under_threshold():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=(
        NavEntry(
            "DBs I admin",
            children=lambda caps: [NavEntry(d) for d in sorted(caps.db_admin)],
        ),
    )))
    html = render_nav(c, _caps(db_admin=("z", "a")))
    assert "<li" in html
    a_pos = html.index(">a<")
    z_pos = html.index(">z<")
    assert a_pos < z_pos


def test_dynamic_children_collapse_to_popover_above_threshold():
    c = Contributions()
    c.nav.add(NavGroup(label="Auth", entries=(
        NavEntry(
            "DBs I admin",
            children=lambda caps: [NavEntry(d) for d in sorted(caps.db_admin)],
        ),
    )))
    many = tuple(f"db{i:02}" for i in range(15))
    html = render_nav(c, _caps(db_admin=many))
    assert "iris-nav-popover" in html
    assert "db00" in html and "db14" in html


def test_html_escapes_label_with_html_chars():
    c = Contributions()
    c.nav.add(NavGroup(label="<script>", entries=(
        NavEntry("ok"),
    )))
    html = render_nav(c, EMPTY_CAPABILITIES)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
