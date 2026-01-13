#!/usr/bin/env python3
"""Sign a policy file with Ed25519 private key."""

import argparse
import json
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from twinops.agent.policy_signing import sign_policy, verify_policy_signature


def main():
    parser = argparse.ArgumentParser(description="Sign a policy file")
    parser.add_argument(
        "--policy-file", "-p",
        required=True,
        help="Path to policy JSON file"
    )
    parser.add_argument(
        "--private-key", "-k",
        required=True,
        help="Path to Ed25519 private key PEM"
    )
    parser.add_argument(
        "--public-key",
        help="Path to Ed25519 public key PEM (for verification)"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file for signed policy (default: <policy-file>.signed.json)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify signature after signing"
    )

    args = parser.parse_args()

    policy_path = Path(args.policy_file)
    private_key_path = Path(args.private_key)

    if not policy_path.exists():
        print(f"Error: Policy file not found: {policy_path}")
        sys.exit(1)

    if not private_key_path.exists():
        print(f"Error: Private key not found: {private_key_path}")
        sys.exit(1)

    # Read policy JSON (preserving exact bytes for signing)
    policy_json = policy_path.read_text()
    private_pem = private_key_path.read_text()

    # Sign the policy
    signature = sign_policy(policy_json, private_pem)

    print(f"Policy signed successfully")
    print(f"Signature: {signature[:32]}...")

    # Verify if requested
    if args.verify and args.public_key:
        public_key_path = Path(args.public_key)
        if not public_key_path.exists():
            print(f"Warning: Public key not found for verification: {public_key_path}")
        else:
            public_pem = public_key_path.read_text()
            is_valid = verify_policy_signature(policy_json, public_pem, signature)
            if is_valid:
                print("✓ Signature verified")
            else:
                print("✗ Signature verification FAILED")
                sys.exit(1)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = policy_path.with_suffix(".signed.json")

    # Write signed policy
    signed_output = {
        "policy_json": policy_json,
        "signature": signature,
    }

    # Also provide separate files for AAS integration
    output_path.write_text(json.dumps(signed_output, indent=2))
    print(f"Signed policy saved to: {output_path}")

    # Write signature only file
    sig_path = output_path.with_suffix(".sig")
    sig_path.write_text(signature)
    print(f"Signature saved to: {sig_path}")


if __name__ == "__main__":
    main()
