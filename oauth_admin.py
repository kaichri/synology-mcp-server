"""Offline administration: password hashes and emergency OAuth revocation."""
import argparse
import getpass
import os

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from pathlib import Path
from oauth_config import secret, validate_password_hash
from oauth_store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["hash-password", "verify-password", "revoke-all"])
    parser.add_argument("--database", default=os.environ.get("MCP_OAUTH_DATABASE", "data/oauth.sqlite3"))
    parser.add_argument("--output", help="Write a new hash file without printing the hash; refuses to overwrite")
    parser.add_argument("--hash-file", help="Hash file for verify-password; defaults to configured secret or secrets/oauth-password-hash.txt")
    args = parser.parse_args()
    if args.output and args.command != "hash-password" or args.hash_file and args.command != "verify-password":
        parser.error("--output is for hash-password; --hash-file is for verify-password")
    if args.command == "hash-password":
        password = getpass.getpass("OAuth password (at least 16 characters): ")
        if len(password) < 16 or password != getpass.getpass("Repeat password: "):
            parser.error("Passwords must match and contain at least 16 characters")
        hashed = PasswordHasher().hash(password)
        if args.output:
            path = Path(args.output)
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                    file.write(hashed + "\n")
            except OSError:
                parser.error("Could not create hash file; check permissions and whether it already exists")
            print("Password hash saved.")
        else:
            print(hashed)
    elif args.command == "verify-password":
        try:
            hashed = (Path(args.hash_file).read_text(encoding="utf-8").strip() if args.hash_file
                      else secret("MCP_OAUTH_PASSWORD_HASH") or Path("secrets/oauth-password-hash.txt").read_text(encoding="utf-8").strip())
            validate_password_hash(hashed)
        except (OSError, ValueError):
            parser.error("Could not read a valid Argon2id hash; check the secret file and its contents")
        try:
            valid = PasswordHasher().verify(hashed, getpass.getpass("OAuth password: "))
        except VerificationError:
            valid = False
        if not valid:
            parser.exit(1, "Password does not match.\n")
        print("Password verified.")
    else:
        store = Store(args.database)
        try:
            with store.transaction():
                store.execute("UPDATE tokens SET revoked=1")
                store.execute("UPDATE codes SET used=1")
                store.execute("DELETE FROM pending")
        finally:
            store.close()
        print("All OAuth grants revoked. The separately configured legacy bearer token is unchanged.")


if __name__ == "__main__":
    main()
