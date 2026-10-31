"""X.509 identities and pinned TLS contexts for Cluster peers."""

from __future__ import annotations

import ipaddress
import ssl
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from atlas_core.ipc import IPCError


def crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise IPCError("cluster_dependency_missing") from None
    return x509, serialization, ed25519


def endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme != "tls"
            or not host
            or not port
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError
        ipaddress.ip_address(host)
        return host, port
    except (ValueError, TypeError):
        raise IPCError("invalid_endpoint") from None


def new_key() -> str:
    _, serialization, ed25519 = crypto()
    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def _key(pem: str):
    _, serialization, _ = crypto()
    return serialization.load_pem_private_key(pem.encode(), password=None)


def request_identity(key: str, node_id: str) -> str:
    x509, serialization, _ = crypto()
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, node_id)])
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .sign(_key(key), None)
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )


def issue(
    ca_key: str, cluster_id: str, *, csr: str | None = None, node_id: str | None = None
) -> str:
    x509, serialization, ed25519 = crypto()
    key = _key(ca_key)
    issuer = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cluster_id)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .issuer_name(issuer)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
    )
    if csr is None:
        builder = (
            builder.subject_name(issuer)
            .public_key(key.public_key())
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        )
    else:
        request = x509.load_pem_x509_csr(csr.encode())
        if not request.is_signature_valid or not isinstance(
            request.public_key(), ed25519.Ed25519PublicKey
        ):
            raise IPCError("invalid_identity")
        builder = (
            builder.subject_name(
                x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, node_id)])
            )
            .public_key(request.public_key())
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(f"{node_id}.invalid")]),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                        x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                    ]
                ),
                critical=False,
            )
        )
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
        critical=False,
    )
    if csr is None:
        builder = builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    builder = builder.add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=csr is None,
            crl_sign=csr is None,
            encipher_only=None,
            decipher_only=None,
        ),
        critical=True,
    )
    return builder.sign(key, None).public_bytes(serialization.Encoding.PEM).decode()


def peer_identity(certificate: bytes) -> tuple[str, str]:
    x509, serialization, _ = crypto()
    cert = x509.load_der_x509_certificate(certificate)
    node_id = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    return node_id, cert.public_bytes(serialization.Encoding.PEM).decode()


def tls_context(
    directory: Path,
    ca: str,
    *,
    key: str | None = None,
    certificate: str | None = None,
    server: bool = False,
) -> ssl.SSLContext:
    context = ssl.SSLContext(
        ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_verify_locations(cadata=ca)
    context.verify_mode = ssl.CERT_OPTIONAL if server else ssl.CERT_REQUIRED
    if key is not None:
        # SSLContext accepts filenames; temporary key files stay within private state.
        with tempfile.TemporaryDirectory(prefix="tls-", dir=directory) as staging:
            key_path, cert_path = Path(staging) / "key", Path(staging) / "cert"
            with key_path.open("w") as stream:
                key_path.chmod(0o600)
                stream.write(key)
            cert_path.write_text(certificate)
            context.load_cert_chain(cert_path, key_path)
    return context
