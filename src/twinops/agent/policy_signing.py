"""CovenantTwin - Cryptographically signed policy verification."""

import base64
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from twinops.common.logging import get_logger

logger = get_logger(__name__)


class PolicyVerificationError(Exception):
    """Error verifying policy signature."""

    pass


@dataclass
class SignedPolicy:
    """A signed policy extracted from CovenantTwin."""

    policy_json: str
    public_key_pem: str
    signature_b64: str
    is_verified: bool = False


def verify_policy_signature(
    policy_json: str,
    public_key_pem: str,
    signature_b64: str,
) -> bool:
    """
    Verify Ed25519 signature over exact UTF-8 bytes of PolicyJson.

    This avoids JSON canonicalization ambiguity—sign what you store.

    Args:
        policy_json: Raw JSON string of policy
        public_key_pem: PEM-encoded Ed25519 public key
        signature_b64: Base64-encoded signature

    Returns:
        True if signature is valid

    Raises:
        PolicyVerificationError: If verification fails
    """
    try:
        # Load public key
        pub_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))

        if not isinstance(pub_key, ed25519.Ed25519PublicKey):
            raise PolicyVerificationError("Key is not Ed25519")

        # Decode signature
        signature = base64.b64decode(signature_b64)

        # Verify
        pub_key.verify(signature, policy_json.encode("utf-8"))
        return True

    except InvalidSignature:
        logger.warning("Policy signature verification failed")
        return False
    except Exception as e:
        raise PolicyVerificationError(f"Verification error: {e}") from e


def sign_policy(
    policy_json: str,
    private_key_pem: str,
) -> str:
    """
    Sign a policy with an Ed25519 private key.

    Args:
        policy_json: Raw JSON string of policy
        private_key_pem: PEM-encoded Ed25519 private key

    Returns:
        Base64-encoded signature
    """
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )

    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise PolicyVerificationError("Key is not Ed25519")

    signature = private_key.sign(policy_json.encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """
    Generate a new Ed25519 key pair for policy signing.

    Returns:
        Tuple of (private_key_pem, public_key_pem)
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    return private_pem, public_pem


async def extract_signed_policy_from_submodel(
    submodel: dict[str, Any],
) -> SignedPolicy | None:
    """
    Extract signed policy from a PolicyTwin submodel.

    Expected structure:
    - PolicyJson: Property containing the policy
    - PolicyPublicKeyPem: Property with Ed25519 public key
    - PolicySignature: Property with Base64 signature

    Args:
        submodel: Submodel JSON structure

    Returns:
        SignedPolicy or None if not found
    """
    elements = submodel.get("submodelElements", [])

    policy_json = None
    public_key_pem = None
    signature_b64 = None

    for elem in elements:
        id_short = elem.get("idShort", "")

        if id_short == "PolicyJson":
            value = elem.get("value")
            if isinstance(value, str):
                policy_json = value
        elif id_short == "PolicyPublicKeyPem":
            value = elem.get("value")
            if isinstance(value, str):
                public_key_pem = value
        elif id_short == "PolicySignature":
            value = elem.get("value")
            if isinstance(value, str):
                signature_b64 = value

    if not all([policy_json, public_key_pem, signature_b64]):
        return None

    assert policy_json is not None
    assert public_key_pem is not None
    assert signature_b64 is not None

    return SignedPolicy(
        policy_json=policy_json,
        public_key_pem=public_key_pem,
        signature_b64=signature_b64,
    )


def verify_and_load_policy(
    signed_policy: SignedPolicy,
    require_verification: bool = True,
) -> dict[str, Any]:
    """
    Verify signature and parse policy JSON.

    Args:
        signed_policy: SignedPolicy object
        require_verification: If True, raise on invalid signature

    Returns:
        Parsed policy dictionary

    Raises:
        PolicyVerificationError: If verification required and fails
    """
    import json

    is_valid = verify_policy_signature(
        signed_policy.policy_json,
        signed_policy.public_key_pem,
        signed_policy.signature_b64,
    )

    if not is_valid and require_verification:
        raise PolicyVerificationError("Policy signature is invalid")

    signed_policy.is_verified = is_valid

    parsed = json.loads(signed_policy.policy_json)
    if not isinstance(parsed, dict):
        raise PolicyVerificationError("Policy JSON must be a JSON object")
    return parsed
