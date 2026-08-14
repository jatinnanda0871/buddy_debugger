"""Structural + negative tests for the operation-dispatch schema machinery.

Data-driven over server.py's own `*_OPS` tables rather than hand-enumerated,
so every operation across all 7 tools gets required-field and
unknown-operation coverage automatically -- adding or changing an operation
in an OPS table gets these checks for free, instead of relying on someone
remembering to add a matching test.

These validate the schema directly with `jsonschema` (fast, no MCP session);
`test_server_protocol.py` covers the same shapes end-to-end through the real
`call_tool()` handler.
"""

from __future__ import annotations

import jsonschema
import pytest

from gdb_mcp.server import (
    ALL_TOOLS,
    GDB_BREAKPOINT_OPS,
    GDB_EXEC_OPS,
    GDB_INSPECT_OPS,
    GDB_SESSION_OPS,
    TOOLS,
    VSC_EXEC_OPS,
    VSC_INSPECT_OPS,
    VSC_SESSION_OPS,
    VSCODE_TOOLS,
    _pop_operation,
    select_tools,
)

TOOL_OPS = {
    "gdb_session": GDB_SESSION_OPS,
    "gdb_exec": GDB_EXEC_OPS,
    "gdb_breakpoint": GDB_BREAKPOINT_OPS,
    "gdb_inspect": GDB_INSPECT_OPS,
    "vsc_session": VSC_SESSION_OPS,
    "vsc_exec": VSC_EXEC_OPS,
    "vsc_inspect": VSC_INSPECT_OPS,
}
SCHEMAS = {t.name: t.inputSchema for t in ALL_TOOLS}

#: (tool, operation) for every operation of every tool.
ALL_OPERATIONS = [(t, op) for t, ops in TOOL_OPS.items() for op in ops]
#: (tool, operation, required_field) for every required field of every operation.
ALL_REQUIRED_FIELDS = [
    (t, op, field)
    for t, ops in TOOL_OPS.items()
    for op, spec in ops.items()
    for field in spec.get("required", [])
]


def _placeholder_for(field_schema: dict) -> object:
    """A minimally valid value for a field's JSON schema, for building probe instances."""
    if "enum" in field_schema:
        return field_schema["enum"][0]
    return {"string": "x", "integer": 1, "number": 1.0, "boolean": True, "array": []}.get(
        field_schema.get("type"), "x"
    )


# -- shape of the 7 tools themselves ------------------------------------------


def test_exactly_seven_tools():
    assert len(ALL_TOOLS) == 7
    assert len(TOOLS) == 4
    assert len(VSCODE_TOOLS) == 3


def test_every_tool_name_matches_a_known_ops_table():
    assert {t.name for t in ALL_TOOLS} == set(TOOL_OPS)


@pytest.mark.parametrize("tool_name,ops", TOOL_OPS.items())
def test_operation_enum_matches_the_ops_table_exactly(tool_name, ops):
    schema = SCHEMAS[tool_name]
    assert set(schema["properties"]["operation"]["enum"]) == set(ops)


@pytest.mark.parametrize("tool_name,ops", TOOL_OPS.items())
def test_one_allof_branch_per_operation(tool_name, ops):
    assert len(SCHEMAS[tool_name]["allOf"]) == len(ops)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_gdb_tools_take_a_session(tool):
    assert "session" in tool.inputSchema["properties"]


@pytest.mark.parametrize("tool", VSCODE_TOOLS, ids=lambda t: t.name)
def test_vsc_tools_do_not_take_a_session(tool):
    """There is only one VS Code session; a `session` field would be misleading."""
    assert "session" not in tool.inputSchema["properties"]


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.name)
def test_no_tool_restricts_additional_properties(tool):
    """Pins the deliberate choice: unknown args reach dispatch and become a
    clean TypeError, rather than a schema-level rejection with a vaguer
    message (see test_unexpected_argument_does_not_crash_the_server)."""
    assert "additionalProperties" not in tool.inputSchema


# -- positive: minimal valid calls --------------------------------------------


@pytest.mark.parametrize("tool_name,operation", ALL_OPERATIONS)
def test_operation_with_only_its_required_fields_is_accepted(tool_name, operation):
    schema = SCHEMAS[tool_name]
    spec = TOOL_OPS[tool_name][operation]
    instance = {"operation": operation}
    for field in spec.get("required", []):
        instance[field] = _placeholder_for(schema["properties"][field])
    jsonschema.validate(instance=instance, schema=schema)  # must not raise


# -- negative: the actual point of this file ----------------------------------


@pytest.mark.parametrize("tool_name,operation,missing_field", ALL_REQUIRED_FIELDS)
def test_missing_a_required_field_is_rejected(tool_name, operation, missing_field):
    schema = SCHEMAS[tool_name]
    spec = TOOL_OPS[tool_name][operation]
    instance = {"operation": operation}
    for field in spec.get("required", []):
        if field != missing_field:
            instance[field] = _placeholder_for(schema["properties"][field])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=instance, schema=schema)


@pytest.mark.parametrize("tool_name", TOOL_OPS)
def test_unknown_operation_value_is_rejected(tool_name):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"operation": "__bogus__"}, schema=SCHEMAS[tool_name])


@pytest.mark.parametrize("tool_name", TOOL_OPS)
def test_missing_operation_entirely_is_rejected(tool_name):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={}, schema=SCHEMAS[tool_name])


@pytest.mark.parametrize("tool_name", TOOL_OPS)
def test_operation_as_wrong_type_is_rejected(tool_name):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"operation": 1}, schema=SCHEMAS[tool_name])


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_session_as_wrong_type_is_rejected(tool):
    ops = TOOL_OPS[tool.name]
    any_operation = next(iter(ops))
    instance = {"operation": any_operation, "session": 123}
    for field in ops[any_operation].get("required", []):
        instance[field] = _placeholder_for(tool.inputSchema["properties"][field])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=instance, schema=tool.inputSchema)


# a couple of fields with real type constraints, spot-checked across tools
# (not exhaustive -- ALL_OPERATIONS already proves every field's *presence*
# requirement; this proves a few representative fields' *type* requirement)
TYPED_FIELD_CASES = [
    ("gdb_breakpoint", "delete", "number", "not-a-number"),
    ("gdb_inspect", "memory", "word_size", 1),  # enum is [4, 8]
    ("gdb_exec", "run", "stop_at_main", "yes"),  # boolean expected
    ("vsc_inspect", "set_breakpoints", "breakpoints", "not-an-array"),
]


@pytest.mark.parametrize("tool_name,operation,field,bad_value", TYPED_FIELD_CASES)
def test_wrong_field_type_is_rejected(tool_name, operation, field, bad_value):
    schema = SCHEMAS[tool_name]
    spec = TOOL_OPS[tool_name][operation]
    instance = {"operation": operation}
    for req_field in spec.get("required", []):
        instance[req_field] = _placeholder_for(schema["properties"][req_field])
    instance[field] = bad_value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=instance, schema=schema)


# -- select_tools() ------------------------------------------------------------


@pytest.mark.parametrize(
    "which,expected",
    [
        ("gdb", TOOLS),
        ("standalone", TOOLS),
        ("vscode", VSCODE_TOOLS),
        ("vsc", VSCODE_TOOLS),
        ("all", ALL_TOOLS),
        ("", ALL_TOOLS),
        (None, ALL_TOOLS),
        ("__bogus__", ALL_TOOLS),
        ("GDB", TOOLS),
        ("  vscode  ", VSCODE_TOOLS),
    ],
)
def test_select_tools(which, expected):
    assert select_tools(which) == expected


def test_select_tools_returns_a_copy_not_the_shared_list():
    """Callers mutating the returned list must not corrupt the module's own
    TOOLS/VSCODE_TOOLS/ALL_TOOLS lists for the next caller."""
    result = select_tools("gdb")
    result.clear()
    assert len(TOOLS) == 4


# -- _pop_operation() -----------------------------------------------------------


def test_pop_operation_returns_and_removes_it():
    args = {"operation": "start", "x": 1}
    assert _pop_operation("gdb_session", args) == "start"
    assert args == {"x": 1}


@pytest.mark.parametrize("args", [{}, {"operation": ""}, {"operation": None}])
def test_pop_operation_missing_or_empty_raises_valueerror(args):
    with pytest.raises(ValueError, match="operation"):
        _pop_operation("gdb_session", dict(args))
