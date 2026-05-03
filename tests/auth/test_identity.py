from iris.auth.identity import User


def test_user_carries_username_separate_from_subject():
    u = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins",),
    )
    assert u.username == "alice"
    assert u.subject == "mock:alice"
