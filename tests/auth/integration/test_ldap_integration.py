"""LDAP integration tests against a real bitnami/openldap container."""

from __future__ import annotations


def test_collection_smoke():
    """Placeholder: pytest can collect tests under tests/auth/integration."""
    assert True


import ssl
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


def test_tls_helper_generates_valid_chain(tmp_path):
    """generate_ca_and_leaf produces a leaf cert signed by the CA, with
    SANs covering localhost + 127.0.0.1, in PEM files we can load."""
    from tests.auth.integration._tls import generate_ca_and_leaf

    paths = generate_ca_and_leaf(tmp_path)

    ca_pem = paths.ca_pem.read_bytes()
    server_pem = paths.server_pem.read_bytes()
    server_key = paths.server_key.read_bytes()

    ca_cert = x509.load_pem_x509_certificate(ca_pem)
    server_cert = x509.load_pem_x509_certificate(server_pem)

    # Leaf is signed by CA: verify the signature with CA's public key.
    ca_cert.public_key().verify(  # type: ignore[union-attr]
        server_cert.signature,
        server_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        server_cert.signature_hash_algorithm,  # type: ignore[arg-type]
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
    import stat
    assert stat.S_IMODE(paths.server_key.stat().st_mode) == 0o600
