"""LDAP integration tests against a real bitnami/openldap container."""

from __future__ import annotations

import ssl
import stat

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def test_collection_smoke():
    """Placeholder: pytest can collect tests under tests/auth/integration."""
    assert True


def test_tls_helper_generates_valid_chain(tmp_path):
    """generate_ca_and_leaf produces a leaf cert signed by the CA, with
    SANs covering localhost + 127.0.0.1, in PEM files we can load."""
    from tests.auth.integration._tls import generate_ca_and_leaf

    paths = generate_ca_and_leaf(tmp_path)

    ca_cert = x509.load_pem_x509_certificate(paths.ca_pem.read_bytes())
    server_cert = x509.load_pem_x509_certificate(paths.server_pem.read_bytes())

    # Leaf is signed by CA. Narrow the public-key union to RSA so pyright is
    # happy and the .verify() call type-checks; this also fails loudly if the
    # generator ever swaps to a non-RSA key type.
    ca_pubkey = ca_cert.public_key()
    assert isinstance(ca_pubkey, rsa.RSAPublicKey)
    sig_hash = server_cert.signature_hash_algorithm
    assert sig_hash is not None
    ca_pubkey.verify(
        server_cert.signature,
        server_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        sig_hash,
    )

    # SANs include localhost (DNS) and 127.0.0.1 (IP).
    san_ext = server_cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    dns_names = san_ext.value.get_values_for_type(x509.DNSName)
    ip_addresses = [str(ip) for ip in san_ext.value.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ip_addresses

    # ssl.SSLContext can load both as a cert chain (proves PEM format is valid).
    ctx = ssl.create_default_context(cafile=str(paths.ca_pem))
    ctx.load_cert_chain(certfile=str(paths.server_pem), keyfile=str(paths.server_key))

    # Key file is mode 0600.
    assert stat.S_IMODE(paths.server_key.stat().st_mode) == 0o600


def test_tls_paths_fixture_yields_resolved_paths(tls_paths):
    """The session-scoped tls_paths fixture yields absolute, resolved paths."""
    assert tls_paths.ca_pem.is_absolute()
    assert tls_paths.ca_pem.exists()
    assert tls_paths.server_pem.exists()
    assert tls_paths.server_key.exists()
