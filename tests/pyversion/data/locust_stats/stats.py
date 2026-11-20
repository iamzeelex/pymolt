"""Trimmed copy of the shapes in locust/stats.py (locust 2.x) that the Python 3.9 TypedDict
annotation-order change reaches: a guarded ``TypedDict`` import, two TypedDicts over one base,
dict comprehensions over ``X.__annotations__.keys()`` (serialize) and a keys view used only for
membership (unserialize). Annotations are respelled; field names and order are locust's.
Fixture data for tests/pyversion/test_behavior.py, never imported."""
from __future__ import annotations

from typing import cast

try:
    from typing import Protocol, TypedDict
except ImportError:
    from typing_extensions import Protocol, TypedDict  # type: ignore  # noqa: UP035


class StatsBaseDict(TypedDict):
    name: str
    method: str


class StatsEntryDict(StatsBaseDict):
    last_request_timestamp: float | None
    start_time: float
    num_requests: int
    num_none_requests: int
    num_failures: int
    total_response_time: int
    max_response_time: int
    min_response_time: int | None
    total_content_length: int
    response_times: dict[int, int]
    num_reqs_per_sec: dict[int, int]
    num_fail_per_sec: dict[int, int]


class StatsErrorDict(StatsBaseDict):
    error: str
    occurrences: int


class StatsHolder(Protocol):
    name: str
    method: str


class StatsEntry:
    def serialize(self) -> StatsEntryDict:
        return cast(StatsEntryDict, {key: getattr(self, key, None) for key in StatsEntryDict.__annotations__.keys()})  # noqa: E501

    @classmethod
    def unserialize(cls, data: StatsEntryDict) -> StatsEntry:
        obj = cls()
        valid_keys = StatsEntryDict.__annotations__.keys()

        for key, value in data.items():
            if key in ["name", "method"] or key not in valid_keys:
                continue

            setattr(obj, key, value)
        return obj


class StatsError:
    def serialize(self) -> StatsErrorDict:
        def _getattr(obj, key, default):
            return getattr(obj, key, default)

        return cast(StatsErrorDict, {key: _getattr(self, key, None) for key in StatsErrorDict.__annotations__.keys()})  # noqa: E501
