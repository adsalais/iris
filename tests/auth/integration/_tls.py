"""Generate a self-signed CA + leaf cert for integration tests.

Pure cryptography, no openssl shell-out. Used by the Keycloak fixture for
HTTPS and by OAuthProvider for verifying the IdP's certificate chain.
"""

from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class TLSPaths:
    ca_pem: Path
    server_pem: Path
    server_key: Path


def generate_ca_and_leaf(target_dir: Path) -> TLSPaths:
    """Generate a CA + leaf cert into target_dir.

    Returns:
        TLSPaths with absolute paths to ca.pem, server.pem, server.key.
        server.key has mode 0600.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=365)

    # CA
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "iris-test-ca"),
    ])
    ca_public_key = ca_key.public_key()
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # SubjectKeyIdentifier is required on CA certs so that leaf certs can
        # include a matching AuthorityKeyIdentifier — required by OpenSSL 3.x.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_public_key),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Leaf (CN=localhost, SAN=DNS:localhost,IP:127.0.0.1)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    server_public_key = server_key.public_key()
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(ca_subject)
        .public_key(server_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_public_key),
            critical=False,
        )
        # AuthorityKeyIdentifier links the leaf back to the CA key; required by
        # OpenSSL 3.x when building certificate chains.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_public_key),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = target_dir / "ca.pem"
    server_pem = target_dir / "server.pem"
    server_key_path = target_dir / "server.key"

    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_pem.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_path.write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    server_key_path.chmod(0o600)

    return TLSPaths(
        ca_pem=ca_pem.resolve(),
        server_pem=server_pem.resolve(),
        server_key=server_key_path.resolve(),
    )
