# /// script
# requires-python = ">=3.14"
# dependencies = ["psutil>=7"]
# ///
"""Run SobaFM for a soak test and tally the result (#27).

`run` starts SobaFM, copies its log to a file, and records the process's memory every minute.
Start a raw-text `/play` in the private server once it is ready, with a listener in the
channel, and stop the run with Ctrl+C or let `--minutes` end it. The environment, including
the API key, is inherited from the shell; `uv run --env-file .env` loads it from `.env`.

    uv run --env-file .env scripts/soak.py run --minutes 70 --out soak/run1
    uv run scripts/soak.py report soak/run1

`report` prints the results row for the issue: total underrun, deck lifetimes and end
reasons, warnings and errors, and whether memory stayed flat. What needs a person's ears or
actions stays on the checklist in the issue.
"""

import argparse
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psutil

SAMPLE_S = 60
UNDERRUN = re.compile(r"Program ended \((?P<outcome>[^)]*)\) with (?P<seconds>[\d.]+) s of underrun")
DECK_END = re.compile(r"Deck (?P<number>\d+) (?P<reason>\S+)(?: \([^)]*\))? after (?P<age>\d+) s")
LEVEL = re.compile(r"^\S+ \S+ (?P<level>WARNING|ERROR|CRITICAL) ")
FLAT_GROWTH = 0.10  # a rise past this fraction between the run's start and end is a leak


def run(out: Path, minutes: float) -> int:
    out.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + minutes * 60
    with (out / "sobafm.log").open("w", encoding="utf-8") as log:
        bot = subprocess.Popen(
            [sys.executable, "-m", "sobafm"], stdout=log, stderr=subprocess.STDOUT, text=True
        )
        process = psutil.Process(bot.pid)
        with (out / "memory.csv").open("w", encoding="utf-8") as memory:
            memory.write("minute,rss_mib\n")
            minute = 0
            try:
                while bot.poll() is None and time.monotonic() < deadline:
                    rss = process.memory_info().rss / 2**20
                    memory.write(f"{minute},{rss:.1f}\n")
                    memory.flush()
                    minute += 1
                    time.sleep(SAMPLE_S)
            except KeyboardInterrupt:
                pass
            finally:
                if bot.poll() is None:
                    bot.terminate()
                    bot.wait(timeout=30)
    print(f"Wrote {out}; run `soak.py report {out}`")
    return 0


def report(out: Path) -> int:
    lines = (out / "sobafm.log").read_text(encoding="utf-8").splitlines()
    underrun = sum(float(m["seconds"]) for line in lines if (m := UNDERRUN.search(line)))
    programs = sum(1 for line in lines if UNDERRUN.search(line))
    decks = [m for line in lines if (m := DECK_END.search(line))]
    reasons: dict[str, int] = {}
    for deck in decks:
        reasons[deck["reason"]] = reasons.get(deck["reason"], 0) + 1
    problems = [line for line in lines if LEVEL.match(line)]

    samples = [
        float(row.split(",")[1])
        for row in (out / "memory.csv").read_text(encoding="utf-8").splitlines()[1:]
    ]
    print(f"Programs ended:      {programs}")
    print(f"Total underrun:      {underrun:.2f} s (limit 2 s)")
    print(f"Decks ended:         {len(decks)} ({', '.join(f'{k}: {v}' for k, v in reasons.items())})")
    print("                     rotations need at least 6 in a 60 minute run")
    print(f"Warnings and errors: {len(problems)}")
    for line in problems[:20]:
        print(f"  {line}")
    if len(samples) >= 20:
        edge = max(len(samples) // 10, 1)
        start, end = statistics.median(samples[:edge]), statistics.median(samples[-edge:])
        growth = (end - start) / start
        verdict = "flat" if growth <= FLAT_GROWTH else "GROWING"
        print(f"Memory:              {start:.0f} MiB -> {end:.0f} MiB ({growth:+.0%}), {verdict}")
    else:
        print(f"Memory:              only {len(samples)} samples; run at least 20 minutes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="run SobaFM and record its log and memory")
    start.add_argument("--out", type=Path, required=True)
    start.add_argument("--minutes", type=float, default=70)
    tally = commands.add_parser("report", help="tally a finished run")
    tally.add_argument("out", type=Path)
    args = parser.parse_args()
    return run(args.out, args.minutes) if args.command == "run" else report(args.out)


if __name__ == "__main__":
    sys.exit(main())
