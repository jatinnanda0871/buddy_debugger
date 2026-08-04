"""Parser tests using real GDB/MI output captured from gdb 12/13."""

import pytest

from gdb_mcp import mi
from gdb_mcp.mi import parse_line


def test_prompt():
    assert parse_line("(gdb)").kind == mi.PROMPT
    assert parse_line("(gdb) \n").kind == mi.PROMPT


def test_blank_and_garbage():
    assert parse_line("") is None
    assert parse_line("\n") is None
    # raw inferior output leaking into the stream must not raise
    assert parse_line("hello from the program") is None


def test_simple_done():
    r = parse_line("^done")
    assert r.kind == mi.RESULT
    assert r.message == "done"
    assert r.payload == {}
    assert r.token is None


def test_token_is_parsed():
    r = parse_line('42^done,value="7"')
    assert r.token == 42
    assert r.payload == {"value": "7"}


def test_error_record():
    r = parse_line('7^error,msg="No symbol \\"foo\\" in current context."')
    assert r.is_error
    assert r.error_message == 'No symbol "foo" in current context.'


def test_nested_tuple():
    line = (
        '^done,bkpt={number="1",type="breakpoint",disp="keep",enabled="y",'
        'addr="0x000000000040113a",func="main",file="t.c",'
        'fullname="/home/j/t.c",line="5",thread-groups=["i1"],times="0",'
        'original-location="main"}'
    )
    r = parse_line(line)
    bkpt = r.payload["bkpt"]
    assert bkpt["number"] == "1"
    assert bkpt["func"] == "main"
    assert bkpt["thread-groups"] == ["i1"]


def test_list_of_repeated_results_keeps_values():
    line = (
        '^done,stack=[frame={level="0",addr="0x40113a",func="inner",'
        'file="t.c",line="5"},frame={level="1",addr="0x401150",'
        'func="outer",file="t.c",line="11"}]'
    )
    r = parse_line(line)
    stack = r.payload["stack"]
    assert isinstance(stack, list) and len(stack) == 2
    assert stack[0]["func"] == "inner"
    assert stack[1]["level"] == "1"


def test_empty_containers():
    r = parse_line("^done,a=[],b={}")
    assert r.payload == {"a": [], "b": {}}


def test_stopped_async_record():
    line = (
        '*stopped,reason="breakpoint-hit",disp="keep",bkptno="1",'
        'frame={addr="0x40113a",func="main",args=[{name="argc",value="1"}],'
        'file="t.c",fullname="/home/j/t.c",line="5"},'
        'thread-id="1",stopped-threads="all",core="3"'
    )
    r = parse_line(line)
    assert r.kind == mi.EXEC
    assert r.message == "stopped"
    assert r.payload["reason"] == "breakpoint-hit"
    assert r.payload["frame"]["args"][0]["name"] == "argc"
    assert r.payload["thread-id"] == "1"


def test_exited_record():
    r = parse_line('*stopped,reason="exited-normally"')
    assert r.payload["reason"] == "exited-normally"

    r = parse_line('*stopped,reason="exited",exit-code="01"')
    assert r.payload["exit-code"] == "01"


def test_notify_record():
    r = parse_line('=thread-group-exited,id="i1",exit-code="0"')
    assert r.kind == mi.NOTIFY
    assert r.message == "thread-group-exited"


def test_running_record():
    r = parse_line("*running,thread-id=\"all\"")
    assert r.kind == mi.EXEC and r.message == "running"


@pytest.mark.parametrize(
    "raw,kind",
    [
        ('~"Breakpoint 1 at 0x40113a\\n"', mi.CONSOLE),
        ('@"program output\\n"', mi.TARGET),
        ('&"info locals\\n"', mi.LOG),
    ],
)
def test_stream_records(raw, kind):
    r = parse_line(raw)
    assert r.kind == kind
    assert r.text.endswith("\n")


def test_escape_sequences():
    r = parse_line(r'~"tab\there\nquote\"done\\slash"')
    assert r.text == 'tab\there\nquote"done\\slash'


def test_octal_and_hex_escapes():
    assert parse_line(r'~"\101\102"').text == "AB"
    assert parse_line(r'~"\x41"').text == "A"


def test_repeated_key_in_tuple_folds_to_list():
    r = parse_line('^done,x={a="1",a="2",a="3"}')
    assert r.payload["x"]["a"] == ["1", "2", "3"]


def test_value_containing_braces_and_commas():
    # GDB embeds struct-ish text inside a quoted value; commas there must not
    # be treated as separators.
    r = parse_line('^done,value="{x = 1, y = {2, 3}}"')
    assert r.payload["value"] == "{x = 1, y = {2, 3}}"


def test_register_values_list():
    line = '^done,register-values=[{number="0",value="0x1"},{number="1",value="0x2"}]'
    r = parse_line(line)
    vals = r.payload["register-values"]
    assert [v["value"] for v in vals] == ["0x1", "0x2"]


def test_breakpoint_table():
    line = (
        '^done,BreakpointTable={nr_rows="1",nr_cols="6",'
        'hdr=[{width="7",alignment="-1",col_name="number",colhdr="Num"}],'
        'body=[bkpt={number="1",type="breakpoint",func="main",times="1"}]}'
    )
    r = parse_line(line)
    body = r.payload["BreakpointTable"]["body"]
    assert body[0]["number"] == "1"
    assert body[0]["times"] == "1"


def test_truncated_line_returns_none_not_raise():
    assert parse_line('^done,bkpt={number="1"') is None
    assert parse_line('~"unterminated') is None
