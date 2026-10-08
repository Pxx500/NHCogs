"""Temporary event-loop lag measurement. See NHCogs/README.md."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("red.NHCogs.loop_lag")

# asyncio debug mode also times callbacks, but it captures a stack on every
# scheduled call. This wrapper only reads the clock unless a callback is slow.
THRESHOLD_SECONDS = 0.1
WINDOW_SECONDS = 48 * 60 * 60
SUMMARY_LIMIT = 5
MAX_LABELS = 32
LABEL_LIMIT = 300
CHAIN_LIMIT = 8
MS_PER_SECOND = 1000

perf_counter = time.perf_counter
wall_time = time.time


class _Measurement:
    """Process-local window. One object so the flags can change without globals."""

    def __init__(self) -> None:
        self.handle_run = None
        self.timer_run = None
        self.active = False
        self.active_until = 0.0
        self.generation = 0
        self.stop_task: asyncio.Task | None = None
        self.sizes_logged = False
        self.records: dict[str, list[float]] = {}


_measurement = _Measurement()


def active() -> bool:
    """Return whether callbacks are being timed."""
    return _measurement.active


def format_deadline(deadline: float) -> str:
    """Return a UTC minute stamp for a measurement deadline."""
    moment = datetime.fromtimestamp(deadline, timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def summary_text() -> str:
    """Return the maintainer summary for this process."""
    if _measurement.active:
        lines = [f"Loop lag measurement is on until {format_deadline(_measurement.active_until)}"]
    else:
        lines = ["Loop lag measurement is off"]
    rows = sorted(
        _measurement.records.items(), key=lambda item: (-item[1][2], -item[1][0], item[0])
    )
    if not rows:
        if _measurement.active:
            lines.append("No slow callbacks yet")
        return "\n".join(lines)
    for label, row in rows[:SUMMARY_LIMIT]:
        lines.append(f"{label}: {row[0]:.0f}, max {row[2]:.0f} ms")
    return "\n".join(lines)


async def enable(bot, config) -> float:
    """Start a window, log joinwatch sizes once, and return the deadline."""
    deadline = wall_time() + WINDOW_SECONDS
    await config.loop_lag_until.set(deadline)
    _measurement.records.clear()
    _measurement.sizes_logged = False
    _begin(config, deadline, resumed=False)
    await _log_sizes_once(bot, report_missing=True)
    return deadline


async def disable(config) -> None:
    """Stop timing and clear the stored deadline."""
    _disarm()
    await _clear_deadline(config)
    log.info("Loop lag measurement stopped")


async def resume(bot, config) -> None:
    """Continue an open window after a restart or reload."""
    try:
        raw = await config.loop_lag_until()
    except Exception:
        log.exception("Loop lag measurement could not read its deadline")
        return
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return
    if raw <= wall_time():
        _disarm()
        await _clear_deadline(config)
        return
    _begin(config, float(raw), resumed=True)
    await _log_sizes_once(bot, report_missing=False)


async def note_suite_loaded(bot) -> None:
    """Log joinwatch sizes once Honeypot can be seen. See NHCogs/README.md."""
    if not _measurement.active:
        return
    try:
        await _log_sizes_once(bot, report_missing=True)
    except Exception:
        log.exception("Loop lag measurement could not log joinwatch sizes")


def pause() -> None:
    """Remove the timing wrapper without clearing a stored window."""
    _disarm()
    _measurement.sizes_logged = False


def _begin(config, deadline: float, *, resumed: bool) -> None:
    generation = _open(deadline)
    _schedule_stop(generation, deadline, config)
    when = format_deadline(deadline)
    if resumed:
        log.info("Loop lag measurement resumed until %s", when)
    else:
        log.info("Loop lag measurement started until %s", when)


def _open(deadline: float) -> int:
    _cancel_stop_task()
    _measurement.generation += 1
    _measurement.active_until = deadline
    _measurement.active = True
    _install()
    return _measurement.generation


def _disarm() -> None:
    _measurement.generation += 1
    _deactivate()
    _cancel_stop_task()


def _deactivate() -> None:
    _measurement.active = False
    _uninstall()


def _cancel_stop_task() -> None:
    task = _measurement.stop_task
    _measurement.stop_task = None
    if task is not None and not task.done():
        task.cancel()


def _schedule_stop(generation: int, deadline: float, config) -> None:
    _measurement.stop_task = asyncio.create_task(
        _wait_and_stop(generation, deadline, config),
        name="nhcogs-loop-lag",
    )


async def _wait_and_stop(generation: int, deadline: float, config) -> None:
    if generation != _measurement.generation:
        return
    try:
        delay = deadline - wall_time()
        if delay > 0:
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if generation != _measurement.generation:
        return
    _deactivate()
    try:
        current = await config.loop_lag_until()
        if generation != _measurement.generation or current != deadline:
            return
        await config.loop_lag_until.set(None)
    except Exception:
        log.exception("Loop lag measurement could not clear its deadline")
        return
    if generation != _measurement.generation:
        return
    log.info("Loop lag measurement stopped")


async def _clear_deadline(config) -> None:
    try:
        await config.loop_lag_until.set(None)
    except Exception:
        log.exception("Loop lag measurement could not clear its deadline")


def _install() -> None:
    events = asyncio.events
    # TimerHandle inherits Handle._run. Save both before either class changes,
    # then set the wrapper on TimerHandle itself.
    handle_run = events.Handle._run
    timer_run = events.TimerHandle._run
    if handle_run is not _timed_run:
        _measurement.handle_run = handle_run
    if timer_run is not _timed_run:
        _measurement.timer_run = timer_run
    events.Handle._run = _timed_run
    events.TimerHandle._run = _timed_run


def _uninstall() -> None:
    events = asyncio.events
    if _measurement.handle_run is not None and events.Handle._run is _timed_run:
        events.Handle._run = _measurement.handle_run
    if _measurement.timer_run is not None and events.TimerHandle._run is _timed_run:
        events.TimerHandle._run = _measurement.timer_run


def _timed_run(self):
    original = (
        _measurement.timer_run
        if isinstance(self, asyncio.events.TimerHandle)
        else _measurement.handle_run
    )
    if not _measurement.active or original is None:
        return None if original is None else original(self)
    started = perf_counter()
    try:
        return original(self)
    finally:
        elapsed = perf_counter() - started
        if elapsed > THRESHOLD_SECONDS:
            _record(self, elapsed)


def _record(handle, elapsed: float) -> None:
    try:
        label = _clip(_label(handle))
        elapsed_ms = elapsed * MS_PER_SECOND
        _remember(label, elapsed_ms)
        log.warning("Loop lag: %s took %.0f ms", label, elapsed_ms)
    except Exception:
        log.exception("Loop lag recorder failed")


def _remember(label: str, elapsed_ms: float) -> None:
    records = _measurement.records
    row = records.get(label)
    if row is None:
        if len(records) >= MAX_LABELS:
            smallest = min(records, key=lambda key: records[key][2])
            if records[smallest][2] >= elapsed_ms:
                return
            del records[smallest]
        records[label] = [1, elapsed_ms, elapsed_ms]
        return
    row[0] += 1
    row[1] += elapsed_ms
    row[2] = max(row[2], elapsed_ms)


def _label(handle) -> str:
    callback = getattr(handle, "_callback", None)
    if callback is None:
        return "unknown callback"
    owner = getattr(callback, "__self__", None)
    if _is_task(owner):
        return _describe_task(owner)
    return _describe_callback(callback, owner)


def _is_task(owner) -> bool:
    return callable(getattr(owner, "get_coro", None)) and callable(getattr(owner, "get_name", None))


def _describe_callback(callback, owner) -> str:
    qualname = (
        getattr(callback, "__qualname__", None)
        or getattr(callback, "__name__", None)
        or type(callback).__name__
    )
    cog = getattr(owner, "qualified_name", None) if owner is not None else None
    if isinstance(cog, str) and cog:
        return f"{qualname} cog={cog}"
    module = getattr(callback, "__module__", None)
    if isinstance(module, str) and module and module != "builtins":
        return f"{module}.{qualname}"
    return str(qualname)


def _describe_task(task) -> str:
    frames = _chain(task)
    interesting = [entry for entry in frames if not _is_scheduler(entry[0])]
    chosen = interesting or frames
    if not chosen:
        function = "task"
    elif len(chosen) == 1:
        function = chosen[0][2]
    else:
        function = f"{chosen[0][2]} -> {chosen[-1][2]}"
    parts = [function]
    cog = _cog_name(chosen) or _cog_name(frames)
    if cog:
        parts.append(f"cog={cog}")
    task_name = _task_name(task)
    if task_name:
        parts.append(f"task={task_name}")
    return " ".join(parts)


def _chain(task) -> list[tuple]:
    try:
        current = task.get_coro()
    except Exception:
        return []
    frames: list[tuple] = []
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(frames) < CHAIN_LIMIT:
        seen.add(id(current))
        code = getattr(current, "cr_code", None)
        if code is not None:
            frames.append((
                code,
                getattr(current, "cr_frame", None),
                getattr(current, "__qualname__", code.co_name),
            ))
        nxt = getattr(current, "cr_await", None)
        current = nxt if getattr(nxt, "cr_code", None) is not None else None
    return frames


def _is_scheduler(code) -> bool:
    if code.co_name == "_run_event":
        return True
    filename = str(getattr(code, "co_filename", "")).replace("\\", "/")
    return "/asyncio/" in filename


def _cog_name(frames) -> str | None:
    for _code, frame, _qualname in frames:
        if frame is None:
            continue
        self_obj = frame.f_locals.get("self")
        name = getattr(self_obj, "qualified_name", None)
        if isinstance(name, str) and name:
            return name
    return None


def _task_name(task) -> str:
    try:
        name = task.get_name()
    except Exception:
        return ""
    if not isinstance(name, str) or not name or name.startswith("Task-"):
        return ""
    return name


def _clip(label: str) -> str:
    if len(label) <= LABEL_LIMIT:
        return label
    return label[: LABEL_LIMIT - 3] + "..."


def _honeypot(bot):
    get_cog = getattr(bot, "get_cog", None)
    if not callable(get_cog):
        return None
    try:
        return get_cog("Honeypot")
    except Exception:
        log.exception("Loop lag measurement could not find Honeypot")
        return None


async def _log_sizes_once(bot, *, report_missing: bool) -> None:
    if not _measurement.active or _measurement.sizes_logged:
        return
    if _honeypot(bot) is None:
        if report_missing:
            log.info("Loop lag: Honeypot is not loaded, so joinwatch sizes were not read")
            _measurement.sizes_logged = True
        return
    await _log_sizes(bot)
    _measurement.sizes_logged = True


async def _log_sizes(bot) -> None:
    honeypot = _honeypot(bot)
    guilds = _guilds(bot)
    if not guilds:
        log.info("Loop lag: no guilds were available for joinwatch sizes")
        return
    guild_ids = [guild.id for guild in guilds]
    counts = await _history_counts(getattr(honeypot, "_case_store", None), guild_ids)
    for guild in guilds:
        observations = counts.get(guild.id)
        rendered = "unavailable" if observations is None else str(observations)
        sizes = await _live_sizes(honeypot, guild)
        log.info(
            "Loop lag sizes: guild %s verified_members=%s pending_roles=%s "
            "pending_role_assignments=%s join_history_observations=%s",
            guild.id,
            sizes["verified"],
            sizes["pending_role"],
            sizes["pending_assignment"],
            rendered,
        )


def _guilds(bot) -> list:
    found = []
    seen: set[int] = set()
    for guild in getattr(bot, "guilds", ()) or ():
        guild_id = getattr(guild, "id", None)
        if isinstance(guild_id, int) and guild_id not in seen:
            seen.add(guild_id)
            found.append(guild)
    found.sort(key=lambda guild: guild.id)
    return found


def _source_lock(cog):
    lock = getattr(cog, "_joinwatch_live_source_lock", None)
    if lock is not None:
        return lock
    lock = asyncio.Lock()
    try:
        cog._joinwatch_live_source_lock = lock
    except Exception:
        return None
    return lock


async def _live_sizes(honeypot, guild) -> dict[str, int]:
    """Count SQLite live rows after cutover. Config maps are not read."""
    empty = {"verified": 0, "pending_role": 0, "pending_assignment": 0}
    store = getattr(honeypot, "_case_store", None)
    guild_id = getattr(guild, "id", None)
    if store is None or not isinstance(guild_id, int) or not callable(getattr(store, "counts", None)):
        return empty
    lock = _source_lock(honeypot)

    async def read() -> dict[str, int]:
        try:
            if callable(getattr(store, "cutover_source", None)):
                source = await asyncio.to_thread(store.cutover_source, guild_id)
                if source != "sqlite":
                    return empty
            totals = await asyncio.to_thread(store.counts, guild_id)
        except Exception:
            log.warning("Loop lag: could not count live JoinWatch rows for guild %s", guild_id)
            return empty
        if not isinstance(totals, dict):
            return empty
        return {kind: _as_count(totals.get(kind)) for kind in empty}

    if lock is None:
        return await read()
    async with lock:
        return await read()


def _as_count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def _history_counts(store, guild_ids: list[int]) -> dict[int, int | None]:
    if store is None or not callable(getattr(store, "get_joinwatch_history", None)):
        log.info("Loop lag: Honeypot has no join-history store")
        return dict.fromkeys(guild_ids)

    def read() -> dict[int, int | None]:
        found: dict[int, int | None] = {}
        for guild_id in guild_ids:
            try:
                history = store.get_joinwatch_history(guild_id)
            except Exception:
                log.warning("Loop lag: could not count join history for guild %s", guild_id)
                found[guild_id] = None
                continue
            observations = history.get("observations") if isinstance(history, dict) else None
            found[guild_id] = len(observations) if isinstance(observations, dict) else 0
        return found

    return await asyncio.to_thread(read)
