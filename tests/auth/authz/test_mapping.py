import pytest

from iris.auth.authz.mapping import RoleMapping, RoleMappingError, parse


def test_parses_minimal_valid_file():
    text = """
roles:
  reader:
    groups: []
    users: []
"""
    m = parse(text)
    assert isinstance(m, RoleMapping)
    assert set(m.roles.keys()) == {"reader"}
    assert m.closure["reader"] == frozenset({"reader"})


def test_omitted_lists_default_to_empty():
    text = """
roles:
  reader: {}
"""
    m = parse(text)
    assert m.roles["reader"].groups == frozenset()
    assert m.roles["reader"].users_lower == frozenset()
    assert m.roles["reader"].includes == ()


def test_includes_creates_transitive_closure():
    text = """
roles:
  reader: {}
  writer:
    includes: [reader]
  admin:
    includes: [writer]
"""
    m = parse(text)
    assert m.closure["reader"] == frozenset({"reader"})
    assert m.closure["writer"] == frozenset({"reader", "writer"})
    assert m.closure["admin"] == frozenset({"reader", "writer", "admin"})


def test_diamond_inheritance_resolves_correctly():
    text = """
roles:
  reader: {}
  writer:
    includes: [reader]
  reviewer:
    includes: [reader]
  admin:
    includes: [writer, reviewer]
"""
    m = parse(text)
    assert m.closure["admin"] == frozenset({"reader", "writer", "reviewer", "admin"})


def test_users_are_lowercased_for_matching():
    text = """
roles:
  admin:
    users: ["Alice", "BOB"]
"""
    m = parse(text)
    assert m.roles["admin"].users_lower == frozenset({"alice", "bob"})


def test_groups_remain_case_sensitive():
    text = """
roles:
  admin:
    groups: ["LDAP-Admins", "platform-team"]
"""
    m = parse(text)
    assert m.roles["admin"].groups == frozenset({"LDAP-Admins", "platform-team"})


def test_rejects_unknown_top_level_key():
    text = """
roles:
  reader: {}
extra: stuff
"""
    with pytest.raises(RoleMappingError, match="unknown top-level key"):
        parse(text)


def test_rejects_missing_top_level_roles_key():
    with pytest.raises(RoleMappingError, match="missing required key 'roles'"):
        parse("other: 1\n")


def test_rejects_unknown_role_entry_key():
    text = """
roles:
  reader:
    extras: []
"""
    with pytest.raises(RoleMappingError, match="unknown key 'extras'"):
        parse(text)


def test_rejects_role_name_with_disallowed_chars():
    text = """
roles:
  "bad name":
    groups: []
"""
    with pytest.raises(RoleMappingError, match="invalid role name"):
        parse(text)


def test_rejects_undefined_include():
    text = """
roles:
  writer:
    includes: [reader]
"""
    with pytest.raises(RoleMappingError, match="undefined role 'reader'"):
        parse(text)


def test_rejects_direct_cycle():
    text = """
roles:
  a:
    includes: [b]
  b:
    includes: [a]
"""
    with pytest.raises(RoleMappingError, match="cycle"):
        parse(text)


def test_rejects_self_cycle():
    text = """
roles:
  a:
    includes: [a]
"""
    with pytest.raises(RoleMappingError, match="cycle"):
        parse(text)


def test_rejects_indirect_cycle():
    text = """
roles:
  a:
    includes: [b]
  b:
    includes: [c]
  c:
    includes: [a]
"""
    with pytest.raises(RoleMappingError, match="cycle"):
        parse(text)


def test_rejects_duplicate_role_keys():
    text = """
roles:
  reader:
    groups: []
  reader:
    users: []
"""
    with pytest.raises(RoleMappingError, match="duplicate"):
        parse(text)


def test_rejects_non_list_groups():
    text = """
roles:
  reader:
    groups: "not-a-list"
"""
    with pytest.raises(RoleMappingError, match="must be a list"):
        parse(text)


def test_rejects_non_string_in_groups():
    text = """
roles:
  reader:
    groups: [123]
"""
    with pytest.raises(RoleMappingError, match="must be a string"):
        parse(text)


def test_empty_roles_block_parses_to_empty_mapping():
    text = "roles: {}\n"
    m = parse(text)
    assert m.roles == {}
    assert m.closure == {}


def test_yaml_syntax_error_raised_as_role_mapping_error():
    text = "roles:\n  - this is: malformed\n   indent: bad\n"
    with pytest.raises(RoleMappingError):
        parse(text)
