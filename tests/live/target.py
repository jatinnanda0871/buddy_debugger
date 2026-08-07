"""Debug target for exercising the bridge: globals, nested frames, locals."""

import sys

g_connection_count = 17
g_hostname = "prod-db-01"
g_config = {"retries": 3, "timeout_ms": 5000, "tags": ["a", "b"]}


class Header:
    def __init__(self, length, magic):
        self.length = length
        self.magic = magic

    def __repr__(self):
        return f"Header(length={self.length}, magic={self.magic:#x})"


def parse_header(raw, limit):
    header = Header(len(raw), 0xDEADBEEF)
    scratch = [i * i for i in range(6)]
    total = sum(scratch)
    if header.length > limit:
        raise ValueError(f"header too long: {header.length} > {limit}")
    return header, total


def handle_packet(payload, limit):
    print(f"handling {len(payload)} bytes", flush=True)
    header, total = parse_header(payload, limit)
    return header, total


def main():
    print("target starting", flush=True)
    header, total = handle_packet(b"hello world", 4096)
    print(f"ok: {header} total={total}", flush=True)
    print(f"globals: {g_connection_count} {g_hostname} {g_config}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
