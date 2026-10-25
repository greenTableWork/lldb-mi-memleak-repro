#!/usr/bin/env python3
"""Bounded, real-process macOS lldb-mi allocation and varobj lifecycle probe."""

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time


class Usage(ctypes.Structure):
    # macOS SDK sys/resource.h, rusage_info_v2.
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
            "pageins", "wired_size", "resident_size", "phys_footprint",
            "proc_start_abstime", "proc_exit_abstime", "child_user_time",
            "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups",
            "child_pageins", "child_elapsed_abstime", "diskio_bytesread",
            "diskio_byteswritten",
        )
    ]


libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
libproc.proc_pid_rusage.restype = ctypes.c_int


def usage(pid):
    data = Usage()
    if libproc.proc_pid_rusage(pid, 2, ctypes.byref(data)):
        raise OSError(ctypes.get_errno(), "proc_pid_rusage failed")
    return {"rss_bytes": data.resident_size,
            "footprint_bytes": data.phys_footprint,
            "start_abstime": data.proc_start_abstime}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lldb-mi", required=True, type=Path)
    parser.add_argument("--output", type=Path,
                        help="new evidence directory; defaults to a temporary directory")
    parser.add_argument("--count", type=int, default=8192)
    parser.add_argument("--mode", choices=("create", "lifecycle", "update", "contracts"),
                        default="create")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--expression", default="payload",
                        help="fixture expression for create/lifecycle probes")
    parser.add_argument("--stack-logging", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--expect-correct", action="store_true",
                        help="fail on orphan children or missing update notifications")
    parser.add_argument("--max-mib", type=int, default=512)
    parser.add_argument("--max-seconds", type=int, default=90)
    args = parser.parse_args()
    if not 1 <= args.count <= 65536 or not 1 <= args.cycles <= 1000:
        parser.error("count must be 1..65536 and cycles 1..1000")
    if not 64 <= args.max_mib <= 1024 or not 10 <= args.max_seconds <= 300:
        parser.error("max-mib must be 64..1024 and max-seconds 10..300")
    binary = args.lldb_mi.expanduser().resolve(strict=True)
    if not os.access(binary, os.X_OK):
        parser.error("lldb-mi must be executable; use an executable copy if needed")
    out = (args.output.resolve() if args.output else
           Path(tempfile.mkdtemp(prefix="lldb-mi-memory-")) / "run")
    out.mkdir(parents=True, exist_ok=False)
    source = out / "fixture.cpp"
    fixture_name = "contracts.cpp" if args.mode == "contracts" else "fixture.cpp"
    source.write_bytes(Path(__file__).with_name(fixture_name).read_bytes())
    fixture = out / "fixture"
    compile_cmd = ["xcrun", "clang++", "-O0", "-g", "-std=c++17",
                   f"-DMEMBER_COUNT={args.count}", str(source), "-o", str(fixture)]
    subprocess.run(compile_cmd, check=True, timeout=30)
    break_line = next(i for i, text in enumerate(source.read_text().splitlines(), 1)
                      if "// BREAK_HERE" in text)
    env = os.environ.copy()
    for key in ("MallocStackLogging", "MallocStackLoggingNoCompact",
                "MallocStackLoggingDirectory"):
        env.pop(key, None)
    if args.stack_logging:
        env["MallocStackLogging"] = "1"
        env["MallocStackLoggingDirectory"] = str(out)
    report = {
        "binary": str(binary),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "compile": compile_cmd,
        "fixture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "count": args.count,
        "mode": args.mode,
        "expression": args.expression,
        "cycles": args.cycles,
        "stack_logging": args.stack_logging,
        "limits": {"mib": args.max_mib, "seconds": args.max_seconds},
        "checkpoints": [], "commands": [], "diagnostics": [],
    }
    err = (out / "stderr.txt").open("wb")
    transcript = (out / "mi-transcript.txt").open("wb")
    child = subprocess.Popen([str(binary), "--interpreter=mi"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=err, env=env,
                             start_new_session=True, bufsize=0)
    report["pid"] = child.pid
    started = time.monotonic()
    done = threading.Event()
    guard_reason = []

    def kill_group():
        if child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # A system debugserver may remain in the group after the debugger
            # exits. The debugger and recorded inferior are sufficient here.
            if child.poll() is None:
                child.kill()

    def guard():
        with (out / "memory.jsonl").open("w") as log:
            while not done.wait(0.05):
                if child.poll() is not None:
                    return
                try:
                    row = {"elapsed_s": round(time.monotonic() - started, 3),
                           **usage(child.pid)}
                    log.write(json.dumps(row) + "\n")
                    log.flush()
                    reason = None
                    if max(row["rss_bytes"], row["footprint_bytes"]) > args.max_mib * 2**20:
                        reason = "memory limit"
                    elif row["elapsed_s"] > args.max_seconds:
                        reason = "wall clock limit"
                    if reason:
                        guard_reason.append(reason)
                        kill_group()
                        return
                except OSError as exc:
                    # The process can disappear between poll() and the rusage
                    # call during normal exit. ESRCH is not a watchdog fault.
                    if exc.errno == errno.ESRCH:
                        return
                    if child.poll() is None:
                        guard_reason.append(str(exc))
                        kill_group()
                    return

    watcher = threading.Thread(target=guard, daemon=True)
    watcher.start()
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    buffer = bytearray()
    pending = []
    inferiors = {}
    token = 0

    def wait_for(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, line in enumerate(pending):
                if predicate(line):
                    return pending.pop(i)
            if not selector.select(timeout=0.1):
                if child.poll() is not None:
                    raise RuntimeError(f"debugger exited: {child.returncode}; {guard_reason}")
                continue
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(f"debugger closed stdout; {guard_reason}")
            transcript.write(chunk)
            transcript.flush()
            buffer.extend(chunk)
            if len(buffer) > 4 * 2**20:
                raise RuntimeError("MI line exceeded 4 MiB")
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                decoded = line.decode(errors="replace").rstrip("\r")
                match = re.match(r'=thread-group-started,.*pid="(\d+)"', decoded)
                if match:
                    pid = int(match.group(1))
                    inferiors[pid] = usage(pid)["start_abstime"]
                if decoded.startswith(("*stopped", "*running")) or re.match(r"\d+\^", decoded):
                    pending.append(decoded)
            if len(pending) > 1000:
                raise RuntimeError("too many unconsumed MI records")
        raise TimeoutError("MI response timed out")

    def command(text, allow_error=False):
        nonlocal token
        token += 1
        begin = time.monotonic()
        transcript.write(f"> {token}{text}\n".encode())
        transcript.flush()
        child.stdin.write(f"{token}{text}\n".encode())
        result = wait_for(lambda line: line.startswith(f"{token}^"))
        report["commands"].append({"command": text, "result": result,
                                   "elapsed_s": round(time.monotonic() - begin, 6)})
        if "^error" in result and not allow_error:
            raise RuntimeError(result)
        return result

    def checkpoint(label):
        row = {"label": label, "elapsed_s": round(time.monotonic() - started, 3),
               **usage(child.pid)}
        report["checkpoints"].append(row)
        print(json.dumps(row), flush=True)

    def diagnostics(label):
        if not args.diagnostics:
            return
        commands = [
            ("vmmap", ["/usr/bin/vmmap", "-summary", str(child.pid)]),
            ("heap", ["/usr/bin/heap", "-s", str(child.pid)]),
        ]
        if args.stack_logging:
            commands.append(("malloc_history", ["/usr/bin/malloc_history",
                                               str(child.pid), "-allBySize"]))
        for name, cmd in commands:
            path = out / f"{label}-{name}.txt"
            try:
                with path.open("wb") as handle:
                    result = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT,
                                            timeout=15)
                report["diagnostics"].append({"command": cmd, "path": path.name,
                                               "returncode": result.returncode})
            except subprocess.TimeoutExpired:
                report["diagnostics"].append({"command": cmd, "error": "timeout"})

    failed = False
    try:
        # Disable inherited malloc logging in the inferior; measure the debugger.
        command('-interpreter-exec console "settings set target.inherit-env false"')
        command("-file-exec-and-symbols " + json.dumps(str(fixture)))
        command("-break-insert " + json.dumps(f"{source}:{break_line}"))
        command("-exec-run")
        stop = wait_for(lambda line: line.startswith("*stopped"))
        report["initial_stop"] = stop
        if 'reason="breakpoint-hit"' not in stop:
            raise RuntimeError("fixture did not stop at the requested breakpoint")
        checkpoint("after_stop")
        diagnostics("before")
        checkpoint("before_create")
        if args.mode == "create":
            command("-var-create root * " + json.dumps(args.expression))
            checkpoint("after_create")
            diagnostics("after")
            command("-var-delete root")
            checkpoint("after_delete")
            diagnostics("deleted")
        elif args.mode == "lifecycle":
            stale = []
            deleted_counts = []
            for cycle in range(args.cycles):
                name = f"root{cycle}"
                command(f"-var-create {name} * " + json.dumps(args.expression))
                result = command(f"-var-list-children 0 {name} 0 1")
                child_name = re.search(r'child=\{name="([^"]+)"', result).group(1)
                command(f"-var-list-children 0 {child_name} 0 2")
                result = command(f"-var-delete {name}")
                match = re.search(r'ndeleted="(\d+)"', result)
                deleted_counts.append(int(match.group(1)) if match else None)
                result = command(f"-var-evaluate-expression {child_name}", allow_error=True)
                stale.append("^done" in result)
                if (cycle + 1) % 100 == 0:
                    checkpoint(f"after_cycle_{cycle + 1}")
            report["deleted_parent_child_still_accessible"] = stale
            report["deleted_counts"] = deleted_counts
            checkpoint("after_cycles")
            diagnostics("after")
        elif args.mode == "contracts":
            from contracts import run
            report["checks"] = run(command, wait_for, source, args.count)
            checkpoint("after_contracts")
            diagnostics("after")
        else:
            second_line = next(
                i for i, text in enumerate(source.read_text().splitlines(), 1)
                if "// BREAK_AGAIN" in text)
            command("-break-insert " + json.dumps(f"{source}:{second_line}"))
            command("-var-create root * payload")
            command("-var-list-children 0 root 0 1")
            command("-var-list-children 0 root.items 0 1")
            command("-var-list-children 1 root.items.[0] 0 2")
            command("-exec-continue")
            stop = wait_for(lambda line: line.startswith("*stopped"))
            if 'reason="breakpoint-hit"' not in stop:
                raise RuntimeError("fixture missed its second breakpoint")
            report["update_parent_result"] = command("-var-update 1 root")
            report["updated_leaf_value"] = command(
                "-var-evaluate-expression root.items.[0].first")
            report["unchanged_updates"] = [
                command("-var-update 1 root") for _ in range(args.cycles)]
            checkpoint("after_update")
            diagnostics("after")
        command("-gdb-exit")
        child.wait(timeout=5)
        if child.returncode != 0:
            raise RuntimeError(f"debugger exited with status {child.returncode}")
        failures = []
        if any(report.get("deleted_parent_child_still_accessible", [])):
            failures.append("children remain accessible after parent deletion")
        if args.mode == "lifecycle" and any(
                n != 2 + min(args.count, 2) for n in report["deleted_counts"]):
            failures.append("deletion count does not include all registered descendants")
        if args.mode == "update":
            result = report["update_parent_result"]
            if 'name="root.items.[0].first",value="43"' not in result:
                failures.append("parent update omitted the changed registered leaf")
            if any("changelist=[]" not in r for r in report["unchanged_updates"]):
                failures.append("unchanged update repeats a notification")
        failures.extend(check["check"] for check in report.get("checks", [])
                        if not check["passed"])
        report["contract_failures"] = failures
        if args.expect_correct and failures:
            failed = True
    except (RuntimeError, TimeoutError, subprocess.TimeoutExpired, OSError) as exc:
        failed = True
        report["error"] = str(exc)
    finally:
        kill_group()
        child.wait(timeout=5)
        # A debugserver can place the inferior in another process group.
        # Kill only a recorded fixture PID with the same process identity.
        for pid, start_abstime in inferiors.items():
            try:
                if usage(pid)["start_abstime"] == start_abstime:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        done.set()
        watcher.join(timeout=1)
        selector.close()
        child.stdin.close()
        child.stdout.close()
        transcript.close()
        err.close()
        report["guard_reason"] = guard_reason
        report["inferior_pids"] = list(inferiors)
        report["exitcode"] = child.returncode
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"output": str(out), "error": report.get("error"),
                          "guard_reason": guard_reason,
                          "contract_failures": report.get("contract_failures", [])}),
              flush=True)
    return 1 if failed or guard_reason else 0


if __name__ == "__main__":
    sys.exit(main())
