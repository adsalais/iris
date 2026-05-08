from iris.auth.session import Rights, rights_from_dict, rights_to_dict


def test_empty_rights_admits_nothing():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert not r.has_read("finance")
    assert not r.has_write("finance")
    assert not r.has_admin("finance")


def test_is_admin_implies_all():
    r = Rights(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert r.has_read("anything")
    assert r.has_write("anything")
    assert r.has_admin("anything")


def test_db_admin_implies_writer_and_reader():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert r.has_read("finance")
    assert r.has_write("finance")
    assert r.has_admin("finance")
    assert not r.has_read("hr")


def test_db_writer_implies_reader_not_admin():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset({"finance"}),
        db_reader=frozenset(),
    )
    assert r.has_read("finance")
    assert r.has_write("finance")
    assert not r.has_admin("finance")


def test_db_reader_only_reads():
    r = Rights(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset({"finance"}),
    )
    assert r.has_read("finance")
    assert not r.has_write("finance")
    assert not r.has_admin("finance")


def test_serialization_roundtrip():
    r = Rights(
        is_admin=True,
        can_create_database=True,
        db_admin=frozenset({"finance"}),
        db_writer=frozenset({"hr", "logs"}),
        db_reader=frozenset({"clickstream"}),
    )
    d = rights_to_dict(r)
    assert d == {
        "is_admin": True,
        "can_create_database": True,
        "db_admin": ["finance"],
        "db_writer": ["hr", "logs"],
        "db_reader": ["clickstream"],
    }
    assert rights_from_dict(d) == r


def test_deserialize_missing_field_defaults_false_or_empty():
    r = rights_from_dict({"is_admin": False, "can_create_database": False})
    assert r.db_admin == frozenset()
    assert r.db_writer == frozenset()
    assert r.db_reader == frozenset()
