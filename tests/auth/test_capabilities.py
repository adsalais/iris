from iris.auth.rights import Capabilities, capabilities_from_dict, capabilities_to_dict


def test_empty_capabilities_admits_nothing():
    c = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert not c.has_read("finance")
    assert not c.has_write("finance")
    assert not c.has_admin("finance")


def test_is_admin_implies_all():
    c = Capabilities(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert c.has_read("anything")
    assert c.has_write("anything")
    assert c.has_admin("anything")


def test_db_admin_implies_writer_and_reader():
    c = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert c.has_read("finance")
    assert c.has_write("finance")
    assert c.has_admin("finance")
    assert not c.has_read("hr")


def test_db_writer_implies_reader_not_admin():
    c = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset({"finance"}),
        db_reader=frozenset(),
    )
    assert c.has_read("finance")
    assert c.has_write("finance")
    assert not c.has_admin("finance")


def test_db_reader_only_reads():
    c = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset({"finance"}),
    )
    assert c.has_read("finance")
    assert not c.has_write("finance")
    assert not c.has_admin("finance")


def test_serialization_roundtrip():
    c = Capabilities(
        is_admin=True,
        can_create_database=True,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset({"hr", "logs"}),
        db_reader=frozenset({"clickstream"}),
    )
    d = capabilities_to_dict(c)
    assert d == {
        "is_admin": True,
        "can_create_database": True,
        "db_admin": ["finance"],
        "db_writer": ["hr", "logs"],
        "db_reader": ["clickstream"],
    }
    assert capabilities_from_dict(d) == c


def test_deserialize_missing_field_defaults_false_or_empty():
    c = capabilities_from_dict({"is_admin": False, "can_create_database": False})
    assert c.db_admin == frozenset()
    assert c.db_writer == frozenset()
    assert c.db_reader == frozenset()
