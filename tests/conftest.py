import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _write_cert_key(tmp_path, name, days_valid=365, not_before_offset_days=0):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"test-{name}")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now + datetime.timedelta(days=not_before_offset_days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + datetime.timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def make_valid_cert_key(tmp_path):
    return _write_cert_key(tmp_path, "valid")


def make_expired_cert_key(tmp_path):
    return _write_cert_key(tmp_path, "expired", days_valid=1, not_before_offset_days=-30)


def make_mismatched_key(tmp_path):
    """Devuelve un cert válido junto con la clave de OTRO par (no coincide)."""
    cert_path, _ = _write_cert_key(tmp_path, "pairA")
    _, other_key_path = _write_cert_key(tmp_path, "pairB")
    return cert_path, other_key_path
