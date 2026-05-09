"""Pin the testcontainer's iris_svc privilege set.

If a future testcontainer image, env-var change, or conftest edit ever
strips iris_svc of NAMED COLLECTION ADMIN, iris's bootstrap_admin's
``GRANT ALL ON *.*`` will start failing with an opaque CH error. This
smoke test pins the privilege so the regression fires before any of the
bootstrap-using tests do.
"""
from __future__ import annotations


def test_iris_svc_can_grant_all(ch_client):
    """iris_svc must hold every privilege ``GRANT ALL`` requires —
    crucially the named-collection family (``NAMED COLLECTION``,
    ``SHOW NAMED COLLECTIONS SECRETS``, etc.) that the stock
    testcontainer's ``test`` user lacks. The conftest's users.d overlay
    grants the missing pieces to ``test``, and iris_svc inherits them
    via ``GRANT CURRENT GRANTS ON *.* TO iris_svc WITH GRANT OPTION``.

    Smoke-test by attempting ``GRANT ALL`` to a throwaway role as
    iris_svc; if the privilege chain breaks, ``GRANT ALL`` raises with
    a clear ``Missing permissions:`` error and this test pins the
    regression before any bootstrap-using test does.
    """
    ch_client.command("CREATE ROLE IF NOT EXISTS iris_svc_grant_probe")
    try:
        ch_client.command(
            "GRANT ALL ON *.* TO iris_svc_grant_probe WITH GRANT OPTION"
        )
    finally:
        ch_client.command("DROP ROLE IF EXISTS iris_svc_grant_probe")
