#!/usr/bin/env python3
"""shaq v0.0.5 — Shazam from your terminal."""

# ---------------------------------------------------------------------------
# Dependency bootstrap — runs before anything else is imported.
# Mirrors the requirements in pyproject.toml; edit here if deps change.
# ---------------------------------------------------------------------------
import importlib.metadata
import subprocess
import sys

_DEPS = [
    "pyaudio ~= 0.2.13",
    "pydub ~= 0.25.1",
    "rich >= 13.4, < 16.0",
    "shazamio >= 0.6, < 0.9",
]

# Map install name → import name for the handful that differ.
_IMPORT_NAMES: dict[str, str] = {
    "pydub": "pydub",
    "pyaudio": "pyaudio",
    "rich": "rich",
    "shazamio": "shazamio",
}


def _check_and_install(specs: list[str]) -> None:
    from packaging.requirements import Requirement
    from packaging.version import Version

    missing: list[str] = []

    for spec in specs:
        req = Requirement(spec)
        try:
            installed = Version(importlib.metadata.version(req.name))
            if installed not in req.specifier:
                print(
                    f"[shaq] {req.name} {installed} doesn't satisfy {req.specifier} — reinstalling",
                    file=sys.stderr,
                )
                missing.append(spec)
        except importlib.metadata.PackageNotFoundError:
            print(f"[shaq] {req.name} not found — installing", file=sys.stderr)
            missing.append(spec)

    if missing:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing]
        )
        print("[shaq] Dependencies installed — continuing.\n", file=sys.stderr)


# packaging is stdlib-adjacent but not guaranteed; bootstrap it first if needed.
try:
    from packaging.requirements import Requirement  # noqa: F401
except ModuleNotFoundError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "packaging"]
    )

_check_and_install(_DEPS)
# ---------------------------------------------------------------------------

import argparse
import asyncio
import curses
import json
import logging
import os
import shutil
import sys
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import pyaudio
from pydub import AudioSegment
from rich import progress
from rich.console import Console
from rich.logging import RichHandler
from rich.status import Status
from shazamio import Shazam

logging.basicConfig(
    level=os.environ.get("SHAQ_LOGLEVEL", "INFO").upper(),
    format="%(message)s",
    datefmt="[%X]",
)

_DEFAULT_CHUNK_SIZE = 4096
_FORMAT = pyaudio.paInt16
_DEFAULT_CHANNELS = 1
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_DURATION = 10

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terminal device picker
# ---------------------------------------------------------------------------

def _get_input_devices() -> list[tuple[int, str]]:
    """Return list of (index, name) for all PortAudio input devices."""
    devnull_fds = (os.open(os.devnull, os.O_WRONLY), os.open(os.devnull, os.O_WRONLY))
    saved = (os.dup(sys.stdout.fileno()), os.dup(sys.stderr.fileno()))
    os.dup2(devnull_fds[0], sys.stdout.fileno())
    os.dup2(devnull_fds[1], sys.stderr.fileno())
    try:
        p = pyaudio.PyAudio()
        devices = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) >= 1:
                devices.append((i, info["name"]))
        p.terminate()
    finally:
        os.dup2(saved[0], sys.stdout.fileno())
        os.dup2(saved[1], sys.stderr.fileno())
        for fd in [*devnull_fds, *saved]:
            os.close(fd)
    return devices


def _pick_device_curses(stdscr, devices: list[tuple[int, str]]) -> int:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(2, curses.COLOR_CYAN, -1)

    selected = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        header = "Select input device  (↑/↓ to move, Enter to confirm, q to quit)"
        stdscr.addstr(0, 0, header[:w - 1], curses.color_pair(2) | curses.A_BOLD)
        stdscr.addstr(1, 0, ("─" * min(w - 1, 66))[:w - 1], curses.color_pair(2))

        for row, (idx, name) in enumerate(devices):
            y = row + 2
            if y >= h - 1:
                break
            label = f"  [{idx}]  {name}"[:w - 1]
            if row == selected:
                stdscr.addstr(y, 0, label.ljust(min(w - 1, 66))[:w - 1], curses.color_pair(1) | curses.A_BOLD)
            else:
                stdscr.addstr(y, 0, label)

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")) and selected > 0:
            selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")) and selected < len(devices) - 1:
            selected += 1
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            return devices[selected][0]
        elif key in (ord("q"), 27):
            sys.exit(0)


def _pick_device(devices: list[tuple[int, str]]) -> int:
    if not devices:
        print("No input devices found. Check your audio configuration.", file=sys.stderr)
        sys.exit(1)
    if len(devices) == 1:
        idx, name = devices[0]
        print(f"Using only available device: [{idx}] {name}")
        return idx
    return curses.wrapper(_pick_device_curses, devices)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

@contextmanager
def _console() -> Iterator[Console]:
    """
    Temporarily dups and nulls the standard streams, while yielding a
    rich Console on the dup'd stderr.

    This is done because of PyAudio's misbehaving internals.
    See: https://stackoverflow.com/questions/67765911
    """
    try:
        dup_fds = (os.dup(sys.stdout.fileno()), os.dup(sys.stderr.fileno()))
        null_fds = tuple(os.open(os.devnull, os.O_WRONLY) for _ in range(2))
        os.dup2(null_fds[0], sys.stdout.fileno())
        os.dup2(null_fds[1], sys.stderr.fileno())
        dup_stderr = os.fdopen(dup_fds[1], mode="w")
        yield Console(file=dup_stderr)
    finally:
        os.dup2(dup_fds[0], sys.stdout.fileno())
        os.dup2(dup_fds[1], sys.stderr.fileno())
        for fd in [*null_fds, *dup_fds]:
            os.close(fd)


@contextmanager
def _pyaudio_ctx() -> Iterator[pyaudio.PyAudio]:
    p = pyaudio.PyAudio()
    try:
        yield p
    finally:
        p.terminate()


def _listen(console: Console, args: argparse.Namespace) -> AudioSegment:
    """
    Record from the selected device using PyAudio's callback mode.

    Callback mode lets PortAudio manage its own buffer on its own schedule,
    which avoids the -9981 overflow errors that the blocking read() loop causes
    when the device's native sample rate doesn't match our chunk math.
    We hand shazamio an AudioSegment directly — no sample-rate knowledge needed.
    """
    with _pyaudio_ctx() as p:
        info = p.get_device_info_by_index(args.device)
        rate = int(info["defaultSampleRate"])
        channels = min(1, int(info["maxInputChannels"]))

        frames: list[bytes] = []

        def _cb(in_data, frame_count, time_info, status):
            frames.append(in_data)
            return (None, pyaudio.paContinue)

        stream = p.open(
            format=_FORMAT,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=args.device,
            stream_callback=_cb,
        )

        with progress.Progress(console=console) as prog:
            task = prog.add_task("shaq is listening...", total=args.duration)
            stream.start_stream()
            elapsed = 0.0
            import time
            while elapsed < args.duration:
                time.sleep(0.1)
                elapsed += 0.1
                prog.update(task, completed=min(elapsed, args.duration))
            stream.stop_stream()

        stream.close()

        raw = b"".join(frames)
        return AudioSegment(
            data=raw,
            sample_width=p.get_sample_size(_FORMAT),
            frame_rate=rate,
            channels=channels,
        )


def _from_file(console: Console, args: argparse.Namespace) -> AudioSegment:
    with Status(f"Extracting from {args.input}", console=console):
        audio = AudioSegment.from_file(args.input)
        return audio[:args.duration * 1000]


async def _shaq(console: Console, args: argparse.Namespace) -> dict[str, Any]:
    data: AudioSegment | bytearray = _listen(console, args) if args.listen else _from_file(console, args)
    shazam = Shazam(language="en-US", endpoint_country="US")
    return await shazam.recognize_song(data, proxy=args.proxy)  # type: ignore


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # Not required=True so --list-devices works without --listen/--input.
    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument(
        "--listen", action="store_true", help="detect from the system's microphone"
    )
    input_group.add_argument("--input", type=Path, help="detect from the given audio input file")

    parser.add_argument(
        "-d", "--duration", metavar="SECS", type=int, default=_DEFAULT_DURATION,
        help="only analyze the first SECS of the input (microphone or file)",
    )
    parser.add_argument("-j", "--json", action="store_true", help="emit Shazam's response as JSON on stdout")
    parser.add_argument("--albumcover", action="store_true", help="return url to HD album cover")

    adv = parser.add_argument_group(
        title="Advanced Options",
        description="Advanced users only: options to tweak recording, transcoding, etc. behavior.",
    )
    adv.add_argument(
        "--chunk-size", type=int, default=_DEFAULT_CHUNK_SIZE,
        help="read from the microphone in chunks of this size; only affects --listen",
    )
    adv.add_argument(
        "--channels", type=int, choices=(1, 2), default=_DEFAULT_CHANNELS,
        help="the number of channels to use; only affects --listen",
    )
    adv.add_argument(
        "--sample-rate", type=int, default=_DEFAULT_SAMPLE_RATE,
        help="the sample rate to use; only affects --listen",
    )
    adv.add_argument("--proxy", type=str, help="send the request to a proxy server")
    adv.add_argument(
        "--device", type=int, default=None, metavar="INDEX",
        help="skip the device picker and use this PortAudio input device index",
    )
    adv.add_argument(
        "--list-devices", action="store_true",
        help="list available audio input devices and exit",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parser().parse_args()

    if args.list_devices:
        devices = _get_input_devices()
        if not devices:
            print("No input devices found.")
        else:
            print("Available input devices:")
            for idx, name in devices:
                print(f"  [{idx}] {name}")
        sys.exit(0)

    if not args.listen and args.input is None:
        _parser().error("one of the arguments --listen --input is required")

    # Show interactive picker when --listen is used without --device.
    if args.listen and args.device is None:
        devices = _get_input_devices()
        args.device = _pick_device(devices)

    with _console() as console:
        logger.addHandler(RichHandler(console=console))
        logger.debug(f"parsed {args=}")

        if not shutil.which("ffmpeg"):
            console.print("[red]Fatal: ffmpeg not found on $PATH[/red]")
            sys.exit(1)

        try:
            raw = asyncio.run(_shaq(console, args))
        except KeyboardInterrupt:
            console.print("[red]Interrupted.[/red]")
            sys.exit(2)

    if args.json:
        json.dump(raw, sys.stdout, indent=2)
        if not raw.get("matches"):
            sys.exit(1)
    else:
        if not raw.get("matches"):
            print("No matches.")
            sys.exit(1)
        track = raw.get("track", {})
        print(f"Track: {track.get('title', 'Unknown')}")
        print(f"Artist: {track.get('subtitle', 'Unknown')}")
        if args.albumcover:
            images = track.get("images", {})
            if images.get("coverart"):
                hq = images["coverart"].replace("/400x400cc.jpg", "/1000x1000cc.png")
                print(f"Album Cover: {hq}")


if __name__ == "__main__":
    main()
