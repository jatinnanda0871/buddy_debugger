"""Static contract checks between the real extension and its test double.

`tests/fake_bridge.py` stands in for `vscode-extension/extension.js` in nearly
every bridge test, which means those tests only ever prove the Python client
agrees with the *fake*. If the extension grows a route, renames a descriptor
field, or drops one, the fake keeps the suite green while the product is
broken -- exactly how the `aschar 46` and `-data-disassemble` defects survived.

These tests pin the two places the two implementations must agree: the HTTP
route table, and the discovery descriptor. They are static (no VS Code, no
Node), so they run everywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXTENSION = REPO / "vscode-extension" / "extension.js"
FAKE = REPO / "tests" / "fake_bridge.py"
CLIENT = REPO / "src" / "gdb_mcp" / "vscode_bridge.py"


def extension_source() -> str:
    return EXTENSION.read_text(encoding="utf-8")


def extension_routes() -> set[str]:
    """Routes the real extension dispatches, from its switch statement."""
    return set(re.findall(r"case '(/[a-z]+)'", extension_source()))


def fake_routes() -> set[str]:
    return set(re.findall(r'path == "(/[a-z]+)"', FAKE.read_text(encoding="utf-8")))


def client_routes() -> set[str]:
    """Routes the Python client actually calls."""
    source = CLIENT.read_text(encoding="utf-8")
    found = set(re.findall(r'call\(\s*"[A-Z]+",\s*\n?\s*f?"(/[a-z]+)', source))
    found |= set(re.findall(r'"(/[a-z]+)\?', source))
    return found


def test_the_files_are_where_these_tests_think_they_are():
    for path in (EXTENSION, FAKE, CLIENT):
        assert path.is_file(), f"missing {path}"


def test_extension_declares_some_routes():
    """Guard the regex itself: a silent zero would make every check vacuous."""
    assert len(extension_routes()) >= 6, extension_routes()


def test_fake_bridge_implements_every_extension_route():
    missing = extension_routes() - fake_routes()
    assert not missing, (
        f"fake_bridge does not implement {sorted(missing)}. Tests using the "
        "fake would pass while the real bridge is exercised differently."
    )


def test_fake_bridge_invents_no_routes_of_its_own():
    extra = fake_routes() - extension_routes()
    assert not extra, (
        f"fake_bridge serves {sorted(extra)}, which the extension does not. "
        "Tests could depend on behaviour that does not exist in production."
    )


def test_client_only_calls_routes_the_extension_serves():
    unknown = client_routes() - extension_routes()
    assert not unknown, (
        f"the client calls {sorted(unknown)}, which the extension does not "
        "serve; those calls would 404 against the real bridge."
    )


def extension_descriptor_fields() -> set[str]:
    """Keys the extension writes into ~/.gdb-mcp/bridges/<pid>.json.

    Parses the `const info = {...}` literal (JS shorthand included, so `token,`
    counts) plus any later `info.field = ...` assignments.
    """
    source = extension_source()
    start = source.index("const info = {")
    open_brace = source.index("{", start)
    depth = 0
    block = ""
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                block = source[open_brace + 1 : index]
                break
    fields = set(re.findall(r"^\s*([A-Za-z_]\w*)\s*[:,]", block, re.M))
    fields |= set(re.findall(r"info\.([A-Za-z_]\w*)\s*=", source))
    return fields


def test_descriptor_fields_the_client_reads_are_written_by_the_extension():
    """Discovery is a file contract, so a rename breaks it silently."""
    written = extension_descriptor_fields()
    # Everything vscode_bridge.discover()/BridgeClient reads out of it.
    required = {"pid", "token", "transport", "startedAt", "workspaceFolders"}
    missing = required - written
    assert not missing, (
        f"the client reads descriptor field(s) {sorted(missing)}, but "
        f"extension.js only writes {sorted(written)}"
    )


def test_descriptor_carries_both_transports():
    written = extension_descriptor_fields()
    assert "socketPath" in written, "unix transport field missing"
    assert {"host", "port"} <= written, "tcp fallback fields missing"


def test_fake_bridge_descriptor_matches_the_extension():
    """The fake's discovery_info() must not invent or omit fields."""
    fake_source = FAKE.read_text(encoding="utf-8")
    start = fake_source.index("def discovery_info")
    end = fake_source.index("\n    def ", start + 1)  # stop at the next method
    block = fake_source[start:end]
    fake_fields = set(re.findall(r'"([A-Za-z_]\w*)":', block))
    fake_fields |= set(re.findall(r'info\["([A-Za-z_]\w*)"\]', block))

    written = extension_descriptor_fields()
    invented = fake_fields - written
    assert not invented, (
        f"fake_bridge writes descriptor field(s) {sorted(invented)} that the "
        "real extension does not"
    )


def test_event_fields_the_client_reads_are_emitted_by_the_extension():
    """The event stream is how the client learns the program stopped.

    A rename here would strand every resume in its "still running" branch.
    """
    source = extension_source()
    # Envelope written by pushEvent(), plus the per-event fields the client
    # reads in _await_stop / _describe_event.
    for field in ("seq", "time", "type", "reason", "threadId", "exitCode"):
        assert re.search(rf"\b{field}\b", source), (
            f"the client reads event field {field!r}, which extension.js "
            "never emits"
        )


def test_stop_event_names_agree():
    """vscode_bridge.STOP_EVENTS must match what the extension actually pushes."""
    from gdb_mcp.vscode_bridge import STOP_EVENTS

    source = extension_source()
    pushed = set(re.findall(r"pushEvent\('([a-zA-Z]+)'", source))
    missing = set(STOP_EVENTS) - pushed
    assert not missing, (
        f"the client waits for event type(s) {sorted(missing)} that the "
        f"extension never pushes; it pushes {sorted(pushed)}"
    )


def test_extension_requires_a_bearer_token_on_every_request():
    source = extension_source()
    assert "authorized(req)" in source, "no auth check in the request handler"
    assert "Bearer " in source, "extension does not parse a bearer token"
    # The check must gate dispatch, not merely exist.
    assert re.search(r"if \(!authorized\(req\)\)", source), (
        "authorized() is defined but never used to reject a request"
    )


def test_fake_bridge_also_enforces_the_token():
    """Otherwise the client's auth handling is never really exercised."""
    source = FAKE.read_text(encoding="utf-8")
    assert "authorization" in source and "401" in source
