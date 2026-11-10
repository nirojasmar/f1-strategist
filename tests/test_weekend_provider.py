"""Proveedor del perfil del fin de semana: del almacén al perfil del motor."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.stint_summary import StintSummary
from f1_strategist.metrics.types import Quality
from f1_strategist.store import (
    MetricSample,
    SessionInfo,
    SessionStore,
    TyreInventorySnapshot,
    WeekendInfo,
)
from f1_strategist.weekend_profile import WeekendProfile
from f1_strategist.weekend_provider import (
    WeekendProfileResult,
    build_profile_for_weekend,
    locate_weekend,
    profiles_for_capture,
    registered_profile,
    resolve_weekend_profile,
)


def _session(session_uid: int = 0xF125, **overrides: object) -> SessionInfo:
    values: dict[str, object] = {
        "source": "live",
        "started_at_utc": "2026-11-05T10:00:00Z",
        "game_format": "F1 25",
        "game_year": 2025,
        "game_version": "1.26",
        "track": "monza",
        "session_type": "race",
        "total_laps": 13,
        "tool_version": "0.1.0",
        "parser_version": "1",
        "config_hash": "b" * 64,
        "config_json": "{}",
    }
    values.update(overrides)
    return SessionInfo(session_uid=session_uid, **values)  # type: ignore[arg-type]


def _summary(
    *,
    compound: str = "c3",
    stint_index: int = 0,
    start_lap: int = 1,
    laps: int = 10,
    clean_laps: int = 6,
    pace_mean_s: float = 90.0,
    wear_rate_pct_per_lap: float = 0.10,
) -> StintSummary:
    return StintSummary(
        car_index=0,
        stint_index=stint_index,
        start_lap=start_lap,
        end_lap=start_lap + laps - 1,
        laps=laps,
        compound=compound,
        visual_compound=None,
        clean_laps=clean_laps,
        tyre_age_start=0,
        tyre_age_end=laps,
        fuel_start_kg=40.0,
        fuel_end_kg=38.0,
        pace_mean_s=pace_mean_s,
        pace_mean_fuel_corrected_s=None,
        pace_trend_s_per_lap=None,
        pace_trend_fuel_corrected_s_per_lap=None,
        wear_start_pct=0.0,
        wear_end_pct=1.0,
        wear_rate_pct_per_lap=wear_rate_pct_per_lap,
        track_temperature_mean_c=30.0,
        weather=("clear",),
        representative=True,
        quality=Quality.OK,
    )


def _inventory_set(*, kind: str, set_index: int, compound: str = "c3") -> TyreInventorySnapshot:
    return TyreInventorySnapshot(
        kind=kind,  # type: ignore[arg-type]
        set_index=set_index,
        actual_tyre_compound=compound,
        visual_tyre_compound="soft",
        wear_pct=0.0,
        available=True,
        lifespan_laps=18,
        usable_life_laps=18,
        lap_delta_time_ms=0,
        recommended_session="qualifying_1",
        fitted=False,
    )


class _BrokenStore:
    """Almacén que falla al leer, para comprobar que la captura no se detiene."""

    def get_weekend(self, weekend_id: int) -> WeekendInfo | None:
        raise RuntimeError("base de datos rota")

    def list_weekends(self) -> list[WeekendInfo]:
        raise RuntimeError("base de datos rota")

    def list_sessions(self) -> list[SessionInfo]:
        raise RuntimeError("base de datos rota")

    def find_weekend(
        self, *, weekend_link_id: int, season_link_id: int | None = None
    ) -> WeekendInfo | None:
        raise RuntimeError("base de datos rota")

    def list_weekend_sessions(self, weekend_id: int) -> list[SessionInfo]:
        raise RuntimeError("base de datos rota")

    def iter_stint_summaries(self, session_id: int) -> Iterator[StintSummary]:
        raise RuntimeError("base de datos rota")

    def iter_tyre_inventory(self, session_id: int) -> list[TyreInventorySnapshot]:
        raise RuntimeError("base de datos rota")

    def iter_metric_samples(
        self, session_id: int, *, key: str | None = None, lap: int | None = None
    ) -> Iterator[MetricSample]:
        raise RuntimeError("base de datos rota")


def test_without_a_linked_weekend_there_is_no_profile(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        session = store.get_session(session_id)
        assert session is not None

        result = resolve_weekend_profile(store, session, MetricsConfig())

    assert result == WeekendProfileResult()
    assert not result.available
    assert result.error is None


def test_profile_is_built_from_the_sessions_of_the_weekend(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice_1"))
        qualifying = store.create_session(_session(session_uid=2, session_type="qualifying_1"))
        weekend_id = store.link_sessions([practice, qualifying])
        store.add_stint_summaries(practice, [_summary(compound="c3")])
        store.add_stint_summaries(qualifying, [_summary(compound="c4", pace_mean_s=89.0)])
        store.add_tyre_inventory(practice, [_inventory_set(kind="race_start", set_index=0)])

        result = resolve_weekend_profile(
            store,
            _session(weekend_id=weekend_id),
            MetricsConfig(),
        )

    assert result.error is None
    assert result.weekend_id == weekend_id
    assert result.available
    profile = result.profile
    assert profile is not None
    assert profile.sessions == (practice, qualifying)
    assert profile.compound("c3") is not None
    assert profile.compound("c4") is not None
    assert profile.quality is not Quality.UNAVAILABLE
    assert [item.set_index for item in profile.inventory] == [0]


def test_weekend_is_located_from_the_game_identifiers(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        store.create_session(_session(session_uid=1, weekend_link_id=42, season_link_id=7))
        weekend = store.find_weekend(weekend_link_id=42, season_link_id=7)
        assert weekend is not None

        located = locate_weekend(
            store, _session(session_uid=2, weekend_link_id=42, season_link_id=7)
        )

        assert located == weekend


def test_weekend_is_not_guessed_from_track_or_mismatched_ids(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice_1"))
        weekend_id = store.link_sessions([practice])
        weekend = store.get_weekend(weekend_id)

        assert locate_weekend(store, _session(session_uid=2)) is None
        assert locate_weekend(store, _session(session_uid=2, weekend_id=weekend_id)) == weekend
        assert locate_weekend(store, _session(session_uid=2, weekend_link_id=999)) is None

        store.create_session(_session(session_uid=3, track="spa"))
        spa_practice = store.list_sessions()[1].id
        assert spa_practice is not None
        store.link_sessions([spa_practice])

        other = store.create_session(_session(session_uid=4, track="monza"))
        store.link_sessions([other])

        assert locate_weekend(store, _session(session_uid=5, track="monza")) is None


def test_locating_prefers_the_session_weekend_over_the_track(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = store.create_session(_session(session_uid=1))
        first_weekend = store.link_sessions([first])
        second = store.create_session(_session(session_uid=2))
        second_weekend = store.link_sessions([second])
        assert first_weekend != second_weekend

        located = locate_weekend(store, _session(session_uid=3, weekend_id=second_weekend))

        assert located is not None
        assert located.id == second_weekend


def test_a_reading_failure_is_reported_without_raising() -> None:
    result = resolve_weekend_profile(
        _BrokenStore(),  # type: ignore[arg-type]
        _session(weekend_link_id=42, season_link_id=7),
        MetricsConfig(),
    )

    assert result.profile is None
    assert result.error == "base de datos rota"
    assert not result.available


def test_excluded_session_does_not_feed_the_profile(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice_1"))
        race = store.create_session(_session(session_uid=2))
        store.link_sessions([practice, race])
        store.add_stint_summaries(practice, [_summary(compound="c3")])
        store.add_stint_summaries(race, [_summary(compound="c5", pace_mean_s=88.0)])
        race_info = store.get_session(race)
        assert race_info is not None and race_info.weekend_id is not None

        profile = build_profile_for_weekend(
            store,
            race_info.weekend_id,
            MetricsConfig(),
            exclude_session_id=race,
        )

    assert profile.sessions == (practice,)
    assert profile.compound("c3") is not None
    assert profile.compound("c5") is None


def test_pit_loss_uses_latest_sample_once_and_preserves_stop_count(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice_1"))
        race = store.create_session(_session(session_uid=2))
        weekend_id = store.link_sessions([practice, race])
        store.add_metric_samples(
            practice,
            [
                MetricSample(10.0, "pit.loss_estimate_s", "low", "s", 1, 10.0, 1, 20.0),
                MetricSample(20.0, "pit.loss_estimate_s", "low", "s", 2, 20.0, 2, 21.0),
                MetricSample(30.0, "pit.loss_estimate_s", "low", "s", 2, 30.0, 2, 21.0),
            ],
        )

        result = resolve_weekend_profile(
            store,
            _session(session_uid=3, weekend_id=weekend_id),
            MetricsConfig(),
        )

    assert result.profile is not None
    assert result.profile.pit_loss_s.value == 21.0
    assert result.profile.pit_loss_s.samples == 2
    assert result.profile.pit_loss_s.quality is Quality.LOW
    assert result.profile.pit_loss_s.sessions == (practice,)


def test_profiles_are_loaded_by_capture_and_session_uid(tmp_path: Path) -> None:
    profile = WeekendProfile()
    with SessionStore.open(tmp_path / "sessions.db") as store:
        store.create_session(
            _session(
                session_uid=101,
                capture_sha256="a" * 64,
                profile_json=profile.to_json(),
                profile_hash=profile.hash,
            )
        )
        store.create_session(
            _session(
                session_uid=202,
                capture_sha256="a" * 64,
                profile_json=profile.to_json(),
                profile_hash=profile.hash,
            )
        )

        profiles = profiles_for_capture(store, "a" * 64)

    assert profiles == {101: profile, 202: profile}


def test_registered_profile_reads_the_stored_json_and_hash() -> None:
    assert registered_profile(_session()) is None

    session = replace(_session(), profile_json='{"schema_version":1}', profile_hash="abc")

    assert registered_profile(session) == ('{"schema_version":1}', "abc")
