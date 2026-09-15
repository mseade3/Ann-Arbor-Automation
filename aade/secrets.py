"""
Small CLI for storing AADE secrets in OS keyring.

Examples:
  python -m aade.secrets set OPENAI_API_KEY
  python -m aade.secrets get OPENAI_API_KEY
  python -m aade.secrets delete OPENAI_API_KEY
  python -m aade.secrets list
"""

from __future__ import annotations

import argparse
import getpass
import os


DEFAULT_SERVICE = "ann-arbor-automated"
KNOWN_KEYS = (
    "OPENAI_API_KEY",
    "GOOGLE_PLACES_API_KEY",
    "GOOGLE_EMBED_API_KEY",
    "MAPBOX_API_KEY",
)


def _service_name(cli_value: str | None) -> str:
    return cli_value or os.getenv("AADE_KEYRING_SERVICE") or DEFAULT_SERVICE


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Manage AADE secrets in OS keyring")
    ap.add_argument(
        "--service",
        default=None,
        help=(
            "Keyring service name (default: AADE_KEYRING_SERVICE env var, "
            f"else '{DEFAULT_SERVICE}')"
        ),
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Set a secret value")
    p_set.add_argument("key", help="Secret key name (e.g. OPENAI_API_KEY)")
    p_set.add_argument(
        "value",
        nargs="?",
        default=None,
        help="Secret value. If omitted, prompt securely.",
    )

    p_get = sub.add_parser("get", help="Get a secret value")
    p_get.add_argument("key", help="Secret key name")

    p_delete = sub.add_parser("delete", help="Delete a secret value")
    p_delete.add_argument("key", help="Secret key name")

    sub.add_parser("list", help="Show known AADE keys and whether they exist")
    return ap


def main() -> int:
    ap = _build_parser()
    args = ap.parse_args()
    service = _service_name(args.service)
    try:
        import keyring  # type: ignore
    except Exception:
        print("keyring is not installed. Run: pip install -r requirements.txt")
        return 2

    if args.command == "set":
        value = args.value if args.value is not None else getpass.getpass("Secret value: ")
        if not value:
            print("No value provided; nothing saved.")
            return 1
        keyring.set_password(service, args.key, value)
        print(f"Saved {args.key} in keyring service '{service}'.")
        return 0

    if args.command == "get":
        value = keyring.get_password(service, args.key)
        if value is None:
            print(f"No value found for {args.key} in service '{service}'.")
            return 1
        print(value)
        return 0

    if args.command == "delete":
        value = keyring.get_password(service, args.key)
        if value is None:
            print(f"No value found for {args.key} in service '{service}'.")
            return 1
        keyring.delete_password(service, args.key)
        print(f"Deleted {args.key} from service '{service}'.")
        return 0

    if args.command == "list":
        for key in KNOWN_KEYS:
            exists = keyring.get_password(service, key) is not None
            status = "set" if exists else "missing"
            print(f"{key}: {status}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
