"""Download sqlglot at the pinned commit from GitHub and unpack it to the given folder.

The images we build from have no git or curl, so this uses only the standard library.
"""
import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"


def main(dest: str) -> None:
    data = urllib.request.urlopen(URL, timeout=120).read()
    prefix = f"sqlglot-{COMMIT}/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.startswith(prefix):
                continue
            member.name = member.name[len(prefix):]
            if not member.name:
                continue
            tar.extract(member, dest, filter="data")
    print(f"sqlglot {COMMIT[:8]} unpacked to {dest}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot")
