"""A fake monotonic clock for T3 tests: ``sleep`` advances it, nothing waits."""

from __future__ import annotations


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.slept = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.t += seconds
            self.slept += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingPort:
    """Wraps a fake serial port and records the clock time of every byte written."""

    def __init__(self, inner, clock) -> None:
        self.inner = inner
        self.clock = clock
        self.byte_times: list[tuple[float, int]] = []

    def write(self, data: bytes) -> int:
        for b in data:
            self.byte_times.append((self.clock(), b))
        return self.inner.write(data)

    @property
    def in_waiting(self) -> int:
        return self.inner.in_waiting

    def read(self, size: int = 1) -> bytes:
        return self.inner.read(size)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        return self.inner.read_until(expected, size)

    def reset_input_buffer(self) -> None:
        self.inner.reset_input_buffer()

    def close(self) -> None:
        self.inner.close()

    def gaps(self) -> list[float]:
        times = [t for t, _ in self.byte_times]
        return [b - a for a, b in zip(times, times[1:], strict=False)]
