from __future__ import annotations

import socket
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main

from .conftest import make_packet


def test_run_with_invalid_config_exits_with_error(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[telemetry]\nlisten_port = 99999\n", encoding="utf-8")
    assert main(["run", "--config", str(config)]) == 2


def test_run_with_port_in_use_exits_with_error(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        assert main(["run", "--port", str(port), "--no-capture"]) == 2
    assert "No se pudo abrir UDP" in capsys.readouterr().err


def test_inspect_prints_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.f1cap"
    with CaptureWriter(path) as writer:
        for i in range(4):
            writer.write(CapturedPacket(i * 1_000_000, "10.0.0.5", 4000, make_packet(2)))
    assert main(["inspect", str(path)]) == 0
    out = capsys.readouterr().out
    assert "4 paquetes" in out
    assert "LapData" in out
    assert "2025" in out


def test_inspect_missing_file_exits_with_error(tmp_path: Path) -> None:
    assert main(["inspect", str(tmp_path / "no-existe.f1cap")]) == 2


def test_events_summary_reads_rows_and_reports_suppression(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    detection = SimpleNamespace(
        t_sim_s=12.0,
        lap=4,
        rule="fuel_margin",
        event_id="fuel:1",
        phase="opened",
        priority="critical",
        category="fuel",
        message_key="event.fuel_margin_critical",
        facts_json='{"margin_kg":1.0}',
        material_key="fuel:4",
    )
    decision = SimpleNamespace(
        t_sim_s=12.0,
        event_id="fuel:1",
        phase="opened",
        outcome="suppress",
        reason="quiet_period",
        alert_id=None,
        interrupt=False,
    )

    class Store:
        def __enter__(self) -> Store:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get_session(self, session_id: int) -> object:
            return object()

        def iter_detected_events(self, session_id: int) -> tuple[object, ...]:
            return (detection,)

        def iter_gate_decisions(self, session_id: int) -> tuple[object, ...]:
            return (decision,)

    monkeypatch.setattr(cli_module.SessionStore, "open", lambda *_args, **_kwargs: Store())
    assert main(["events", "summary", "--session", "7", "--db", "unused.sqlite"]) == 0
    output = capsys.readouterr().out
    assert "Decisiones suprimidas: 1" in output
    assert "quiet_period" in output
    assert "fuel_margin" in output


def test_events_commands_report_missing_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Store:
        def __enter__(self) -> Store:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get_session(self, session_id: int) -> None:
            return None

    monkeypatch.setattr(cli_module.SessionStore, "open", lambda *_args, **_kwargs: Store())
    assert main(["events", "show", "--session", "404", "--db", "unused.sqlite"]) == 2
    assert "No existe la sesión 404" in capsys.readouterr().err


def _events_database(path: Path) -> int:
    with SessionStore.open(path) as store:
        session_id = store.create_session(
            SessionInfo(
                session_uid=0xE10,
                source="live",
                started_at_utc="2026-11-05T10:00:00Z",
                track="monza",
                session_type="race",
                total_laps=10,
            )
        )
        store.add_detected_events(
            session_id,
            (
                StoredDetectedEvent(
                    seq=1,
                    t_sim_s=12.0,
                    lap=4,
                    rule="fuel_margin",
                    event_id="fuel:1",
                    phase="opened",
                    priority="critical",
                    category="fuel",
                    message_key="event.fuel_margin_critical",
                    facts_json='{"margin_kg": 1.0, "margin_laps": null}',
                    material_key="fuel:4",
                ),
                StoredDetectedEvent(
                    seq=2,
                    t_sim_s=15.0,
                    lap=4,
                    rule="fuel_margin",
                    event_id="fuel:1",
                    phase="updated",
                    priority="critical",
                    category="fuel",
                    message_key="event.fuel_margin_critical",
                    facts_json='{"margin_kg": 0.5, "margin_laps": null}',
                    material_key="fuel:4",
                ),
                StoredDetectedEvent(
                    seq=3,
                    t_sim_s=17.0,
                    lap=4,
                    rule="fuel_margin",
                    event_id="fuel:1",
                    phase="updated",
                    priority="critical",
                    category="fuel",
                    message_key="event.fuel_margin_critical",
                    facts_json='{"margin_kg": 0.4, "margin_laps": null}',
                    material_key="fuel:4",
                ),
                StoredDetectedEvent(
                    seq=4,
                    t_sim_s=20.0,
                    lap=5,
                    rule="pit_window",
                    event_id="pit:1",
                    phase="opened",
                    priority="strategic",
                    category="strategy",
                    message_key="event.pit_window_open",
                    facts_json='{"ideal_lap": 6, "latest_lap": 7, "laps_to_ideal": 1}',
                    material_key="pit:5",
                ),
            ),
        )
        store.add_gate_decisions(
            session_id,
            (
                StoredGateDecision(
                    seq=1,
                    t_sim_s=12.0,
                    event_id="fuel:1",
                    phase="opened",
                    outcome="suppress",
                    reason="quiet_period",
                    alert_id=None,
                    interrupt=False,
                ),
                StoredGateDecision(
                    seq=2,
                    t_sim_s=15.0,
                    event_id="fuel:1",
                    phase="updated",
                    outcome="suppress",
                    reason="quiet_period",
                    alert_id=None,
                    interrupt=False,
                ),
                StoredGateDecision(
                    seq=3,
                    t_sim_s=17.0,
                    event_id="fuel:1",
                    phase="updated",
                    outcome="suppress",
                    reason="quiet_period",
                    alert_id=None,
                    interrupt=False,
                ),
                StoredGateDecision(
                    seq=4,
                    t_sim_s=18.0,
                    event_id="fuel:1",
                    phase="opened",
                    outcome="emit",
                    reason="emitted",
                    alert_id="alert:2",
                    interrupt=True,
                ),
                StoredGateDecision(
                    seq=5,
                    t_sim_s=20.0,
                    event_id="pit:1",
                    phase="opened",
                    outcome="emit",
                    reason="eligible",
                    alert_id="alert:1",
                    interrupt=False,
                ),
            ),
        )
    return session_id


def test_events_show_reads_synthetic_database_without_modifying_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "220")
    path = tmp_path / "events.db"
    session_id = _events_database(path)
    before = path.read_bytes()

    assert main(["events", "show", "--session", str(session_id), "--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "fuel_margin" in output
    assert "pit_window" in output
    assert "Margen de combustible crítico" in output
    assert "Ventana de parada abierta" in output
    assert "suppress/quiet_period" in output
    assert "emit/eligible" in output
    assert "emit/emitted" in output
    assert output.count("suppress/quiet_period") == 3

    assert (
        main(
            [
                "events",
                "show",
                "--session",
                str(session_id),
                "--db",
                str(path),
                "--rule",
                "pit_window",
            ]
        )
        == 0
    )
    filtered_by_rule = capsys.readouterr().out
    assert "pit_window" in filtered_by_rule
    assert "fuel_margin" not in filtered_by_rule

    assert (
        main(
            [
                "events",
                "show",
                "--session",
                str(session_id),
                "--db",
                str(path),
                "--outcome",
                "suppress",
            ]
        )
        == 0
    )
    filtered_by_outcome = capsys.readouterr().out
    assert "quiet_period" in filtered_by_outcome
    assert "eligible" not in filtered_by_outcome
    assert path.read_bytes() == before


def test_events_summary_reports_suppression_repetitions_and_laps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "events.db"
    session_id = _events_database(path)

    assert main(["events", "summary", "--session", str(session_id), "--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "4 detecciones, 5 decisiones" in output
    assert "Avisos emitidos: 2" in output
    assert "Decisiones suprimidas: 3; repeticiones de material: 2" in output
    assert "Detecciones por regla" in output
    assert "Decisiones por resultado" in output
    assert "Decisiones por motivo" in output
    assert "Avisos por vuelta" in output


def test_events_commands_report_an_empty_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "empty.db"
    with SessionStore.open(path) as store:
        session_id = store.create_session(
            SessionInfo(session_uid=0xE11, source="live", started_at_utc="2026-11-05T10:00:00Z")
        )

    assert main(["events", "show", "--session", str(session_id), "--db", str(path)]) == 0
    assert "no tiene eventos que coincidan" in capsys.readouterr().out

    assert main(["events", "summary", "--session", str(session_id), "--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "0 detecciones, 0 decisiones" in output
    assert "Decisiones suprimidas: 0" in output


class _SimulationRule:
    """Regla siempre activa para comprobar el estado del motor en `simulate`."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        del config
        self.policy = ConditionPolicy(
            rule="simulation",
            category="test",
            priority=EventPriority.CRITICAL,
            message_key="event.data_stale",
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        return (
            Observation(
                key=f"simulation_{inputs.lap}",
                active=True,
                value=1.0,
                facts=(("group", "prueba"), ("age_s", 1)),
            ),
        )


def _simulation_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    packets = list(
        build_race_timed(
            (ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),),
            total_laps=3,
            samples_per_lap=2,
        )
    )
    parsed = tuple(packet for packet, _ in packets)
    path = tmp_path / "simulation.f1cap"
    with CaptureWriter(path, include_created_utc=False) as writer:
        for index in range(len(packets)):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index * 2_000_000,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    return path


def test_events_simulate_keeps_engine_state_across_packets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _simulation_capture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "f1_strategist.events.engine.EVENT_RULES",
        (_SimulationRule(),),
    )
    monkeypatch.setenv("COLUMNS", "220")

    assert main(["events", "simulate", str(path)]) == 0
    output = capsys.readouterr().out
    assert output.count("Datos de prueba obsoletos") == 3
    assert "emit" in output

    assert main(["events", "simulate", str(path), "--from-lap", "2", "--to-lap", "2"]) == 0
    lap_two_output = capsys.readouterr().out
    assert lap_two_output.count("Datos de prueba obsoletos") == 1
    assert "No se detectaron eventos en el rango solicitado." not in lap_two_output
    event_rows = [
        line for line in lap_two_output.splitlines() if "Datos de prueba obsoletos" in line
    ]
    assert len(event_rows) == 1
    assert event_rows[0].split("│")[2].strip() == "2"
