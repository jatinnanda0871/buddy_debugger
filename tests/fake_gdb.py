"""A minimal fake `gdb --interpreter=mi` for testing the session layer.

Speaks just enough GDB/MI to exercise token correlation, console-output
capture, and the asynchronous `*stopped` handshake -- including the case that
matters most: an execution command that answers `^running` immediately and only
later emits `*stopped`.

Run as: python fake_gdb.py --interpreter=mi3 -q -nx
"""

import re
import sys
import threading
import time

TOKEN_RE = re.compile(r"^(\d*)(.*)$", re.S)


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def prompt() -> None:
    emit("(gdb) ")


def delayed_stop(delay: float, payload: str) -> None:
    def run() -> None:
        time.sleep(delay)
        emit(payload)
        prompt()

    threading.Thread(target=run, daemon=True).start()


STOP_AT_MAIN = (
    '*stopped,reason="breakpoint-hit",disp="del",bkptno="1",'
    'frame={addr="0x40113a",func="main",args=[],file="t.c",'
    'fullname="/tmp/t.c",line="5"},thread-id="1",stopped-threads="all"'
)


#: Bumped on every step, so -stack-list-variables can report a local that
#: actually changes across stops -- needed to exercise locals-diffing.
_step_count = 0

#: True once `record` has run and until `record stop` does -- reverse
#: execution commands only succeed while this is set, same as real gdb
#: without an active recording. `record` has no MI form (confirmed against
#: real gdb 15.1), so it arrives here as a console command, same as any
#: other CLI-only command.
_recording = False

#: The one breakpoint -break-insert creates, mutated in place by
#: -break-condition / -break-after so -break-list can reflect edits --
#: needed to exercise gdb_modify_breakpoint without a real gdb.
_breakpoint: dict[str, str] = {}


def _reset_breakpoint() -> None:
    _breakpoint.clear()
    _breakpoint.update(
        number="1",
        type="breakpoint",
        disp="keep",
        enabled="y",
        addr="0x40113a",
        func="main",
        file="t.c",
        line="5",
        times="0",
    )


def _format_bkpt(fields: dict) -> str:
    return ",".join(f'{k}="{v}"' for k, v in fields.items())


def main() -> int:
    global _step_count, _recording
    if "--interpreter=mi3" not in sys.argv and "--interpreter=mi2" not in sys.argv:
        sys.stderr.write("fake_gdb: expected --interpreter=mi2|mi3\n")
        return 1

    prompt()  # initial readiness prompt

    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        m = TOKEN_RE.match(line)
        token, command = m.group(1), m.group(2)

        if command == "-gdb-exit":
            emit(f"{token}^exit")
            return 0

        if command.startswith("-gdb-set") or command == "-enable-pretty-printing":
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith("-file-exec-and-symbols"):
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith("-break-insert"):
            _reset_breakpoint()
            emit(f"{token}^done,bkpt={{{_format_bkpt(_breakpoint)}}}")
            prompt()
            continue

        if command.startswith("-break-condition"):
            # "-break-condition NUMBER EXPR..." or bare "-break-condition NUMBER"
            rest = command[len("-break-condition") :].strip().split(" ", 1)
            expr = rest[1] if len(rest) > 1 else ""
            if expr:
                _breakpoint["cond"] = expr
            else:
                _breakpoint.pop("cond", None)
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith("-break-after"):
            parts = command[len("-break-after") :].strip().split()
            if len(parts) >= 2:
                _breakpoint["ignore"] = parts[1]
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith("-break-list"):
            body = f"bkpt={{{_format_bkpt(_breakpoint)}}}" if _breakpoint else ""
            emit(f'{token}^done,BreakpointTable={{nr_rows="1",body=[{body}]}}')
            prompt()
            continue

        if command.startswith("-break-enable"):
            _breakpoint["enabled"] = "y"
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith("-break-disable"):
            _breakpoint["enabled"] = "n"
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith("-data-disassemble"):
            emit(
                f'{token}^done,asm_insns=[{{address="0x40113a",func-name="main",'
                f'offset="0",inst="mov eax,0x0"}},{{address="0x40113e",'
                f'func-name="main",offset="4",inst="ret"}}]'
            )
            prompt()
            continue

        if command.startswith("-exec-run"):
            emit(f"{token}^running")
            emit('*running,thread-id="all"')
            prompt()
            delayed_stop(0.05, STOP_AT_MAIN)
            continue

        if command.startswith('-interpreter-exec console "record stop'):
            _recording = False
            emit(f"{token}^done")
            prompt()
            continue

        if command.startswith('-interpreter-exec console "record'):
            _recording = True
            emit(f"{token}^done")
            prompt()
            continue

        if "--reverse" in command:
            if not _recording:
                emit(
                    f'{token}^error,msg="Target does not support this command '
                    'without a process record."'
                )
                prompt()
                continue
            emit(f"{token}^running")
            prompt()
            delayed_stop(
                0.02,
                '*stopped,reason="end-stepping-range",'
                'frame={addr="0x401130",func="main",file="t.c",line="4"},'
                'thread-id="1"',
            )
            continue

        if command.startswith("-exec-continue"):
            emit(f"{token}^running")
            emit('*running,thread-id="all"')
            prompt()
            # Long-running: only stops when interrupted.
            continue

        if command.startswith("-exec-interrupt"):
            emit(f"{token}^done")
            prompt()
            delayed_stop(
                0.05,
                '*stopped,reason="signal-received",signal-name="SIGINT",'
                'frame={addr="0x40115a",func="loop",file="t.c",line="9"},'
                'thread-id="1"',
            )
            continue

        if command.startswith("-exec-step") or command.startswith("-exec-next"):
            _step_count += 1
            emit(f"{token}^running")
            prompt()
            delayed_stop(
                0.02,
                '*stopped,reason="end-stepping-range",'
                'frame={addr="0x40114a",func="main",file="t.c",line="6"},'
                'thread-id="1"',
            )
            continue

        if command.startswith("-stack-list-variables"):
            emit(
                f'{token}^done,variables=[{{name="i",value="{_step_count}"}},'
                f'{{name="total",value="42"}}]'
            )
            prompt()
            continue

        if command.startswith("-stack-list-frames"):
            emit(
                f'{token}^done,stack=[frame={{level="0",addr="0x40113a",'
                f'func="main",file="t.c",line="5"}}]'
            )
            prompt()
            continue

        if command.startswith("-data-read-memory-bytes"):
            emit(
                f'{token}^done,memory=[{{begin="0x1000",offset="0x0",'
                f'end="0x1008",contents="48656c6c6f210001"}}]'
            )
            prompt()
            continue

        if command.startswith("-data-read-memory"):
            emit(
                f'{token}^done,addr="0x00001000",nr-bytes="8",total-bytes="8",'
                f'next-row="0x00001008",prev-row="0x00000ff8",'
                f'next-page="0x00001008",prev-page="0x00000ff8",'
                f'memory=[{{addr="0x00001000",data=["0x48","0x65","0x6c","0x6c"],'
                f'ascii="Hell"}},{{addr="0x00001004",'
                f'data=["0x6f","0x21","0x00","0x01"],ascii="o!.."}}]'
            )
            prompt()
            continue

        if command.startswith("-symbol-info-variables"):
            if "nomatch" in command:
                emit(f"{token}^done,symbols={{debug=[]}}")
            else:
                emit(
                    f'{token}^done,symbols={{debug=[{{filename="src/server.c",'
                    f'fullname="/proj/src/server.c",symbols=[{{line="12",'
                    f'name="g_connection_count",type="int",'
                    f'description="static int g_connection_count;"}},'
                    f'{{line="18",name="g_hostname",type="char [256]",'
                    f'description="char g_hostname[256];"}}]}}]}}'
                )
            prompt()
            continue

        if command.startswith("-data-evaluate-expression"):
            if "boom" in command:
                emit(f'{token}^error,msg="No symbol \\"boom\\" in current context."')
            else:
                emit(f'{token}^done,value="42"')
            prompt()
            continue

        if command.startswith("-interpreter-exec console"):
            emit('~"console line one\\n"')
            emit('~"console line two\\n"')
            emit('&"log noise\\n"')
            emit(f"{token}^done")
            prompt()
            continue

        emit(f'{token}^error,msg="Undefined MI command: {command}"')
        prompt()

    return 0


if __name__ == "__main__":
    sys.exit(main())
