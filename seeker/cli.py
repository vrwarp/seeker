"""CLI entry-point for the Seeker daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from seeker.config import load_config, SeekerConfig


def _setup_logging(config: SeekerConfig) -> None:
    """Configure the root logger based on config."""
    handlers: list[logging.Handler] = []
    if config.logging.console:
        handlers.append(logging.StreamHandler(sys.stderr))
    if config.logging.file:
        handlers.append(logging.FileHandler(config.logging.file))

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _rotate_log(log_file: str) -> None:
    """Move the current log file to ./logs/ with a timestamp, then clear it."""
    from datetime import datetime
    log_path = Path(log_file)
    if not log_path.exists() or log_path.stat().st_size == 0:
        return
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = logs_dir / f"seeker_{ts}.log"
    log_path.rename(dest)


def cmd_start(args: argparse.Namespace) -> None:
    """Start the Seeker daemon."""
    config = load_config(args.config)
    _rotate_log(config.logging.file)
    _setup_logging(config)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.manuscript:
        config.prompt.manuscript = args.manuscript
    if args.audio_file:
        config.audio.audio_file = args.audio_file
    if args.mode:
        config.prompt.mode = args.mode
    if args.anticipation is not None:
        config.prompt.anticipation_seconds = args.anticipation
    if args.arrangement:
        config.prompt.arrangement_pdf = args.arrangement

    from seeker.daemon import OperatorServer, SeekerDaemon

    async def _run() -> None:
        daemon = SeekerDaemon(config)
        server = OperatorServer(daemon, config)
        await server.start()
        await daemon.start(manuscript_path=config.prompt.manuscript or None)

    asyncio.run(_run())


def cmd_devices(_args: argparse.Namespace) -> None:
    """List available audio input devices."""
    from seeker.audio_capture import AudioCapture

    devices = AudioCapture.list_devices()
    if not devices:
        print("No audio input devices found.")
        return
    print(f"{'Index':<8}{'Name':<40}{'Channels':<10}{'Sample Rate'}")
    print("-" * 72)
    for d in devices:
        print(f"{d.index:<8}{d.name:<40}{d.max_input_channels:<10}{d.default_sample_rate:.0f}")


def cmd_test_pp(args: argparse.Namespace) -> None:
    """Test ProPresenter API connectivity."""
    config = load_config(args.config)
    _setup_logging(config)

    import aiohttp
    from seeker.propresenter_client import ProPresenterClient

    async def _run() -> None:
        async with aiohttp.ClientSession() as session:
            client = ProPresenterClient(config.propresenter, session)
            ok = await client.health_check()
            if ok:
                print(f"✓ ProPresenter reachable at {client.base_url}")
                pres = await client.get_active_presentation()
                if pres:
                    print(f"  Active presentation: {pres.name} ({pres.slide_count} slides)")
                else:
                    print("  No active presentation.")
            else:
                print(f"✗ Cannot reach ProPresenter at {client.base_url}")
                sys.exit(1)

    asyncio.run(_run())


def cmd_test_audio(args: argparse.Namespace) -> None:
    """Capture a short audio clip and save as raw PCM for verification."""
    config = load_config(args.config)
    _setup_logging(config)

    import struct
    from pathlib import Path

    duration = args.duration
    output = Path(args.output)

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=config.audio.queue_max_size)

    from seeker.audio_capture import AudioCapture

    capture = AudioCapture(config.audio, queue)
    capture.start()

    chunks_needed = int(duration * 1000 / config.audio.chunk_duration_ms)
    collected: list[bytes] = []

    print(f"Recording {duration}s of audio ({chunks_needed} chunks)...")
    try:
        for _ in range(chunks_needed):
            capture._capture_loop.__func__  # just ensure method exists
            data = capture._stream.read(config.audio.chunk_frames, exception_on_overflow=False)
            collected.append(data)
    finally:
        capture.stop()

    output.write_bytes(b"".join(collected))
    print(f"Saved to {output}")
    print(f"Play with: ffplay -f s16le -ar {config.audio.sample_rate} -ac {config.audio.channels} {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seeker",
        description="Automated sermon slide synchronization daemon",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # start
    sp_start = sub.add_parser("start", help="Start the daemon")
    sp_start.add_argument("--manuscript", "-m", help="Path to sermon manuscript file")
    sp_start.add_argument("--audio-file", "-f", help="Path to an audio file (e.g. mp3) to ingest instead of live audio")
    sp_start.add_argument("--auto-activate", action="store_true", help="Auto-activate when PP sermon detected")
    sp_start.add_argument("--mode", choices=["sermon", "song"], default="sermon", help="Tracking mode")
    sp_start.add_argument("--anticipation", type=float, default=None, help="Predictive lead time in seconds for song mode")
    sp_start.add_argument("--arrangement", help="Path to arrangement sheet PDF for song structure context")

    # devices
    sub.add_parser("devices", help="List available audio input devices")

    # test-pp
    sub.add_parser("test-pp", help="Test ProPresenter connectivity")

    # test-audio
    sp_audio = sub.add_parser("test-audio", help="Record a short audio test clip")
    sp_audio.add_argument("--duration", type=float, default=5.0, help="Seconds to record (default: 5)")
    sp_audio.add_argument("--output", default="test_audio.raw", help="Output file path")

    # version
    sub.add_parser("version", help="Show version")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        cmd = args.command
        if cmd == "start":
            cmd_start(args)
        elif cmd == "devices":
            cmd_devices(args)
        elif cmd == "test-pp":
            cmd_test_pp(args)
        elif cmd == "test-audio":
            cmd_test_audio(args)
        elif cmd == "version":
            from seeker import __version__
            print(f"seeker {__version__}")
    finally:
        logging.shutdown()


if __name__ == "__main__":
    main()
