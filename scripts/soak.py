r"""Run SobaFM for a soak test, and check a run's log against the soak criteria (#27).

`run` starts SobaFM in this process, unchanged, and writes everything it prints to
`<run directory>/sobafm.log`. It adds `soak:` lines to SobaFM's log: each change of a station's
live deck, checked every 5 seconds, and every minute this process's memory, its task and object
counts, and each station's program, underrun, and decks. Creating a file named `stop` in the run
directory closes SobaFM gracefully. `report` reads the log, checks the run, and prints its row for
the issue's results table.

From the repository root, so SobaFM finds `.env`:

    uv run python scripts/soak.py run soak/run1
    uv run python scripts/soak.py report soak/run1 --label "Run 1" --commit <commit>

In the container image, with this directory mounted and no other SobaFM using the same token:

    docker run --rm --name sobafm-soak --env-file .env -v ./scripts:/scripts:ro \
        -v sobafm-data:/data ghcr.io/slackysoba/sobafm:<tag> \
        python /scripts/soak.py run /data/soak/run1
    docker exec sobafm-soak touch /data/soak/run1/stop
    docker run --rm -v ./scripts:/scripts:ro -v sobafm-data:/data \
        ghcr.io/slackysoba/sobafm:<tag> python /scripts/soak.py report /data/soak/run1 ...

A run is one raw-text `/play` in a private server, with a listener in the voice channel until the
program ends, at SobaFM's default log level. Create the stop file once the log shows
`Program ended`. Memory is the working set and private bytes on Windows, and VmRSS and RssAnon
(resident anonymous memory) from /proc/self/status on Linux; other platforms log none, and
`report` shows it as n/a.

A run passes with one request that played its full duration, at most 2 s of underrun, at least 6
handovers, no unhandled errors (no tracebacks, errors, or mixer errors), and flat memory: private
memory in the program's last 10 minutes at most 10 MiB above minutes 5 to 15. What needs a
person's ears or actions, such as listening and voice channel handling, stays on the issue.
"""

import argparse
import asyncio
import ctypes
import gc
import io
import itertools
import logging
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sobafm.__main__ as entry
from sobafm.bot import SobaFM
from sobafm.deck import Deck
from sobafm.pcm import FRAME_SECONDS
from sobafm.station import Station

LOG_NAME = "sobafm.log"
STOP_NAME = "stop"
POLL_S = 5.0  # how often the live decks and the stop file are checked
REPORT_S = 60.0

UNDERRUN_LIMIT_S = 2.0
HANDOVERS_NEEDED = 6
GROWTH_LIMIT_MIB = 10.0

RECORD = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) ([\w.]+): (.*)$")
MEMORY = re.compile(
    r"soak: (\d+) min; (?:memory ([\d.]+) MiB working set, ([\d.]+) MiB private; )?"
    r"(\d+) tasks; (\d+) objects"
)
LIVE = re.compile(r"soak: station \d+: live deck (#(\d+)|none)")
STATION = re.compile(r"underrun ([\d.]+) s, mixer errors (\d+)")
DECK = re.compile(
    r"Deck (\d+) (\w+)(?: \((.*)\))? after (\d+) s: (\d+) s of audio(?: at ([\d.]+)x real time)?"
)
ENDED = re.compile(r"Program ended \((\w+)\) with ([\d.]+) s of underrun")

log = logging.getLogger("sobafm.soak")


if sys.platform == "win32":
    from ctypes import wintypes

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [  # PROCESS_MEMORY_COUNTERS_EX
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_MemoryCounters),
        wintypes.DWORD,
    ]
    _kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL

    def memory_mib() -> tuple[float, float] | None:
        """This process's working set and private bytes, in MiB."""
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not _kernel32.K32GetProcessMemoryInfo(
            _kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize / 2**20, counters.PrivateUsage / 2**20

elif sys.platform == "linux":

    def memory_mib() -> tuple[float, float] | None:
        """This process's resident and resident anonymous memory, in MiB, if the kernel reports
        them."""
        fields: dict[str, str] = {}
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            name, _, value = line.partition(":")
            fields[name] = value
        try:  # in kB
            return int(fields["VmRSS"].split()[0]) / 1024, int(fields["RssAnon"].split()[0]) / 1024
        except KeyError, IndexError, ValueError:
            return None

else:

    def memory_mib() -> tuple[float, float] | None:
        return None


def describe(station: Station) -> str:
    """A station's program, underrun, and decks, without the program's title."""
    program = station.program
    if program is None:
        phase = "idle"
    elif program.playing:
        phase = f"playing, {(station.time_left or 0.0) / 60:.1f} min left"
    else:
        phase = "starting"
    decks = "; ".join(
        f"#{deck.number} {deck.state}{' paused' if deck.paused else ''}"
        f" age {deck.age:.0f} s, {deck.buffered_seconds:.1f} s buffered"
        + (f", {deck.rate:.2f}x" if deck.rate is not None else "")
        + (f", ended {deck.end_reason}" if deck.end_reason is not None else "")
        for deck in station.decks
    )
    mixer = station.mixer
    return (
        f"{phase}; underrun {mixer.underruns * FRAME_SECONDS:.2f} s, mixer errors {mixer.errors};"
        f" decks: {decks or 'none'}"
    )


async def watch(bot: SobaFM, stop: Path) -> None:
    """Log each station's live deck when it changes, and the process and stations every minute,
    until SobaFM closes or the stop file appears."""
    started = time.monotonic()
    next_report = started
    live: dict[int, int | None] = {}  # each station's live deck number
    while not bot.is_closed():
        if await asyncio.to_thread(stop.exists):
            log.info("soak: stop file found; closing")
            await bot.close()
            return
        for guild_id, station in list(bot.stations.items()):
            source = station.mixer.live
            number = source.number if isinstance(source, Deck) else None
            if live.get(guild_id) != number:
                live[guild_id] = number
                log.info(
                    "soak: station %d: live deck %s",
                    guild_id,
                    "none" if number is None else f"#{number}",
                )
        if time.monotonic() >= next_report:
            next_report += REPORT_S
            minutes = (time.monotonic() - started) / 60
            counts = len(asyncio.all_tasks()), len(gc.get_objects())
            if (memory := memory_mib()) is None:
                log.info("soak: %.0f min; %d tasks; %d objects", minutes, *counts)
            else:
                log.info(
                    "soak: %.0f min; memory %.1f MiB working set, %.1f MiB private;"
                    " %d tasks; %d objects",
                    minutes,
                    *memory,
                    *counts,
                )
            for guild_id, station in bot.stations.items():
                log.info("soak: station %d: %s", guild_id, describe(station))
        await asyncio.sleep(POLL_S)


def install(stop: Path) -> None:
    """Start watching once SobaFM's client is set up, which is after it logs in."""
    original = SobaFM.setup_hook
    tasks: set[asyncio.Task[None]] = set()

    async def setup_hook(self: SobaFM) -> None:
        await original(self)
        task = asyncio.create_task(watch(self, stop), name="soak")
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    SobaFM.setup_hook = setup_hook


def run(directory: Path) -> int:
    """Run SobaFM with its output in the run directory's log, returning its exit status."""
    directory.mkdir(parents=True, exist_ok=True)
    stop = directory / STOP_NAME
    if stop.exists():
        print(f"soak: remove {stop} first", file=sys.stderr)
        return 2
    path = directory / LOG_NAME
    try:
        # Left open for anything printed as Python shuts down
        output = path.open("x", encoding="utf-8", errors="backslashreplace", buffering=1)
    except FileExistsError:
        print(f"soak: {path} exists; use a new run directory", file=sys.stderr)
        return 2
    console = sys.stdout
    print(f"soak: SobaFM's output goes to {path}; create {stop} to close it", flush=True)
    sys.stdout = sys.stderr = output
    install(stop)
    status = 1  # if SobaFM raises, the traceback goes to the log
    try:
        entry.main()
        status = 0
    except SystemExit as exit_:
        if exit_.code is None or isinstance(exit_.code, int):
            status = exit_.code or 0
        else:
            print(exit_.code, file=sys.stderr)
    finally:
        print(f"soak: SobaFM exited with status {status}", file=console, flush=True)
    return status


@dataclass(frozen=True)
class Record:
    time: datetime
    level: str
    logger: str
    message: str


def report(directory: Path, label: str, commit: str) -> int:
    """Print a run's program, playback, memory, and problems, the checks, and its results row."""
    path = directory / LOG_NAME
    records: list[Record] = []
    stray: list[str] = []  # lines that aren't log records, such as tracebacks
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := RECORD.match(line):
            time_ = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S")
            records.append(Record(time_, match[2], match[3], match[4]))
        elif line.strip():
            stray.append(line)

    starts = [
        r
        for r in records
        if r.logger == "sobafm.station" and r.message.startswith("Program started")
    ]
    ends = [(r.time, match) for r in records if (match := ENDED.search(r.message))]
    reasons = [
        r for r in records if r.logger == "sobafm.station" and "ending the program" in r.message
    ]
    requests = sum(r.message.startswith("Interpretation took") for r in records)
    print(f"Log: {path}")
    print(f"Requests: {requests}; programs started: {len(starts)}; ended: {len(ends)}")
    for r in starts + reasons:
        print(f"  {r.time:%H:%M:%S} {r.message}")
    for t, match in ends:
        print(f"  {t:%H:%M:%S} Program ended ({match[1]}) with {match[2]} s of underrun")
    if not starts:
        print("No program played; nothing to check.")
        return 0
    begin = starts[0].time
    finish = min([t for t, _ in ends] + [r.time for r in reasons] or [records[-1].time])
    minutes = (finish - begin).total_seconds() / 60
    print(f"Program length: {minutes:.1f} min, {begin:%H:%M:%S} to {finish:%H:%M:%S}")

    underrun = sum(float(match[2]) for _, match in ends)
    dry = sum("ran dry" in r.message for r in records)
    silences = [
        float(r.message.split()[2]) for r in records if r.message.startswith("Silence lasted")
    ]
    stations = [match for r in records if (match := STATION.search(r.message))]
    mixer_errors = int(stations[-1][2]) if stations else 0
    print(
        f"Underrun: {underrun:.2f} s in {dry} dry spells; silences {silences or 'none'};"
        f" mixer errors {mixer_errors}"
    )

    live = [
        match[2]
        for r in records
        if (match := LIVE.search(r.message)) and match[2] and begin <= r.time
    ]
    sequence = [number for number, _ in itertools.groupby(live)]
    handovers = max(0, len(sequence) - 1)
    print(
        f"Handovers: {handovers}; live decks in order: "
        f"{', '.join('#' + n for n in sequence) or 'none'}"
    )

    decks = [
        match for r in records if r.logger == "sobafm.deck" and (match := DECK.search(r.message))
    ]
    by_reason = Counter(deck[2] for deck in decks)
    rates = [float(deck[6]) for deck in decks if deck[6]]
    print(f"Decks ended: {len(decks)} {dict(by_reason)}")
    if rates:
        print(
            f"Generation rates: min {min(rates):.2f}x, median {statistics.median(rates):.2f}x,"
            f" max {max(rates):.2f}x"
        )
    for deck in decks:
        notes = f" ({deck[3]})" if deck[3] else ""
        rate = f" at {deck[6]}x" if deck[6] else ""
        print(f"  deck {deck[1]}: {deck[2]}{notes} after {deck[4]} s, {deck[5]} s of audio{rate}")

    # (time, working set, private, objects) for each minute of the program with memory logged
    during = [
        (r.time, float(match[2]), float(match[3]), int(match[5]))
        for r in records
        if (match := MEMORY.search(r.message)) and match[2] and begin <= r.time <= finish
    ]
    growth: float | None = None
    if during:
        private = [p for _, _, p, _ in during]
        print(
            f"Memory while playing: private {private[0]:.1f} -> {private[-1]:.1f} MiB"
            f" (max {max(private):.1f}), working set max {max(w for _, w, _, _ in during):.1f} MiB,"
            f" objects {during[0][3]} -> {during[-1][3]}"
        )
        early = [p for t, _, p, _ in during if 5 <= (t - begin).total_seconds() / 60 <= 15]
        late = private[-10:]
        if early and len(during) >= 30:
            growth = statistics.mean(late) - statistics.mean(early)
            print(f"Private bytes, last 10 min vs minutes 5-15: {growth:+.1f} MiB")
    else:
        print("Memory while playing: n/a")

    problems = [r for r in records if r.level in ("WARNING", "ERROR", "CRITICAL")]
    print(f"Warnings and errors: {len(problems)}")
    for r in problems[:20]:
        print(f"  {r.time:%H:%M:%S} {r.level} {r.logger}: {r.message[:160]}")
    tracebacks = sum("Traceback" in line for line in stray)
    print(f"Other output lines: {len(stray)}, tracebacks: {tracebacks}")
    for line in stray[:10]:
        print(f"  {line[:160]}")

    checks = {
        "one request, played its full duration": requests == 1
        and len(starts) == 1
        and any("Play duration elapsed" in r.message for r in reasons),
        f"underrun <= {UNDERRUN_LIMIT_S:.0f} s": underrun <= UNDERRUN_LIMIT_S,
        f">= {HANDOVERS_NEEDED} handovers": handovers >= HANDOVERS_NEEDED,
        "no unhandled errors": tracebacks == 0
        and mixer_errors == 0
        and all(r.level == "WARNING" for r in problems),
        "flat memory": growth is not None and growth <= GROWTH_LIMIT_MIB,
    }
    print("Checks:")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    result = "pass" if all(checks.values()) else "fail"
    memory_cell = (
        f"{during[0][2]:.0f} -> {during[-1][2]:.0f} MiB ({growth:+.1f})"
        if growth is not None
        else "n/a"
    )
    rate_cell = f"{min(rates):.2f}-{max(rates):.2f}x" if rates else "n/a"
    reasons_cell = ", ".join(f"{n} {reason}" for reason, n in sorted(by_reason.items()))
    print()
    print(
        "| Run | Date | Commit | Program | Underrun | Handovers | Decks ended | Rates"
        " | Private memory | Warnings | Result |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    print(
        f"| {label} | {begin:%Y-%m-%d} | {commit} | {minutes:.0f} min | {underrun:.2f} s"
        f" | {handovers} | {reasons_cell} | {rate_cell} | {memory_cell} | {len(problems)}"
        f" | {result} |"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="run SobaFM, logging the soak lines")
    start.add_argument("directory", type=Path, help="the run directory, created if needed")
    check = commands.add_parser("report", help="check a run's log and print its results row")
    check.add_argument("directory", type=Path, help="the run directory")
    check.add_argument("--label", default="?", help="the run's name in the row, such as 'Run 1'")
    check.add_argument("--commit", default="?", help="the commit SobaFM ran")
    args = parser.parse_args()
    if args.command == "run":
        return run(args.directory)
    if not (args.directory / LOG_NAME).is_file():
        parser.error(f"{args.directory / LOG_NAME} does not exist")
    stdout: object = sys.stdout
    if isinstance(stdout, io.TextIOWrapper):
        # The log can hold text the console can't encode, such as a server's name.
        stdout.reconfigure(errors="backslashreplace")
    return report(args.directory, args.label, args.commit)


if __name__ == "__main__":
    sys.exit(main())
