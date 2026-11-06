"""Offline checks of the runnable entry point, media wiring and deployment files.

No Docker, Discord, token, mpg123 or audio is used: small Python children stand
in for the decoder and a temporary directory stands in for the cgroup root.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import cast

import pytest
import yaml  # pyright: ignore[reportMissingTypeStubs]

from sobafm import __main__ as app
from sobafm import discord_adapter
from sobafm.catalog import RecordingKey, Source
from sobafm.decode import DecodeInput, DecoderCommand
from sobafm.discord_adapter import (
    CATALOG_VARIABLE,
    GEMINI_KEY_VARIABLE,
    REVIEWS_VARIABLE,
    TOKEN_VARIABLE,
    Adapter,
    Config,
    Producer,
)
from sobafm.media import MediaInputError, MediaMissingError, MediaProducer, parse_media
from sobafm.pcm import FRAME_BYTES, PcmBankPool
from sobafm.settings import SettingsError, SettingsStore
from tests.test_discord_adapter import FakeSettings, recording

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "placeholder-token-value"  # pragma: allowlist secret - test placeholder
GEMINI_KEY = "placeholder-gemini-value"  # pragma: allowlist secret - test placeholder
PAYLOAD = bytes(range(256)) * 8
LIMITS = {"memory.max": "90177536\n", "memory.swap.max": "0\n", "pids.max": "64\n"}


def cgroup(tmp_path: Path, **values: str) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    for name, value in (LIMITS | {k.replace("_", "."): v for k, v in values.items()}).items():
        (root / name).write_text(value)
    return root


def entry(key: RecordingKey, /, file: str = "song.mp3", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "key": {"source": str(key.source), "recording_id": key.recording_id},
        "file": file,
        "size": len(PAYLOAD),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "max_pcm_bytes": 4 * FRAME_BYTES,
    }
    return item | overrides


def manifest(*entries: dict[str, object]) -> str:
    return json.dumps({"version": 1, "recordings": list(entries)})


def child(script: str) -> DecoderCommand:
    return DecoderCommand(argv=(sys.executable, "-I", "-c", script), limits=(), env={})


# Limit gate.


def test_limit_gate_accepts_exactly_the_adr_values(tmp_path: Path) -> None:
    app.check_limits(cgroup(tmp_path))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory.max", "max\n"),
        ("memory.max", "90177537\n"),
        ("memory.max", "268435456\n"),
        ("memory.swap.max", "max\n"),
        ("memory.swap.max", "90177536\n"),
        ("pids.max", "max\n"),
        ("pids.max", "65\n"),
        ("pids.max", "6 4\n"),
        ("pids.max", "64" + " " * 80),
    ],
)
def test_limit_gate_refuses_each_wrong_value_before_any_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    root = cgroup(tmp_path, **{name.replace(".", "_"): value})
    with pytest.raises(app.LimitError, match=re.escape(name)):
        app.check_limits(root)
    read: list[str] = []
    monkeypatch.setattr(app, "load_startup", lambda: read.append("config"))
    assert app.main(root=root, runner=never_run) == app.EXIT_LIMITS
    assert read == []


@pytest.mark.parametrize("name", list(LIMITS))
def test_limit_gate_refuses_a_missing_file(tmp_path: Path, name: str) -> None:
    root = cgroup(tmp_path)
    (root / name).unlink()
    with pytest.raises(app.LimitError, match=re.escape(name)):
        app.check_limits(root)


async def never_run(config: Config, producer: Producer, store: SettingsStore) -> int:
    raise AssertionError("must not run")


# Configuration.


DEMO = ROOT / "evaluations" / "catalog"
DEMO_KEY = RecordingKey(Source.JAMENDO, "demo-j-fit")


def write_snapshot(directory: Path) -> tuple[Path, Path, Path]:
    """Copy the synthetic demo snapshot and reviews beside a media manifest."""
    paths = (directory / "catalog.json", directory / "reviews.json", directory / "media.json")
    paths[0].write_bytes((DEMO / "selector-demo.json").read_bytes())
    paths[1].write_bytes((DEMO / "selector-demo-reviews.json").read_bytes())
    paths[2].write_text(manifest(entry(DEMO_KEY)))
    return paths


def environment(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in (TOKEN_VARIABLE, CATALOG_VARIABLE, REVIEWS_VARIABLE, app.MEDIA_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(GEMINI_KEY_VARIABLE, GEMINI_KEY)
    monkeypatch.setenv(app.DATABASE_VARIABLE, "unused-unless-a-test-opens-it.db")
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_token_only_configuration_has_no_catalog_or_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment(monkeypatch, SOBAFM_DISCORD_TOKEN=TOKEN)
    startup = app.load_startup()
    assert startup.config.matcher is None and startup.media == {}


def test_media_must_accompany_the_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    catalog_path, reviews_path, media_path = write_snapshot(tmp_path)
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN=TOKEN,
        SOBAFM_CATALOG=str(catalog_path),
        SOBAFM_REVIEWS=str(reviews_path),
    )
    with pytest.raises(app.ConfigError, match="SOBAFM_MEDIA is required"):
        app.load_startup()
    environment(monkeypatch, SOBAFM_DISCORD_TOKEN=TOKEN, SOBAFM_MEDIA=str(media_path))
    with pytest.raises(app.ConfigError, match="requires SOBAFM_CATALOG"):
        app.load_startup()


def test_the_database_location_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch, SOBAFM_DISCORD_TOKEN=TOKEN)
    monkeypatch.delenv(app.DATABASE_VARIABLE)
    with pytest.raises(app.ConfigError, match="SOBAFM_DATABASE is required"):
        app.load_startup()


def test_full_configuration_resolves_media_beside_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog_path, reviews_path, media_path = write_snapshot(tmp_path)
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN=TOKEN,
        SOBAFM_CATALOG=str(catalog_path),
        SOBAFM_REVIEWS=str(reviews_path),
        SOBAFM_MEDIA=str(media_path),
    )
    startup = app.load_startup()
    assert startup.config.matcher is not None
    assert GEMINI_KEY not in repr(startup)
    (source,) = startup.media.values()
    assert source.path == tmp_path / "song.mp3"
    assert TOKEN_VARIABLE not in app.os.environ
    assert GEMINI_KEY_VARIABLE not in app.os.environ


def test_an_unreadable_media_manifest_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog_path, reviews_path, media_path = write_snapshot(tmp_path)
    media_path.write_text("{")
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN=TOKEN,
        SOBAFM_CATALOG=str(catalog_path),
        SOBAFM_REVIEWS=str(reviews_path),
        SOBAFM_MEDIA=str(media_path),
    )
    with pytest.raises(app.ConfigError, match="media manifest could not be loaded"):
        app.load_startup()


def test_missing_token_exits_with_a_message_that_never_prints_a_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    catalog_path, reviews_path, media_path = write_snapshot(tmp_path)
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN="   ",
        SOBAFM_CATALOG=str(catalog_path),
        SOBAFM_REVIEWS=str(reviews_path),
        SOBAFM_MEDIA=str(media_path),
    )
    assert app.main(root=cgroup(tmp_path), runner=never_run) == app.EXIT_CONFIG
    text = caplog.text
    assert "SOBAFM_DISCORD_TOKEN is required" in text
    for value in (str(catalog_path), str(media_path), str(tmp_path)):
        assert value not in text


def test_a_configuration_error_never_prints_the_token_or_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    catalog_path, reviews_path, _ = write_snapshot(tmp_path)
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN=TOKEN,
        SOBAFM_CATALOG=str(catalog_path),
        SOBAFM_REVIEWS=str(reviews_path),
        SOBAFM_MEDIA=str(tmp_path / "absent.json"),
    )
    assert app.main(root=cgroup(tmp_path), runner=never_run) == app.EXIT_CONFIG
    text = caplog.text
    assert TOKEN not in text
    assert str(tmp_path) not in text


# The settings database at startup.


def test_a_database_that_cannot_be_opened_stops_startup_before_discord(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN=TOKEN,
        SOBAFM_DATABASE=str(tmp_path / "absent" / "sobafm.db"),
    )
    assert app.main(root=cgroup(tmp_path), runner=never_run) == app.EXIT_DATABASE
    assert "settings database refused: SQLITE_CANTOPEN" in caplog.text
    assert TOKEN not in caplog.text and str(tmp_path) not in caplog.text


def test_a_database_that_is_not_one_stops_startup_and_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "sobafm.db"
    database.write_bytes(b"not a database, and long enough to be read as one" * 100)
    before = database.read_bytes()
    environment(monkeypatch, SOBAFM_DISCORD_TOKEN=TOKEN, SOBAFM_DATABASE=str(database))
    assert app.main(root=cgroup(tmp_path), runner=never_run) == app.EXIT_DATABASE
    assert "settings database refused: SQLITE_NOTADB" in caplog.text
    assert database.read_bytes() == before


def test_a_refused_limit_gate_never_creates_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "sobafm.db"
    environment(monkeypatch, SOBAFM_DISCORD_TOKEN=TOKEN, SOBAFM_DATABASE=str(database))
    root = cgroup(tmp_path, pids_max="65\n")
    assert app.main(root=root, runner=never_run) == app.EXIT_LIMITS
    assert not database.exists()


def test_a_configuration_error_never_creates_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "sobafm.db"
    environment(monkeypatch, SOBAFM_DATABASE=str(database))
    assert app.main(root=cgroup(tmp_path), runner=never_run) == app.EXIT_CONFIG
    assert not database.exists()


# Media manifest.


KEY = RecordingKey(Source.JAMENDO, "1")


@pytest.mark.parametrize(
    "document",
    [
        manifest(entry(KEY, file="/etc/song.mp3")),
        manifest(entry(KEY, file="../song.mp3")),
        manifest(entry(KEY, file="a/../song.mp3")),
        manifest(entry(KEY, file="a//song.mp3")),
        manifest(entry(KEY, file="a\\song.mp3")),
        manifest(entry(KEY, file="song\n.mp3")),
        manifest(entry(KEY, size=0)),
        manifest(entry(KEY, size=True)),
        manifest(entry(KEY, sha256="A" * 64)),
        manifest(entry(KEY, max_pcm_bytes=FRAME_BYTES + 1)),
        manifest(entry(KEY, max_pcm_bytes=4 * 3_600 * 192_000 + FRAME_BYTES)),
        manifest(entry(KEY), entry(KEY)),
        manifest(entry(KEY, extra=1)),
        manifest(entry(KEY, key={"source": "other", "recording_id": "1"})),
        json.dumps({"version": 2, "recordings": []}),
        '{"version": 1, "version": 1, "recordings": []}',
        "[]",
    ],
)
def test_media_manifest_rejects_invalid_entries(document: str, tmp_path: Path) -> None:
    with pytest.raises(MediaInputError):
        parse_media(document, tmp_path)


def test_media_manifest_binds_exact_identity(tmp_path: Path) -> None:
    media = parse_media(manifest(entry(KEY, file="jamendo/song.mp3")), tmp_path)
    assert media == {
        KEY: DecodeInput(
            tmp_path / "jamendo" / "song.mp3",
            len(PAYLOAD),
            hashlib.sha256(PAYLOAD).hexdigest(),
            4 * FRAME_BYTES,
        )
    }


# Wiring: the decode-backed producer through the adapter's owner and pool.


def media_for(tmp_path: Path) -> dict[RecordingKey, DecodeInput]:
    (tmp_path / "song.mp3").write_bytes(PAYLOAD)
    return parse_media(manifest(entry(KEY)), tmp_path)


def test_main_runs_the_gate_then_connects_the_media_producer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog_path, reviews_path, media_path = write_snapshot(tmp_path)
    database = tmp_path / "sobafm.db"
    environment(
        monkeypatch,
        SOBAFM_DISCORD_TOKEN=TOKEN,
        SOBAFM_CATALOG=str(catalog_path),
        SOBAFM_REVIEWS=str(reviews_path),
        SOBAFM_MEDIA=str(media_path),
        SOBAFM_DATABASE=str(database),
    )
    seen: list[tuple[Config, Producer, SettingsStore]] = []

    async def runner(config: Config, producer: Producer, store: SettingsStore) -> int:
        seen.append((config, producer, store))
        assert await store.close() is True
        return 0

    assert app.main(root=cgroup(tmp_path), runner=runner) == 0
    ((config, producer, store),) = seen
    assert config.matcher is not None and isinstance(producer, MediaProducer)
    assert isinstance(store, SettingsStore) and database.exists()


class FakeBot:
    """Stands in for the Discord client: it starts, stops at once and never connects."""

    def __init__(self) -> None:
        self.closed = False

    async def start(self, token: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def test_run_closes_the_database_on_shutdown_and_reports_a_close_that_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bot = FakeBot()

    def fake_bot(adapter: Adapter) -> FakeBot:
        return bot

    monkeypatch.setattr(discord_adapter, "create_bot", fake_bot)
    config = Config(TOKEN, None)

    async def clean() -> None:
        store = SettingsStore.open(tmp_path / "sobafm.db")
        assert await discord_adapter.run(config, MediaProducer({}), store) == 0
        assert bot.closed
        with pytest.raises(SettingsError, match="closed"):
            await store.update(1, volume_percent=20)

    asyncio.run(clean())

    class StuckStore(SettingsStore):
        async def close(self) -> bool:
            await super().close()
            return False

    async def stuck() -> None:
        store = SettingsStore.open(tmp_path / "sobafm.db")
        store.__class__ = StuckStore
        assert await discord_adapter.run(config, MediaProducer({}), store) == 1

    asyncio.run(stuck())


def test_media_producer_fills_the_adapters_bank_and_leaves_the_lease_to_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pool = PcmBankPool()
        adapter = Adapter(None, MediaProducer({}), FakeSettings(), pool=pool)
        producer = MediaProducer(
            media_for(tmp_path),
            command=child(
                "import sys\nsys.stdin.buffer.read()\n"
                f"sys.stdout.buffer.write(b'\\x02' * {2 * FRAME_BYTES})\n"
            ),
        )
        bank = pool.acquire()
        await producer.produce(recording("1"), bank)
        facts = bank.facts()
        assert facts.pcm_valid and facts.transfer_complete and facts.decoder_complete
        assert not facts.released and pool.leased == 1  # still the adapter's lease
        assert bank.read_frame() == b"\x02" * FRAME_BYTES
        assert bank.release() and pool.leased == 0
        assert adapter.debt == []

    asyncio.run(scenario())


def test_media_producer_settles_the_decoder_before_a_cancellation_returns(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"

    async def scenario() -> None:
        pool = PcmBankPool()
        producer = MediaProducer(
            media_for(tmp_path),
            command=child(
                # Ignore TERM and hang, so only the owner's KILL ends the decoder.
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({str(started)!r}, 'w').close()\n"
                "while True: time.sleep(60)\n"
            ),
        )
        bank = pool.acquire()
        task = asyncio.create_task(producer.produce(recording("1"), bank))
        while not started.exists():  # wait for a condition, not for a race
            await asyncio.sleep(0.01)
        bank.cancel()  # the adapter's retirement: bank first, then the task
        first = bank.facts().cancelled_at
        assert first is not None
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # repeated cancellation keeps the same owner and deadline
        with pytest.raises(asyncio.CancelledError):
            await task
        # Settled (killed and reaped) within the original deadline of the bank's
        # first cancellation, and only then did the producer return.
        assert time.monotonic() - first <= 3.0
        assert bank.facts().cancelled_at == first
        assert bank.release() and pool.leased == 0

    asyncio.run(scenario())


def test_media_producer_refuses_a_recording_without_media(tmp_path: Path) -> None:
    async def scenario() -> None:
        pool = PcmBankPool()
        bank = pool.acquire()
        with pytest.raises(MediaMissingError):
            await MediaProducer({}).produce(recording("1"), bank)
        assert bank.release()

    asyncio.run(scenario())


def test_nothing_imports_from_evaluations() -> None:
    for path in (ROOT / "src" / "sobafm").glob("*.py"):
        assert re.search(r"^\s*(from|import)\s+evaluations\b", path.read_text(), re.M) is None


# Deployment files.


def compose_service() -> dict[str, object]:
    document = cast(
        dict[str, dict[str, dict[str, object]]],
        yaml.safe_load((ROOT / "compose.yaml").read_text()),  # pyright: ignore[reportUnknownMemberType]
    )
    (service,) = document["services"].values()
    return service


def test_compose_carries_the_adr_limits_and_hardening() -> None:
    service = compose_service()
    assert service["mem_limit"] == 90_177_536 and service["memswap_limit"] == 90_177_536
    assert service["pids_limit"] == 64 and service["cpus"] == 2
    assert service["init"] is True and service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["restart"] == "on-failure:5"
    assert service["platform"] == "linux/amd64"
    assert service["env_file"] == ".env"
    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "1m", "max-file": "2"},
    }
    for forbidden in ("privileged", "cap_add", "devices", "network_mode", "user", "cgroup"):
        assert forbidden not in service
    assert (
        ("memory.max", str(service["mem_limit"])),
        ("memory.swap.max", "0"),
        ("pids.max", str(service["pids_limit"])),
    ) == app.REQUIRED_LIMITS


def test_only_data_is_writable_and_it_persists_for_the_non_root_user() -> None:
    """The layout in ADR 0009: read-only catalog, one named volume, and a read-only root."""
    service = compose_service()
    assert service["read_only"] is True
    assert service["volumes"] == ["./catalog:/catalog:ro", "data:/data"]
    assert "tmpfs" not in service
    document = cast(
        dict[str, dict[str, object]],
        yaml.safe_load((ROOT / "compose.yaml").read_text()),  # pyright: ignore[reportUnknownMemberType]
    )
    assert document["volumes"] == {"data": {"name": "sobafm-data"}}
    # A new named volume takes the ownership of the image's own directory.
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "mkdir -p /data" in dockerfile and "chown 1000:1000 /data" in dockerfile


def test_the_example_paths_match_the_volumes() -> None:
    lines = (ROOT / ".env.example").read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert values[app.DATABASE_VARIABLE].startswith("/data/")
    for name in (CATALOG_VARIABLE, REVIEWS_VARIABLE, app.MEDIA_VARIABLE):
        assert values[name].startswith("/catalog/")


def test_a_host_catalog_directory_stays_out_of_git() -> None:
    assert "/catalog/" in (ROOT / ".gitignore").read_text().splitlines()
    # Everything is excluded from the build context unless listed, audio included.
    assert (ROOT / ".dockerignore").read_text().splitlines()[0] == "*"


def test_dockerfile_runs_non_root_with_the_reviewed_decoder_and_no_ffmpeg() -> None:
    lines = [
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if not line.startswith("#")
    ]
    text = "\n".join(lines)
    users = [line for line in lines if line.startswith("USER ")]
    assert users[-1] == "USER 1000:1000"
    assert text.rindex("USER 1000:1000") > text.index("apt-get install")
    for package in ("mpg123", "libmpg123-0t64", "libout123-0t64", "libsyn123-0t64"):
        assert f"{package}=1.33.7-1" in text
    assert "libopus0=1.5.2-2" in text and "--no-install-recommends" in text
    assert "uv sync --locked --no-default-groups" in text
    assert "ffmpeg" not in text.lower()
    assert '"-m", "sobafm"' in text


def test_env_example_holds_placeholders_only() -> None:
    lines = (ROOT / ".env.example").read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert set(values) == {
        TOKEN_VARIABLE,
        GEMINI_KEY_VARIABLE,
        CATALOG_VARIABLE,
        REVIEWS_VARIABLE,
        app.MEDIA_VARIABLE,
        app.DATABASE_VARIABLE,
    }
    assert values[TOKEN_VARIABLE].startswith("replace-with-")
    for value in values.values():
        # Discord tokens are three base64url segments; nothing here resembles one.
        assert re.fullmatch(r"[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}", value) is None
        assert value.startswith(("replace-with-", "/data/", "/catalog/"))
