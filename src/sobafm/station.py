"""A station: one server's program, decks, and mixer, run by a reconcile loop (ADR-0004)."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from typing import Any, Literal, Protocol

import discord

from sobafm.audio_budget import AudioLedger
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.deck import Connect, Deck
from sobafm.engine import Engine
from sobafm.lyria_engine import CONNECT_BACKOFF_S as CONNECT_BACKOFF_S
from sobafm.lyria_engine import FAILED_STARTS as FAILED_STARTS
from sobafm.lyria_engine import HANDOVER_BELOW_S as HANDOVER_BELOW_S
from sobafm.lyria_engine import REFUSAL_RETRY_S as REFUSAL_RETRY_S
from sobafm.lyria_engine import SESSION_LIMIT_S as SESSION_LIMIT_S
from sobafm.lyria_engine import SESSIONS_PER_STATION as SESSIONS_PER_STATION
from sobafm.lyria_engine import START_OUTCOMES as START_OUTCOMES
from sobafm.lyria_engine import LyriaEngine
from sobafm.lyria_engine import SessionPool as SessionPool
from sobafm.mixer import Mixer
from sobafm.pcm import FRAME_SECONDS
from sobafm.plan import MusicPlan
from sobafm.program import Mode
from sobafm.program import Outcome as Outcome
from sobafm.program import Program as Program
from sobafm.source import Source
from sobafm.status import Statuses

log = logging.getLogger(__name__)

TICK_S = 0.25
CROSSFADE_S = 4.0
FADE_IN_S = 2.0
FADE_OUT_S = 3.0
PLAYER_RETRY_S = 1.0  # starting discord.py's player, at most once per interval
RECOVERED_S = 1.0  # audio after a run of underruns that ends it, so a stutter is one run
EMPTY_GRACE_S = 60.0  # how long a program plays to an empty channel (PLAY-4)


class Player(Protocol):
    """The part of discord.py's voice client a station uses."""

    def is_connected(self) -> bool: ...

    def is_playing(self) -> bool: ...

    def play(
        self,
        source: discord.AudioSource,
        *,
        after: Callable[[Exception | None], Any] | None = None,
        signal_type: Literal["auto", "voice", "music"] = "auto",
    ) -> None: ...

    def stop(self) -> None: ...


class Station:
    """Plays one server's program. Only `reconcile()` and `close()` change decks."""

    def __init__(
        self,
        voice: Callable[[], Player | None],
        listeners: Callable[[], int],
        connect: Connect | None = None,
        pool: SessionPool | None = None,
        *,
        volume: float,
        statuses: Statuses,
        engines: Sequence[Engine] | None = None,
        audio: AudioLedger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mixer = Mixer(volume)
        self.program: Program | None = None
        # What played when the program was requested, which resumes if the program can't start
        self._previous: Program | None = None
        if engines is None:
            if connect is None or pool is None:
                raise ValueError("Lyria requires a connector and the process-wide session pool")
            self.audio = audio if audio is not None else AudioLedger()
            engines = [LyriaEngine(connect, pool, audio=self.audio, clock=clock)]
        else:
            if not engines:
                raise ValueError("a station requires an engine")
            self.audio = audio if audio is not None else engines[0].audio
        self.engines = {engine.mode: engine for engine in engines}
        if len(self.engines) != len(engines) or any(e.audio is not self.audio for e in engines):
            raise ValueError("one engine per mode must share the station audio ledger")
        self._closed = False
        self._closing: asyncio.Task[None] | None = None
        self._voice = voice
        self._listeners = listeners
        self._empty_since: float | None = None
        # The mixer's counts of underruns and of reads with audio when last watched: at each
        # tick, and before the program heard changes
        self._underruns_seen = 0
        self._played_seen = 0
        self._dry_since: int | None = None  # the count when the current run of silence began
        self._recovered = 0  # reads with audio since the run's last underrun
        self._clock = clock
        self._playing: object | None = None  # identifies the running player, if any
        self._heard_source: Source | None = None
        self._next_play = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.statuses = statuses  # the server's, which outlive this station
        self._status_held = False  # while SobaFM moves

    @property
    def decks(self) -> list[Deck]:
        """The Lyria engine's retained decks, including playable ended buffers."""
        return [source for source in self.sources if isinstance(source, Deck)]

    @property
    def sources(self) -> list[Source]:
        return [source for engine in self.engines.values() for source in engine.sources]

    @property
    def time_left(self) -> float | None:
        """Seconds until the program ends, or None without one."""
        if self.program is None or self.program.ends_at is None:
            return None
        return max(0.0, self.program.ends_at - self._clock())

    def play(
        self,
        plan: MusicPlan | CatalogPlan,
        requester: str,
        *,
        duration_seconds: float | None = None,
    ) -> asyncio.Future[Outcome]:
        """Start or replace the program, which ends `duration_seconds` from now.

        The result resolves once it plays or fails.
        """
        ends_at = self._clock() + duration_seconds if duration_seconds is not None else None
        program = Program(plan, requester, ends_at=ends_at)
        if self._closed:
            program.settle(Outcome.STOPPED)
            return program.started
        self._accept_handoff(self._voice())
        engine = self.engines.get(program.mode)
        if engine is None:
            program.settle(Outcome.UNAVAILABLE)
            return program.started
        if program.mode is Mode.CATALOG and not self.audio.bounded:
            program.settle(Outcome.UNAVAILABLE)
            return program.started
        if not engine.reserve(program):
            log.info("No playback source capacity left")
            program.settle(Outcome.BUSY)
            return program.started
        if self.program is not None:
            self.program.settle(Outcome.REPLACED)
            if self.program.playing:
                self._previous = self.program
        self.program = program
        self._bind_mixer()
        self._empty_since = None
        self.wake()
        return program.started

    def stop(self) -> None:
        self._accept_handoff(self._voice())
        self._finish(Outcome.STOPPED)
        self.wake()

    def start(self) -> None:
        """Run the reconcile loop in the background until `close()`."""
        self._task = asyncio.create_task(self._run(), name="station")

    def wake(self) -> None:
        """Reconcile now rather than at the next tick."""
        self._wake.set()

    def invalidate_playback(self) -> None:
        """Invalidate an armed boundary before a voice transition can await anything."""
        self.mixer.invalidate_handoff()
        self.wake()

    def _bind_mixer(self) -> None:
        loop = asyncio.get_running_loop()

        def wake() -> None:
            loop.call_soon_threadsafe(self.wake)

        self.mixer.bind(
            self.program,
            self._playing,
            lambda: not self._status_held and self._audible(self._voice()),
            wake,
        )

    def _accept_handoff(self, player: Player | None) -> None:
        handoff = self.mixer.take_handoff()
        if (
            handoff is None
            or handoff.program is not self.program
            or handoff.epoch is not self._playing
            or not self._audible(player)
            or self.program is None
        ):
            return
        self._settle_playing(self.program, handoff.incoming)
        if handoff.outgoing is not None and handoff.outgoing is not handoff.incoming:
            handoff.outgoing.retire()

    async def close(self, outcome: Outcome = Outcome.STOPPED) -> None:
        """Invalidate playback now; one cleanup owner survives repeated caller cancellation."""
        if self._closing is None:
            self._closed = True
            self._finish(outcome)
            self._closing = asyncio.create_task(self._close(), name="station cleanup")
        await asyncio.shield(self._closing)

    async def _close(self) -> None:
        """Detach playback, retire all engines, and stop the loop.

        It also clears the voice channel status, which needs SobaFM still in the channel.
        """
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.mixer.switch_to(None, 0)
        if (player := self._voice()) is not None:
            player.stop()
        self._playing = None
        self._heard_source = None
        await asyncio.gather(*(engine.close() for engine in self.engines.values()))
        await self.statuses.clear()

    @contextlib.asynccontextmanager
    async def moving(self) -> AsyncGenerator[None]:
        """Clear the voice channel status before SobaFM moves, and show it again after."""
        self._status_held = True
        self.invalidate_playback()
        try:
            await self.statuses.clear()
            yield
        finally:
            self._status_held = False

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.reconcile()
            except Exception:  # a failed tick must not stop playback; the next tick retries
                log.exception("Reconcile failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(TICK_S):
                    await self._wake.wait()

    async def reconcile(self) -> None:
        """Apply the reconcile rules once, in order."""
        if self._closed:
            return
        self._accept_handoff(self._voice())
        self._watch_underruns()
        player = self._voice()
        if self.program is not None and player is None:
            log.info("Voice connection lost; ending the program")
            self._finish(Outcome.DISCONNECTED)
        if (
            self.program is not None
            and self.program.ends_at is not None
            and self._clock() >= self.program.ends_at
        ):
            log.info("Play duration elapsed; ending the program")
            self._finish(Outcome.STOPPED)
        if self.program is not None and self._abandoned():
            log.info("No listeners for %d seconds; ending the program", EMPTY_GRACE_S)
            self._finish(Outcome.STOPPED)
        self._discard(player)
        if self.program is not None:
            self._retire(self.program)
            self.engines[self.program.mode].generate(self.program)
        if self.program is not None:
            self._start(self.program, player)
        if self.program is not None:  # starting the player can end the program
            self._hand_over(self.program, player)
        else:
            self._wind_down(player)
        retained = [p for p in (self.program, self._previous) if p is not None]
        for engine in self.engines.values():
            engine.release_if_ended(retained)
        await asyncio.gather(*(source.regulate() for source in self.sources))
        self._sync_status()

    def _sync_status(self) -> None:
        """Show the title being heard as the voice channel status (FB-2).

        A program shows its title once it plays, and a replacement leaves the title still heard
        until it plays itself, including after a move. A drag to another channel is caught up on
        the next tick.
        """
        if not self._status_held:
            heard = self._program_heard()
            source = self.heard_source
            self.statuses.show(source.title if heard is not None and source is not None else None)

    def _abandoned(self) -> bool:
        """Whether the channel has had no listeners for the whole grace period."""
        if self._listeners() > 0:
            self._empty_since = None
            return False
        now = self._clock()
        if self._empty_since is None:
            self._empty_since = now
        return now - self._empty_since >= EMPTY_GRACE_S

    def _finish(self, outcome: Outcome) -> None:
        self.mixer.invalidate_handoff()
        self._watch_underruns()  # silence since the last tick, so the next tick won't count it
        if (heard := self._program_heard()) is not None:
            self._end_run()
            self._log_ended(heard, outcome)
        if self.program is not None:
            self.program.settle(outcome)
            self.program = None
        self._previous = None
        if self._heard_source is not None and self._heard_source.mode is Mode.CATALOG:
            self.mixer.switch_to(None, 0)
            self._heard_source = None

    def _give_up(self, outcome: Outcome) -> None:
        """End a program that could not start, returning to the program still heard (FB-3)
        unless its end time has passed."""
        previous = self._previous
        if previous is None or self.program is None:
            self._finish(outcome)
            return
        self.program.settle(outcome)  # it never played
        if previous.ends_at is not None and self._clock() >= previous.ends_at:
            log.info("Play duration elapsed; ending the program")
            self._finish(Outcome.STOPPED)
            return
        self.program, self._previous = previous, None
        log.info("Returning to the program still playing")

    def _settle_playing(self, program: Program, source: Source) -> None:
        """Settle `program` as playing, so the program it replaced is no longer heard."""
        self._watch_underruns()
        program.settle(Outcome.PLAYING)
        self._heard_source = source
        self.engines[program.mode].going_live(source)
        if (previous := self._previous) is not None:
            self._log_ended(previous, Outcome.REPLACED)
            self._previous = None

    @property
    def heard_source(self) -> Source | None:
        """The accepted source, including the incoming target during a generated fade."""
        return self._heard_source

    def _program_heard(self) -> Program | None:
        """The program listeners hear: the program once it plays, or the one it is replacing."""
        if self.program is not None and self.program.playing:
            return self.program
        return self._previous

    def _watch_underruns(self) -> None:
        """Count new underruns toward the program heard (NFR-2), and log each run of silence in
        it as it starts, and its length once audio plays for a while.

        It runs at each tick and before the program heard changes, so each underrun counts
        once. Reads stop while discord.py reconnects to voice, which ends nothing, and
        RECOVERED_S of audio must play before a run ends, so a stutter logs as one run.
        """
        # Written on discord.py's player thread. Each is read once, so a run and a total agree.
        underruns, played = self.mixer.underruns, self.mixer.played
        new_underruns, new_played = underruns - self._underruns_seen, played - self._played_seen
        self._underruns_seen, self._played_seen = underruns, played
        if (heard := self._program_heard()) is not None:
            heard.underruns += new_underruns
        if new_underruns:
            if self._dry_since is None and heard is not None:
                self._dry_since = underruns - new_underruns
                log.warning("The live deck ran dry; playing silence")
            self._recovered = 0
        elif self._dry_since is not None:
            self._recovered += new_played
            if self._recovered * FRAME_SECONDS >= RECOVERED_S:
                self._end_run()

    def _end_run(self) -> None:
        """Log the length of the current run of silence, if there is one."""
        if self._dry_since is not None:
            seconds = (self._underruns_seen - self._dry_since) * FRAME_SECONDS
            log.info("Silence lasted %.2f s", seconds)
            self._dry_since, self._recovered = None, 0

    @staticmethod
    def _log_ended(program: Program, outcome: Outcome) -> None:
        seconds = program.underruns * FRAME_SECONDS
        log.info("Program ended (%s) with %.2f s of underrun", outcome, seconds)

    def _wind_down(self, player: Player | None) -> None:
        """Without a program: drop idle decks, fade out, stop the player, and free sessions."""
        for source in self.sources:
            if not self.mixer.uses(source):
                source.retire()
        if self._playing is None or player is None:  # unread fades cannot finish
            self.mixer.switch_to(None, 0)
            self._heard_source = None
            return
        if self.heard_source is not None and self.heard_source.mode is Mode.CATALOG:
            self.mixer.switch_to(None, 0)
            self._heard_source = None
        if self.mixer.switching:
            return
        if self.mixer.live is not None:
            self.mixer.switch_to(None, FADE_OUT_S)
        else:
            self._heard_source = None
        if self.mixer.live is None and player.is_playing():
            player.stop()
            self._playing = None

    def _retire(self, program: Program) -> None:
        """End sessions near Lyria's limit, and idle sessions of a replaced plan."""
        for engine in self.engines.values():
            engine.retire(program if program.mode is engine.mode else None, self.mixer)

    def _start(self, program: Program, player: Player | None) -> None:
        """Keep discord.py's player running, and fade in the first ready deck once it runs."""
        if player is None:
            return
        # Check switching first: a player can finish a fade between property
        # reads. Once not switching, only this event loop can change the target.
        idle = not self.mixer.switching and self.mixer.live is None
        deck = self.engines[program.mode].ready_source(program) if idle else None
        if self._playing is None and (deck is not None or not idle):
            self._play(player)
        # A voice transition may invalidate a delivered event after its PCM read.
        # Re-arm the actual live source on the new context; only a new read can
        # accept it, never the invalidated event from the disconnected epoch.
        live = next((s for s in self.sources if s is self.mixer.live), None)
        if (
            not idle
            and live is not None
            and live is not self._heard_source
            and live.program is program
            and self._audible(player)
            and (
                live.mode is Mode.CATALOG
                or (self._previous is not None and self._previous.mode is Mode.CATALOG)
            )
            and self._playing is not None
        ):
            self._bind_mixer()
            self.mixer.arm(live, live, program, self._playing, drain=False, complete=False)
            return
        if deck is None or not self._audible(player):
            return
        if program.mode is Mode.CATALOG or (
            self._previous is not None and self._previous.mode is Mode.CATALOG
        ):
            if self._playing is not None:
                self._bind_mixer()
                self.mixer.arm(None, deck, program, self._playing, drain=False, complete=False)
            return
        self.mixer.switch_to(deck, FADE_IN_S)
        self._settle_playing(program, deck)
        log.info("Program started on deck %d", deck.number)

    def _hand_over(self, program: Program, player: Player | None) -> None:
        """Crossfade to the next deck when the live one belongs to a replaced plan or runs low.

        Only while it can be heard, so a replacement is never reported as playing during a
        voice outage.
        """
        if self.mixer.switching or not self._audible(player):
            return
        target = self.mixer.live
        live = next((s for s in self.sources if s is target), None)
        if live is None:
            return
        if live.program is program and not self.engines[live.mode].handover_due(live):
            return
        deck = self.engines[program.mode].ready_source(program, exclude=live)
        if deck is None:
            return
        seconds = CROSSFADE_S if live.mode is program.mode is Mode.GENERATED else 0
        if seconds == 0:
            if self._playing is not None:
                self._bind_mixer()
                natural = live.program is program
                self.mixer.arm(
                    live,
                    deck,
                    program,
                    self._playing,
                    drain=natural,
                    complete=natural and not live.failed,
                )
            return
        self.mixer.switch_to(deck, seconds)
        live.retire()
        self._settle_playing(program, deck)

    def _discard(self, player: Player | None) -> None:
        """Count each failure, applying any give-up before the next one, then drop ended decks.

        A give-up can restore the program still heard, so each failure and the discard policy
        use the program current at that point.
        """
        # Snapshot ends before any give-up: old-mode failures cannot be counted
        # retroactively against the program restored by this same round.
        ended = [
            (engine, engine.ended_failures(self._target(engine)))
            for engine in self.engines.values()
        ]
        # Snapshot both identity and outcome before FB-3 may restore another mode.
        terminal = [
            (
                engine,
                self._target(engine),
                engine.terminal(self._target(engine), player is not None and player.is_connected()),
            )
            for engine in self.engines.values()
        ]
        for engine, requested, outcome in terminal:
            if requested is not None and self._target(engine) is requested and outcome is not None:
                self._give_up(outcome)
        for engine, sources in ended:
            for source in sources:
                outcome = engine.count_failure(
                    source, self._target(engine), player is not None and player.is_connected()
                )
                if outcome is not None:
                    self._give_up(outcome)
        for engine in self.engines.values():
            engine.drop_ended(self._target(engine), self.mixer)

    def _target(self, engine: Engine) -> Program | None:
        return (
            self.program if self.program is not None and self.program.mode is engine.mode else None
        )

    def _audible(self, player: Player | None) -> bool:
        """Whether a running player sends the mixer's audio to a connected voice channel."""
        return self._playing is not None and player is not None and player.is_connected()

    def _play(self, player: Player) -> None:
        """Start discord.py's player and track it until its thread ends.

        A player thread that gives up on a slow voice reconnect ends without updating
        `is_playing()`, so the station relies on the `after` callback instead.
        """
        loop = asyncio.get_running_loop()
        playing = object()

        def stopped(error: Exception | None) -> None:  # runs on discord.py's player thread
            self.mixer.invalidate_handoff(playing)
            if error is not None:
                log.warning("Voice player stopped: %s", error)
            loop.call_soon_threadsafe(self._player_stopped, playing)

        if self._clock() < self._next_play:
            return
        self._next_play = self._clock() + PLAYER_RETRY_S
        if player.is_playing():
            player.stop()  # its thread has ended
        try:
            player.play(self.mixer, after=stopped, signal_type="music")
        except discord.ClientException as error:  # not connected yet
            log.debug("Could not start the voice player: %s", error)
            return
        except discord.DiscordException:  # such as an Opus encoder error, which retries cannot fix
            log.exception("Could not start the voice player; ending the program")
            self._finish(Outcome.FAILED)
            return
        self._playing = playing
        self._bind_mixer()

    def _player_stopped(self, playing: object) -> None:
        if self._playing is playing:
            self.mixer.invalidate_handoff(playing)
            self._playing = None
        self.wake()
