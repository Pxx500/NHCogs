"""Run the installed Red instance with one controlled privileged-intent fallback."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PRIVILEGED_INTENTS = ("members", "presences", "message_content")
RECOVERY_PATH_ENV = "NHC0GS_GATEWAY_RECOVERY_PATH"
RECOVERY_INTENTS_ENV = "NHC0GS_GATEWAY_RECOVERY_INTENTS"
MIN_RESTART_DELAY = 8
MAX_RESTART_DELAY = 300
POLL_INTERVAL = 0.25
SHUTDOWN_TIMEOUT = 20
_DENIAL = re.compile(
    r"Red requires all Privileged Intents to be enabled\."
    r"|\bPrivilegedIntentsRequired\b"
    r"|(?:gateway[^\n]{0,100}(?:close|code)[^\n]{0,30}\b4014\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    intent_denied: bool = False
    restore_requested: bool = False


def requested_intents(red_args: Sequence[str]) -> tuple[str, ...]:
    disabled = set()
    for position, argument in enumerate(red_args):
        if argument == "--disable-intent" and position + 1 < len(red_args):
            disabled.add(red_args[position + 1])
        elif argument.startswith("--disable-intent="):
            disabled.add(argument.partition("=")[2])
    return tuple(intent for intent in PRIVILEGED_INTENTS if intent not in disabled)


def red_command(red_args: Sequence[str], *, degraded: bool) -> list[str]:
    command = [sys.executable, "-m", "redbot", *red_args]
    if degraded:
        for intent in requested_intents(red_args):
            command.extend(("--disable-intent", intent))
    return command


def stop_child(process: subprocess.Popen) -> None:
    """Wait for the old child to exit before another instance may start."""
    if process.poll() is not None:
        process.wait()
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
    except OSError:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _forward_output(stream, denied: threading.Event) -> None:
    tail = ""
    try:
        while chunk := stream.read1(4096):
            # Forward Red's own output without reading config or echoing its args.
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            text = tail + chunk.decode("utf-8", errors="replace")
            if _DENIAL.search(text):
                denied.set()
            tail = text[-2048:]
    finally:
        stream.close()


def run_child(
    command: Sequence[str], environment: dict[str, str], marker: Path, allow_restore: bool,
) -> ChildResult:
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    process = subprocess.Popen(  # noqa: S603
        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options,
    )
    denied = threading.Event()
    pump = threading.Thread(target=_forward_output, args=(process.stdout, denied), daemon=True)
    pump.start()
    try:
        while process.poll() is None:
            if allow_restore and marker.is_file():
                marker.unlink(missing_ok=True)
                stop_child(process)
                pump.join()
                return ChildResult(process.returncode, denied.is_set(), restore_requested=True)
            time.sleep(POLL_INTERVAL)
        pump.join()
        return ChildResult(process.returncode, denied.is_set())
    finally:
        if process.poll() is None:
            stop_child(process)
        pump.join()


def supervise(
    red_args: Sequence[str], marker: Path, *, start_degraded: bool = False,
    auto_restore: bool = True,
    runner: Callable = run_child,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    requested = requested_intents(red_args)
    degraded = start_degraded and bool(requested)
    restarts = 0
    while True:
        marker.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment[RECOVERY_PATH_ENV] = str(marker.resolve())
        environment[RECOVERY_INTENTS_ENV] = ",".join(requested)
        result = runner(red_command(red_args, degraded=degraded), environment, marker,
                        degraded and auto_restore and bool(requested))
        if result.restore_requested and degraded and auto_restore:
            degraded = False
            print("Gateway grants restored; restarting Red with the original intent settings.",
                  flush=True)
        elif result.returncode != 0 and result.intent_denied and not degraded and requested:
            degraded = True
            print("Privileged intents unavailable; restarting Red with all privileged intents disabled.",
                  flush=True)
        else:
            return result.returncode
        # Each denied full connection gets one degraded attempt. Failed degraded
        # runs are returned above; repeated restoration cycles back off as well.
        delay = min(MAX_RESTART_DELAY, MIN_RESTART_DELAY * 2 ** min(restarts, 6))
        restarts += 1
        sleeper(delay)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-degraded", action="store_true")
    parser.add_argument("--no-auto-restore", action="store_true")
    options, red_args = parser.parse_known_args(argv)
    if red_args[:1] == ["--"]:
        red_args = red_args[1:]
    if not red_args:
        parser.error("Pass the existing Red instance arguments after --")
    try:
        with tempfile.TemporaryDirectory(prefix="nhcogs-gateway-recovery-") as directory:
            return supervise(
                red_args, Path(directory) / "restore-requested",
                start_degraded=options.start_degraded, auto_restore=not options.no_auto_restore,
            )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
