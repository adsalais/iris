"""The Authorization feature's install hook contributes nav and registers intents."""
from __future__ import annotations


def test_install_adds_authorization_nav_group(app):
    contribs = app.state.contributions
    labels = [g.label for g in contribs.nav.groups]
    assert "Authorization" in labels


def test_install_adds_org_admin_nav_group(app):
    contribs = app.state.contributions
    labels = [g.label for g in contribs.nav.groups]
    assert "Org admin" in labels


def test_org_admin_only_visible_to_admin(app):
    from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
    contribs = app.state.contributions
    org_admin_groups = [g for g in contribs.nav.groups if g.label == "Org admin"]
    assert len(org_admin_groups) == 1
    g = org_admin_groups[0]
    assert g.visible(EMPTY_CAPABILITIES) is False
    assert g.visible(Capabilities(
        is_admin=True, can_create_database=False,
        db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset(),
    )) is True


def test_install_registers_my_access_intent(app):
    dispatcher = app.state.intent_dispatcher
    spec = dispatcher.resolve("authorization", "my_access")
    assert spec.feature == "authorization"
    assert spec.intent == "my_access"
    assert spec.title({}) == "My access"
