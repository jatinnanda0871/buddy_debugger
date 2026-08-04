"""Tests for address-range dumps and global-variable discovery."""

from __future__ import annotations

import base64

import pytest

from fake_bridge import FakeBridge  # noqa: F401 - fixture support
from gdb_mcp.tools import build_word_rows, decode_words, render_rows, to_int
from gdb_mcp.vscode_bridge import BridgeError, _parse_info_variables

# -- word grouping ------------------------------------------------------------


def test_decode_words_is_little_endian_hex():
    assert decode_words((0x2A).to_bytes(4, "little"), 4) == ["0x0000002a"]


def test_decode_words_zero_pads_to_word_size():
    assert decode_words((1).to_bytes(4, "little"), 4) == ["0x00000001"]
    assert decode_words((1).to_bytes(8, "little"), 8) == ["0x0000000000000001"]


def test_decode_words_groups_a_range():
    data = b"".join(v.to_bytes(4, "little") for v in (1, 2, 3))
    assert decode_words(data, 4) == ["0x00000001", "0x00000002", "0x00000003"]


def test_decode_words_ignores_trailing_partial_word():
    data = b"\x01\x00\x00\x00\xff"  # one full word plus a stray byte
    assert decode_words(data, 4) == ["0x00000001"]


def test_decode_words_rejects_sub_word_sizes():
    """A word is the smallest unit; bytes and halfwords are not offered."""
    assert decode_words(b"\x01\x02\x03\x04", 1) == []
    assert decode_words(b"\x01\x02\x03\x04", 2) == []
    assert decode_words(b"\x01\x02\x03\x04", 3) == []


# -- row layout ---------------------------------------------------------------


def test_build_word_rows_groups_and_adds_ascii_gutter():
    data = b"Hello, world!!!!"  # exactly two 8-byte giant words
    rows = build_word_rows(data, base_address=0x7FFD0000, word_size=8)
    assert len(rows) == 1
    assert rows[0]["addr"] == "0x00007ffd0000"
    assert rows[0]["data"] == ["0x77202c6f6c6c6548", "0x21212121646c726f"]
    assert rows[0]["ascii"] == "Hello, world!!!!"


def test_build_word_rows_marks_nonprintable_bytes():
    rows = build_word_rows(b"\x00\x01hi\xff\x00\x00\x00", word_size=4)
    # both words sit in one 16-byte row, so the gutter spans all 8 bytes
    assert rows[0]["ascii"] == "..hi...."
    assert rows[0]["data"] == ["0x69680100", "0x000000ff"]


def test_build_word_rows_uses_16_byte_rows_by_default():
    data = bytes(64)
    assert len(build_word_rows(data, word_size=8)) == 4
    assert len(build_word_rows(data, word_size=4)) == 4


def test_build_word_rows_addresses_advance_by_row():
    rows = build_word_rows(bytes(32), base_address=0x1000, word_size=8)
    assert [r["addr"] for r in rows] == ["0x000000001000", "0x000000001010"]


def test_build_word_rows_drops_a_trailing_partial_word():
    rows = build_word_rows(b"\x01" * 9, word_size=4)
    assert sum(len(r["data"]) for r in rows) == 2


def test_build_word_rows_rejects_sub_word_sizes():
    assert build_word_rows(b"\x01\x02\x03\x04", word_size=1) == []
    assert build_word_rows(b"\x01\x02\x03\x04", word_size=2) == []


def test_render_rows_aligns_columns():
    rows = [
        {"addr": "0x1000", "data": ["0x1", "0x22", "0x333"], "ascii": "abc"},
        {"addr": "0x1008", "data": ["0x4444", "0x5", "0x6"], "ascii": "def"},
    ]
    text = render_rows(rows)
    first, second = text.splitlines()
    assert first.startswith("0x1000")
    assert "|abc|" in first and "|def|" in second
    # equal-width cells keep the columns readable down the page
    assert len(first) == len(second)


def test_to_int_accepts_decimal_and_hex():
    """With output-radix 16, GDB may print numbers either way."""
    assert to_int("42") == 42
    assert to_int("0x2a") == 42
    assert to_int(7) == 7
    assert to_int("not a number") is None
    assert to_int(None) is None


# -- info variables parsing ---------------------------------------------------

SAMPLE_INFO_VARIABLES = """All variables matching regular expression "g_":

File src/server.c:
12:\tstatic int g_connection_count;
18:\tchar g_hostname[256];
25:\tstatic struct config *g_config;

File src/util.c:
7:\tint g_verbose;

Non-debugging symbols:
0x0000000000404060  g_internal_flag
"""


def test_parse_info_variables_extracts_names_and_files():
    found = _parse_info_variables(SAMPLE_INFO_VARIABLES)
    names = [entry["name"] for entry in found]
    assert "g_connection_count" in names
    assert "g_verbose" in names

    by_name = {entry["name"]: entry for entry in found}
    assert by_name["g_connection_count"]["file"] == "src/server.c"
    assert by_name["g_connection_count"]["line"] == 12
    assert by_name["g_verbose"]["file"] == "src/util.c"


def test_parse_info_variables_handles_arrays_and_pointers():
    by_name = {e["name"]: e for e in _parse_info_variables(SAMPLE_INFO_VARIABLES)}
    assert by_name["g_hostname"]["declaration"] == "char g_hostname[256];"
    assert by_name["g_config"]["declaration"] == "static struct config *g_config;"


def test_parse_info_variables_deduplicates():
    text = "File a.c:\n1:\tint dup;\n\nFile b.c:\n2:\tint dup;\n"
    assert len(_parse_info_variables(text)) == 1


def test_parse_info_variables_on_empty_output():
    assert _parse_info_variables("") == []
    assert _parse_info_variables("All variables matching x:\n") == []


# -- VS Code memory reads -----------------------------------------------------


@pytest.fixture
async def bridge(tmp_path, monkeypatch):
    import json
    import os

    from gdb_mcp import vscode_bridge

    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "bridges"))
    os.makedirs(vscode_bridge.BRIDGE_DIR, exist_ok=True)
    fake = FakeBridge()
    await fake.start()
    with open(os.path.join(vscode_bridge.BRIDGE_DIR, f"{os.getpid()}.json"), "w") as fh:
        json.dump(fake.discovery_info(), fh)
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def debugger(bridge):
    from gdb_mcp.vscode_bridge import VSCodeDebugger

    return VSCodeDebugger()


async def test_vsc_memory_renders_rows_not_base64(bridge, debugger):
    payload = b"Hello, world!\x00\xff\x01"
    bridge.add_session()
    bridge.dap_responses["readMemory"] = {
        "address": "0x7ffd0000",
        "data": base64.b64encode(payload).decode(),
    }

    result = await debugger.read_memory("0x7ffd0000", count=2)
    assert "data_base64" not in result
    assert result["bytes_read"] == len(payload)
    assert result["word_size"] == 8
    assert result["rows"][0]["ascii"] == "Hello, world!..."
    assert "|Hello, world!...|" in result["dump"]


async def test_vsc_memory_groups_into_hex_words(bridge, debugger):
    payload = b"".join(v.to_bytes(4, "little") for v in (1, 2, 3, 4))
    bridge.add_session()
    bridge.dap_responses["readMemory"] = {
        "address": "0x1000",
        "data": base64.b64encode(payload).decode(),
    }

    result = await debugger.read_memory("0x1000", count=4, word_size=4)
    words = [w for row in result["rows"] for w in row["data"]]
    assert words == ["0x00000001", "0x00000002", "0x00000003", "0x00000004"]
    # count is in words, so 4 words of 4 bytes must request 16 bytes
    assert dict(bridge.dap_calls)["readMemory"]["count"] == 16


async def test_vsc_memory_defaults_to_giant_words(bridge, debugger):
    bridge.add_session()
    bridge.dap_responses["readMemory"] = {
        "address": "0x1000",
        "data": base64.b64encode(bytes(128)).decode(),
    }
    result = await debugger.read_memory("0x1000")
    assert result["word_size"] == 8
    # 16 words x 8 bytes
    assert dict(bridge.dap_calls)["readMemory"]["count"] == 128


async def test_vsc_memory_rejects_sub_word_sizes(bridge, debugger):
    bridge.add_session()
    with pytest.raises(ValueError, match="word_size must be one of"):
        await debugger.read_memory("0x1000", word_size=1)


async def test_vsc_memory_resolves_an_expression_to_an_address(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 9, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {
        "result": "0x55a4c0 <buffer>",
        "memoryReference": "0x55a4c0",
    }
    bridge.dap_responses["readMemory"] = {
        "address": "0x55a4c0",
        "data": base64.b64encode(b"abcdefgh").decode(),
    }

    result = await debugger.read_memory("&buffer", count=1)
    assert dict(bridge.dap_calls)["readMemory"]["memoryReference"] == "0x55a4c0"
    assert result["expression"] == "&buffer"


async def test_vsc_memory_falls_back_to_the_printed_pointer(bridge, debugger):
    """cppdbg does not always populate memoryReference."""
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 9, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "0x7fff1234 <g_buf>"}
    bridge.dap_responses["readMemory"] = {
        "address": "0x7fff1234",
        "data": base64.b64encode(b"xyzwxyzw").decode(),
    }

    await debugger.read_memory("&g_buf", count=1)
    assert dict(bridge.dap_calls)["readMemory"]["memoryReference"] == "0x7fff1234"


async def test_vsc_memory_unresolvable_expression_is_a_clear_error(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 9, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "42"}

    with pytest.raises(BridgeError, match="could not resolve"):
        await debugger.read_memory("some_int", count=1)


# -- VS Code globals ----------------------------------------------------------


async def test_vsc_globals_lists_names_from_info_variables(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 3, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": SAMPLE_INFO_VARIABLES}

    result = await debugger.list_globals(pattern="g_")
    names = [entry["name"] for entry in result["globals"]]
    assert "g_connection_count" in names
    assert result["count"] == len(result["globals"])
    # it must go through the -exec escape, since DAP has no such request
    assert dict(bridge.dap_calls)["evaluate"]["expression"] == "-exec info variables g_"


async def test_vsc_globals_empty_output_explains_the_console_quirk(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 3, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": ""}

    result = await debugger.list_globals(pattern="nothing")
    assert result["globals"] == []
    assert "Debug Console" in result["note"]


# -- standalone GDB memory and globals ----------------------------------------


@pytest.fixture
async def gdb_debugger(monkeypatch):
    from test_session import FakeGdbSession

    from gdb_mcp.tools import Debugger

    monkeypatch.setattr("gdb_mcp.tools.GdbSession", FakeGdbSession)
    dbg = Debugger()
    await dbg.start()
    try:
        yield dbg
    finally:
        await dbg.shutdown_all()


async def test_gdb_memory_builds_a_row_dump(gdb_debugger):
    result = await gdb_debugger.read_memory("&buf", count=8, word_size=4)
    assert result["requested"]["format"] == "hexadecimal"
    assert result["rows"][0]["data"] == ["0x48", "0x65", "0x6c", "0x6c"]
    assert "|Hell|" in result["dump"]
    assert "|o!..|" in result["dump"]
    assert result["next_page"] == "0x00001008"


async def test_gdb_memory_requests_the_right_geometry(gdb_debugger):
    """count is in words; rows/cols must be derived from word_size."""
    session = gdb_debugger.get("default")
    sent = []
    original = session.send

    async def record(command, **kwargs):
        sent.append(command)
        return await original(command, **kwargs)

    session.send = record
    await gdb_debugger.read_memory("0x1000", count=8, word_size=4)
    command = next(c for c in sent if c.startswith("-data-read-memory "))
    # 8 words of 4 bytes, 4 per row -> hex format, 2 rows x 4 cols, ascii on
    assert command == "-data-read-memory 0x1000 x 4 2 4 46"


async def test_gdb_memory_always_requests_the_ascii_gutter(gdb_debugger):
    """The gutter must not be tied to byte-sized words, which no longer exist."""
    session = gdb_debugger.get("default")
    sent = []
    original = session.send

    async def record(command, **kwargs):
        sent.append(command)
        return await original(command, **kwargs)

    session.send = record
    await gdb_debugger.read_memory("0x1000", count=4, word_size=8)
    command = next(c for c in sent if c.startswith("-data-read-memory "))
    assert command.endswith(" 46")


async def test_gdb_memory_rejects_sub_word_sizes(gdb_debugger):
    for bad in (1, 2, 3):
        with pytest.raises(ValueError, match="word_size must be one of"):
            await gdb_debugger.read_memory("0x1000", word_size=bad)


async def test_gdb_globals_parses_symbol_info(gdb_debugger):
    result = await gdb_debugger.list_globals(pattern="g_")
    names = [entry["name"] for entry in result["globals"]]
    assert names == ["g_connection_count", "g_hostname"]
    first = result["globals"][0]
    assert first["type"] == "int"
    assert first["file"] == "src/server.c"
    assert first["line"] == "12"


async def test_gdb_globals_can_fetch_current_values(gdb_debugger):
    result = await gdb_debugger.list_globals(pattern="g_", include_values=True)
    assert result["globals"][0]["value"] == "42"


async def test_gdb_globals_no_match_mentions_file_statics(gdb_debugger):
    result = await gdb_debugger.list_globals(pattern="nomatch")
    assert result["globals"] == []
    assert "'file.c'::name" in result["note"]
