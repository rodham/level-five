#!/usr/bin/env python3
"""Run this repository's suite on Linux, then natively on Windows.

This is the target's test command while the harness is being ported to
Windows. The harness runs under WSL, where it already works, and every gate it
runs -- the suite in the tree, the revert check, a nominated test, the
clean-clone check -- reads only this script's exit code. So making the script
run the suite on both systems makes every gate a two-system gate without
changing the harness.

    dual_suite.py [--strict] <pytest arguments>
    dual_suite.py --write-known-failures <pytest arguments>

Everything after the flags is passed to pytest unchanged, on both systems.

THE LINUX HALF runs under the interpreter that launched this script, so the
clean-clone check's `verification_runner` -- which replaces the first word of
the configured command -- still decides which Python the Linux half uses. If
the Linux half fails, the Windows half is not run: the gate has failed either
way, and a revert check expecting a failure gets it in half the time.

THE WINDOWS HALF runs the Windows interpreter named by L5_WINDOWS_PYTHON (a
WSL path such as /mnt/c/Users/me/.venvs/l5-win/Scripts/python.exe) against a
copy of the tree on a Windows drive. A copy, because a Windows process reading
the tree through \\\\wsl.localhost\\ would be testing a network share, whose
locking, linking and renaming are not NTFS's -- and those are what the port
changes. The copy is a clone of the repository's history with the working
tree's uncommitted changes laid over it, because several tests read this
repository's history. It lands in a fresh directory under L5_WINDOWS_WORKDIR,
or under the Windows %TEMP% when that is unset, and is removed afterwards;
the Windows pytest output is kept beside it as a .log file.

AN UNSET L5_WINDOWS_PYTHON IS A FAILURE, not a skip. A Linux-only pass would
read as green while having tested nothing on Windows.

KNOWN FAILURES. The Windows half starts far from green, so it is held to a
ratchet rather than to zero: every Windows failure must be covered by an entry
in .harness/windows-known-failures.txt. An entry is a pytest node id, or a
file path covering every test in that file. The list is a blocked path in
rules/execution-rules.json, so no stage can add an entry to hide a failure it
caused; a person shrinks it as stories land, by hand or with
--write-known-failures, which runs only the Windows half and rewrites the list
from what failed.

--strict ignores the list: everything selected must pass on both systems. It
is what test_selection_command uses, so a test nominated for the revert check
has to pass on Windows too -- which is exactly the evidence a Windows fix
needs, and why a nominated test that is still on the list cannot pass it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KNOWN_FAILURES = Path(".harness") / "windows-known-failures.txt"
PYTHON_VARIABLE = "L5_WINDOWS_PYTHON"
WORKDIR_VARIABLE = "L5_WINDOWS_WORKDIR"

#: pytest's exit codes for "every test passed" and "some tests failed". Any
#: other code -- interrupted, internal error, usage error, nothing collected --
#: means the run did not finish, and is a failure whatever the list says.
PYTEST_PASSED = 0
PYTEST_TESTS_FAILED = 1

#: The exit code for a run that could not be set up at all.
SETUP_FAILED = 2

SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")
#: pytest pads a section title with underscores to the terminal width, down to
#: a single one when the title is long. The title must hold something other
#: than underscores, so the `_ _ _ _` separator pytest prints between frames of
#: one traceback is not read as the start of a new section.
SECTION_HEADER = re.compile(r"^_+ ((?=.*[^_ ]).+?) _+$")
BANNER = re.compile(r"^={3,} .* ={3,}$")


def say(message: str) -> None:
    print(f"[dual-suite] {message}", flush=True)


def git(root: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=check,
    ).stdout


def windows_path(path: Path) -> str:
    return subprocess.run(
        ["wslpath", "-w", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


def windows_workdir() -> Path:
    """Where the Windows copy goes: L5_WINDOWS_WORKDIR, else the Windows %TEMP%."""
    configured = os.environ.get(WORKDIR_VARIABLE)
    if configured:
        return Path(configured)
    # cmd.exe complains about a UNC working directory, so it is started on C:.
    temp = subprocess.run(
        ["cmd.exe", "/d", "/c", "echo %TEMP%"],
        capture_output=True, text=True, check=True, cwd="/mnt/c",
    ).stdout.strip()
    return Path(subprocess.run(
        ["wslpath", "-u", temp], capture_output=True, text=True, check=True
    ).stdout.strip())


def mirror(root: Path, destination: Path) -> None:
    """Clone `root`'s history into `destination` and lay its working tree over it.

    The clone stands on the commit `root` stands on, under the same branch
    name, with the same `origin` URL, so a test reading this repository's
    history or branch reads what it would read in `root`. Uncommitted changes
    are then copied over -- modified, added and untracked files written,
    deleted ones removed -- so what is tested is the tree the gate is asking
    about, not its last commit.
    """
    git(root, "clone", "-q", "--no-checkout", str(root), str(destination))
    git(destination, "fetch", "-q", str(root), "HEAD")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch and branch != "HEAD":
        git(destination, "checkout", "-q", "-B", branch, "FETCH_HEAD")
    else:
        git(destination, "checkout", "-q", "--detach", "FETCH_HEAD")
    origin = git(root, "remote", "get-url", "origin", check=False).strip()
    if origin:
        git(destination, "remote", "set-url", "origin", origin)

    changed = git(root, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    for relative in filter(None, (changed + untracked).split("\0")):
        source, target = root / relative, destination / relative
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.is_file():
            target.unlink()


def known_failures(root: Path) -> list[str]:
    path = root / KNOWN_FAILURES
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def covered(failure: str, entries: list[str]) -> bool:
    return any(failure == entry or failure.startswith(entry + "::")
               for entry in entries)


def failures_in(output: str) -> list[str]:
    """The node ids pytest's short summary reports as failed or errored."""
    found = []
    for line in output.splitlines():
        matched = SUMMARY_LINE.match(line)
        if matched:
            node = matched.group(2).replace("\\", "/")
            if node not in found:
                found.append(node)
    return found


def sections_for(output: str, nodes: list[str]) -> str:
    """The traceback sections pytest printed for `nodes`, and nothing else.

    A section opens with a `___ title ___` header and ends at the next header
    or banner. A test's title is its node id after the file, with `::` read as
    `.`; setup, teardown and collection errors carry their own prefixes.
    """
    wanted = set()
    for node in nodes:
        file, _, rest = node.partition("::")
        if rest:
            name = rest.replace("::", ".")
            wanted |= {name, f"ERROR at setup of {name}",
                       f"ERROR at teardown of {name}"}
        else:
            wanted.add(f"ERROR collecting {file}")
    kept: list[str] = []
    keeping = False
    for line in output.splitlines():
        header = SECTION_HEADER.match(line)
        if header:
            keeping = header.group(1) in wanted
        elif BANNER.match(line):
            keeping = False
        if keeping:
            kept.append(line)
    return "\n".join(kept)


def run_windows(root: Path, windows_python: str, arguments: list[str]
                ) -> tuple[int, str, Path]:
    """Run pytest on Windows over a copy of `root`. Returns code, output, log."""
    workdir = windows_workdir()
    workdir.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="l5w-", dir=workdir))
    log = destination.with_suffix(".log")
    try:
        say(f"copying the tree to {windows_path(destination)}")
        mirror(root, destination)
        say("running the suite on Windows")
        with open(log, "w", encoding="utf-8") as handle:
            code = subprocess.run(
                [windows_python, "-m", "pytest", *arguments, "-rfE"],
                cwd=destination, stdout=handle, stderr=subprocess.STDOUT,
            ).returncode
    finally:
        shutil.rmtree(destination, ignore_errors=True)
    return code, log.read_text(encoding="utf-8", errors="replace"), log


def tail(output: str, lines: int = 40) -> str:
    return "\n".join(output.splitlines()[-lines:])


def main(argv: list[str]) -> int:
    strict = record = False
    while argv and argv[0] in ("--strict", "--write-known-failures"):
        strict |= argv[0] == "--strict"
        record |= argv[0] == "--write-known-failures"
        argv = argv[1:]

    windows_python = os.environ.get(PYTHON_VARIABLE, "")
    if not windows_python:
        say(f"{PYTHON_VARIABLE} is not set, so the Windows half cannot run. "
            "Set it to the Windows interpreter, as a WSL path. Refusing to "
            "report a Linux-only pass.")
        return SETUP_FAILED
    if not Path(windows_python).is_file():
        say(f"{PYTHON_VARIABLE} names {windows_python}, which is not a file.")
        return SETUP_FAILED
    root = Path.cwd()

    if not record:
        say("running the suite on Linux")
        linux = subprocess.run([sys.executable, "-m", "pytest", *argv]).returncode
        if linux != PYTEST_PASSED:
            say(f"the Linux half failed (pytest exit {linux}); "
                "the Windows half was not run")
            return linux

    try:
        code, output, log = run_windows(root, windows_python, argv)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or ""
        say(f"the Windows half could not be set up: {error} {detail}".strip())
        return SETUP_FAILED
    say(f"Windows pytest exited {code}; its full output is {windows_path(log)}")

    if code not in (PYTEST_PASSED, PYTEST_TESTS_FAILED):
        print(tail(output))
        say("the Windows run did not finish, which fails the gate whatever "
            "the known-failures list says")
        return code
    failures = failures_in(output)

    if record:
        (root / KNOWN_FAILURES).write_text(
            "# Tests that fail when the suite runs natively on Windows.\n"
            "# Read by .harness/dual_suite.py; see its docstring. Remove an\n"
            "# entry once its test passes on Windows; never add one to hide\n"
            "# a regression.\n"
            + "".join(f"{node}\n" for node in sorted(failures)),
            encoding="utf-8")
        say(f"wrote {len(failures)} entries to {KNOWN_FAILURES}")
        return 0

    if strict:
        print(output)
        return code

    entries = known_failures(root)
    unexpected = [node for node in failures if not covered(node, entries)]
    print(tail(output, 3))
    if unexpected:
        print(sections_for(output, unexpected))
        say(f"{len(unexpected)} test(s) failed on Windows that are not on "
            f"{KNOWN_FAILURES}:")
        for node in unexpected:
            print(f"  {node}")
        return 1
    still_listed = [entry for entry in entries
                    if not any(covered(node, [entry]) for node in failures)]
    say(f"Windows: {len(failures)} failure(s), all on the known-failures list")
    if still_listed:
        say(f"{len(still_listed)} listed entr(ies) did not fail in this run; "
            "if it was the whole suite they can come off the list")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
