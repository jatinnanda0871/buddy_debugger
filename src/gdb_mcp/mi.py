"""Parser for GDB/MI output records.

Implements the grammar from the GDB manual, "GDB/MI Output Syntax".  Pure
stdlib so the core has no third-party dependencies.

    output          -> ( out-of-band-record )* [ result-record ] "(gdb)"
    result-record   -> [token] "^" result-class ( "," result )*
    async-record    -> [token] ( "*" | "+" | "=" ) async-class ( "," result )*
    stream-record   -> ( "~" | "@" | "&" ) c-string
    result          -> variable "=" value
    value           -> const | tuple | list
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Record kinds.
RESULT = "result"
EXEC = "exec"
STATUS = "status"
NOTIFY = "notify"
CONSOLE = "console"
TARGET = "target"
LOG = "log"
PROMPT = "prompt"

STREAM_KINDS = (CONSOLE, TARGET, LOG)

_ASYNC_PREFIX = {"*": EXEC, "+": STATUS, "=": NOTIFY}
_STREAM_PREFIX = {"~": CONSOLE, "@": TARGET, "&": LOG}

_VAR_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" "-_."
)
_HEX = frozenset("0123456789abcdefABCDEF")
_OCTAL = frozenset("01234567")

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "e": "\x1b",
    '"': '"',
    "\\": "\\",
    "'": "'",
}


class MIParseError(ValueError):
    """Raised when a line cannot be parsed as GDB/MI output."""


@dataclass
class MIRecord:
    """A single parsed GDB/MI record."""

    kind: str
    #: result-class ("done", "running", "error", ...) or async-class ("stopped", ...)
    message: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    token: int | None = None
    #: decoded text, for stream records only
    text: str | None = None
    raw: str = ""

    @property
    def is_error(self) -> bool:
        return self.kind == RESULT and self.message == "error"

    @property
    def error_message(self) -> str:
        msg = self.payload.get("msg")
        return msg if isinstance(msg, str) else "unknown GDB error"


class _Parser:
    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0

    # -- primitives ---------------------------------------------------------

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise MIParseError(
                f"expected {ch!r} at offset {self.i} in {self.s!r}"
            )
        self.i += 1

    def at_end(self) -> bool:
        return self.i >= len(self.s)

    # -- grammar ------------------------------------------------------------

    def variable(self) -> str:
        start = self.i
        while self.i < len(self.s) and self.s[self.i] in _VAR_CHARS:
            self.i += 1
        if start == self.i:
            raise MIParseError(f"expected variable at offset {self.i} in {self.s!r}")
        return self.s[start : self.i]

    def cstring(self) -> str:
        self.expect('"')
        out: list[str] = []
        while True:
            if self.at_end():
                raise MIParseError(f"unterminated string in {self.s!r}")
            c = self.s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c != "\\":
                out.append(c)
                self.i += 1
                continue

            # escape sequence
            self.i += 1
            if self.at_end():
                raise MIParseError(f"trailing backslash in {self.s!r}")
            e = self.s[self.i]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                self.i += 1
            elif e == "x":
                self.i += 1
                digits = ""
                while len(digits) < 2 and self.peek() in _HEX and self.peek():
                    digits += self.s[self.i]
                    self.i += 1
                out.append(chr(int(digits, 16)) if digits else "x")
            elif e in _OCTAL:
                digits = ""
                while len(digits) < 3 and self.peek() in _OCTAL and self.peek():
                    digits += self.s[self.i]
                    self.i += 1
                out.append(chr(int(digits, 8)))
            else:
                # unknown escape: keep the character verbatim
                out.append(e)
                self.i += 1

    def value(self) -> Any:
        c = self.peek()
        if c == '"':
            return self.cstring()
        if c == "{":
            return self.tuple_()
        if c == "[":
            return self.list_()
        raise MIParseError(f"expected value at offset {self.i} in {self.s!r}")

    def result(self) -> tuple[str, Any]:
        name = self.variable()
        self.expect("=")
        return name, self.value()

    def _looks_like_result(self) -> bool:
        """True if a `variable=` starts at the cursor (lookahead, no consume)."""
        j = self.i
        while j < len(self.s) and self.s[j] in _VAR_CHARS:
            j += 1
        return j > self.i and j < len(self.s) and self.s[j] == "="

    def tuple_(self) -> dict[str, Any]:
        self.expect("{")
        out: dict[str, Any] = {}
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            name, val = self.result()
            _add(out, name, val)
            if self.peek() == ",":
                self.i += 1
                continue
            break
        self.expect("}")
        return out

    def list_(self) -> list[Any]:
        self.expect("[")
        out: list[Any] = []
        if self.peek() == "]":
            self.i += 1
            return out
        while True:
            # A list holds either bare values or `name=value` results; for the
            # latter GDB repeats the same key (frame=,frame=), so the keys carry
            # no information and we keep only the values.
            if self._looks_like_result():
                _name, val = self.result()
                out.append(val)
            else:
                out.append(self.value())
            if self.peek() == ",":
                self.i += 1
                continue
            break
        self.expect("]")
        return out

    def results_to_end(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self.peek() == ",":
            self.i += 1
            name, val = self.result()
            _add(out, name, val)
        return out


class _FoldedList(list):
    """Marker type: a key that GDB repeated within one tuple."""


def _add(out: dict[str, Any], name: str, val: Any) -> None:
    """Insert into a tuple, folding repeated keys into a list."""
    if name not in out:
        out[name] = val
        return
    existing = out[name]
    if isinstance(existing, _FoldedList):
        existing.append(val)
        return
    out[name] = _FoldedList([existing, val])


def parse_line(line: str) -> MIRecord | None:
    """Parse one line of GDB/MI output.

    Returns ``None`` for blank lines and for lines that are not valid MI (raw
    inferior output can leak into the stream on some targets); callers should
    treat ``None`` as "not a protocol record".
    """
    raw = line.rstrip("\r\n")
    if not raw:
        return None
    if raw.strip() in ("(gdb)", "(gdb) "):
        return MIRecord(kind=PROMPT, raw=raw)

    p = _Parser(raw)

    # optional token
    start = p.i
    while p.peek().isdigit():
        p.i += 1
    token = int(raw[start : p.i]) if p.i > start else None

    c = p.peek()
    if c == "^":
        p.i += 1
        try:
            message = p.variable()
            payload = p.results_to_end()
        except MIParseError:
            return None
        return MIRecord(RESULT, message, payload, token, raw=raw)

    if c in _ASYNC_PREFIX:
        p.i += 1
        try:
            message = p.variable()
            payload = p.results_to_end()
        except MIParseError:
            return None
        return MIRecord(_ASYNC_PREFIX[c], message, payload, token, raw=raw)

    if c in _STREAM_PREFIX:
        p.i += 1
        try:
            text = p.cstring()
        except MIParseError:
            return None
        return MIRecord(_STREAM_PREFIX[c], text=text, raw=raw)

    return None
