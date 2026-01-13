#!/usr/bin/env python3
"""Generate Ed25519 key pair for policy signing."""

import argparse
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from twinops.agent.policy_signing import generate_keypair


def main():
    parser = argparse.ArgumentParser(description="Generate Ed25519 key pair for policy signing")
    parser.add_argument(
        "--output", "-o",
        default="keys",
        help="Output directory for keys (default: keys)"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Overwrite existing keys"
    )

    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    private_path = output_dir / "policy_private.pem"
    public_path = output_dir / "policy_public.pem"

    if private_path.exists() and not args.force:
        print(f"Error: {private_path} already exists. Use --force to overwrite.")
        sys.exit(1)

    private_pem, public_pem = generate_keypair()

    private_path.write_text(private_pem)
    public_path.write_text(public_pem)

    # Set restrictive permissions on private key
    private_path.chmod(0o600)

    print(f"Private key saved to: {private_path}")
    print(f"Public key saved to: {public_path}")
    print("\n⚠️  Keep the private key secure and never commit it to version control!")


if __name__ == "__main__":
    main()
