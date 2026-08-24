"""CSV logging of measuring values during a test drive.

Every row is flushed to disk immediately, so a log survives the program
crashing or the cable being yanked out mid-measurement. The header row is
written when the first reading arrives, because that is when the units become
known.

The default format targets Swedish/European Excel: semicolon as the delimiter
and a decimal comma. For pandas, gnuplot and friends, pass ``delimiter=","``
and ``decimal_comma=False``.
"""

from __future__ import annotations

import csv
import datetime as _dt
import time
from pathlib import Path
from typing import IO, Any, Mapping, Sequence

from .formulas import Reading
from .modules import label

__all__ = ["CsvLogger"]


class CsvLogger:
    """Writes measuring values to a CSV file, one row per polling cycle."""

    def __init__(
        self,
        filename: str | Path,
        groups: Sequence[int],
        address: int = 0x01,
        delimiter: str = ";",
        decimal_comma: bool = True,
        values_per_group: int = 4,
    ) -> None:
        self.filename = Path(filename)
        self.groups = list(groups)
        self.address = address
        self.delimiter = delimiter
        self.decimal_comma = decimal_comma
        self.values_per_group = values_per_group
        self.rows = 0
        self._start = time.monotonic()
        self._header_written = False
        self._file: IO[str] | None = None
        self._writer: Any | None = None  # csv.writer has no public type

        self.filename.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig makes Excel render non-ASCII characters correctly.
        self._file = open(self.filename, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file, delimiter=self.delimiter)

    # -- header ------------------------------------------------------------

    def _column_names(self, readings: Mapping[int, Sequence[Reading]]) -> list[str]:
        """Build the header row: ``003.2 Air mass SPEC [mg/stroke]``."""
        names = ["timestamp", "seconds"]
        for group in self.groups:
            values = readings.get(group, ())
            for position in range(1, self.values_per_group + 1):
                text = label(group, position, self.address)
                unit = ""
                if position - 1 < len(values):
                    unit = values[position - 1].unit
                unit_text = f" [{unit}]" if unit else ""
                names.append(f"{group:03d}.{position} {text}{unit_text}")
        return names

    # -- writing -----------------------------------------------------------

    def _format(self, reading: Reading | None) -> str:
        """One cell: a number when possible, otherwise the text, otherwise empty."""
        if reading is None:
            return ""
        if reading.is_number:
            text = f"{reading.number:.3f}"
            return text.replace(".", ",") if self.decimal_comma else text
        if isinstance(reading.value, str):
            return reading.value
        return reading.text

    def log(self, readings: Mapping[int, Sequence[Reading]]) -> None:
        """Write one row with every value from a polling cycle."""
        if self._writer is None or self._file is None:
            raise ValueError("The log is closed.")
        if not self._header_written:
            self._writer.writerow(self._column_names(readings))
            self._header_written = True

        now = _dt.datetime.now()
        row: list[str] = [
            now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}",
            self._format_seconds(time.monotonic() - self._start),
        ]
        for group in self.groups:
            values = list(readings.get(group, ()))
            for position in range(self.values_per_group):
                row.append(
                    self._format(values[position] if position < len(values) else None)
                )
        self._writer.writerow(row)
        self._file.flush()  # crash safe: the row is on disk immediately
        self.rows += 1

    def _format_seconds(self, s: float) -> str:
        text = f"{s:.3f}"
        return text.replace(".", ",") if self.decimal_comma else text

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the file."""
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None

    def __enter__(self) -> CsvLogger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.filename} ({self.rows} rows)"
