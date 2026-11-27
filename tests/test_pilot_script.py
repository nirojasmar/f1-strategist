from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import f1_strategist.pilot.script as script_module
from f1_strategist.capture import CapturedPacket, CaptureReader, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.config import AppConfig, LLMConfig, PilotConfig
from f1_strategist.fingerprint import PilotFingerprintAccumulator
from f1_strategist.metrics.types import Quality
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from f1_strategist.pilot.script import (
    ScriptError,
    ask_capture_at_lap,
    load_script,
    run_pilot_script,
)
from f1_strategist.pilot.types import (
    AnswerKind,
    FactKind,
    Intent,
    PilotAnswer,
    ToolFact,
    ToolInputs,
    ToolResult,
)
from f1_strategist.playback import PlaybackSession
from f1_strategist.store import SessionStore, StoredPilotQuery

from .conftest import make_packet
from .scenarios import ScenarioCar, build_race_timed


class _FakeResponder:
    """Doble determinista que simula el motor en modo plantilla para Ola 1."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, int | None, str]] = []
        self._seq = 0

    def reset(self) -> None:
        self.calls.clear()
        self._seq = 0

    def ask(self, text: str, *, inputs: ToolInputs, source: str = "script") -> PilotAnswer:
        self._seq += 1
        lap = inputs.state.current_lap
        self.calls.append((text, inputs.now, lap, source))
        fuel = inputs.metrics.get("fuel.margin_laps_at_finish")
        raw_fuel = None if fuel is None else fuel.value
        fuel_val = float(raw_fuel) if isinstance(raw_fuel, (int, float)) else None
        facts = (
            (
                ToolFact(
                    name="fuel_margin_laps",
                    value=fuel_val,
                    unit="lap",
                    kind=FactKind.ESTIMATED,
                    quality=Quality.OK,
                ),
            )
            if fuel_val is not None
            else ()
        )
        intent = Intent.FUEL_STATUS if "combustible" in text.lower() else Intent.GENERAL_RACE_STATE
        return PilotAnswer(
            query_id=f"{source}-{self._seq}",
            intent=intent,
            kind=AnswerKind.ANSWER,
            text=f"Vuelta {lap} t={inputs.now:.1f}s fuel={fuel_val}",
            message_key="pilot.test_answer",
            params={"lap": lap, "fuel": fuel_val},
            tool_result=ToolResult(
                intent=intent,
                quality=Quality.OK,
                facts=facts,
                data_lap=lap,
            ),
            # Latencia deliberadamente variable para probar que la huella la ignora.
            latency_s=0.001 * self._seq,
            followup_pending=True,
        )


class _TrackingResponder(_FakeResponder):
    """Registra invalidaciones de aclaración y reinicios sin perder la secuencia."""

    def __init__(self) -> None:
        super().__init__()
        self.cancellations = 0
        self.resets = 0

    def reset(self) -> None:
        super().reset()
        self.resets += 1

    def cancel_pending_clarification(self) -> None:
        self.cancellations += 1


def _expected_fingerprint(rows: Sequence[StoredPilotQuery]) -> str:
    accumulator = PilotFingerprintAccumulator()
    accumulator.add(json.loads(row.exchange_json) for row in rows if row.exchange_json is not None)
    return accumulator.fingerprint


def _synthetic_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    total_laps: int = 6,
    session_time_offset_s: float = 0.0,
    session_uid_offset_from_s: float | None = None,
    min_observed_s: float | None = None,
    max_observed_s: float | None = None,
) -> Path:
    cars = (
        ScenarioCar(
            index=0,
            name="Alex Albon",
            grid_position=1,
            base_lap_time_s=90.0,
            initial_fuel_kg=30.0,
            fuel_per_lap_kg=2.0,
            tyre_wear_per_lap_pct=3.0,
        ),
        ScenarioCar(
            index=1,
            name="Lewis Hamilton",
            grid_position=2,
            base_lap_time_s=90.4,
            initial_fuel_kg=30.0,
            fuel_per_lap_kg=2.0,
            tyre_wear_per_lap_pct=3.0,
        ),
    )
    packets = [
        (packet, observed_at)
        for packet, observed_at in build_race_timed(cars, total_laps=total_laps, samples_per_lap=2)
        if (min_observed_s is None or observed_at >= min_observed_s)
        and (max_observed_s is None or observed_at <= max_observed_s)
    ]
    parsed = tuple(
        replace(
            packet,
            header=replace(
                packet.header,
                session_time=packet.header.session_time + session_time_offset_s,
                session_uid=packet.header.session_uid
                + (
                    1
                    if session_uid_offset_from_s is not None
                    and observed_at >= session_uid_offset_from_s
                    else 0
                ),
            ),
        )
        for packet, observed_at in packets
    )
    path = tmp_path / "race.f1cap"
    with CaptureWriter(path, include_created_utc=False) as writer:
        for index, (_, observed_at) in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=int(observed_at * 1_000_000_000),
                    source_ip="127.0.0.1",
                    source_port=20777,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.playback.parse_packet", fake_parse)
    return path


def test_run_pilot_script_is_deterministic_across_repeated_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    script = tmp_path / "guion.jsonl"
    script.write_text(
        "\n".join(
            [
                '{"at_lap": 2, "text": "¿Cómo voy?"}',
                "",
                '{"at_session_time_s": 250.0, "text": "¿Cuánto combustible queda?"}',
                '{"at_lap": 5, "text": "¿Cuánto combustible queda?"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = AppConfig(
        llm=LLMConfig(enabled=True, model="real-model"),
        pilot=PilotConfig(llm_followup=True),
    )
    first = run_pilot_script(capture, script, cfg, responder=_FakeResponder())
    second = run_pilot_script(capture, script, cfg, responder=_FakeResponder())

    assert len(first.exchanges) == 3
    assert first.untriggered == ()
    assert first.exchanges[0].lap == 2
    assert first.exchanges[0].trigger == "lap:2"
    assert first.exchanges[1].t_sim_s >= 250.0
    assert first.exchanges[1].trigger == "session_time_s:250.0"
    assert first.exchanges[2].lap == 5
    # En modo plantilla se desactiva followup_pending y la latencia no entra en la huella.
    assert all(not item.answer.followup_pending for item in first.exchanges)
    assert first.fingerprint == second.fingerprint
    assert first.to_json() == second.to_json()


def test_pilot_fingerprint_is_invariant_to_playback_speed_and_capture_reingestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)

    class _FastClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def wait(self, timeout: float | None) -> bool:
            self.now += timeout if timeout is not None else 0.0
            return True

    fingerprints: list[str] = []
    playback_answers: list[tuple[str, str, object]] = []
    for speed in (0.0, 1.0, 4.0):
        clock = _FastClock()
        session = PlaybackSession(
            capture,
            speed=speed,
            clock=clock,
            wait_timeout=clock.wait,
        )
        try:
            session.play()
            assert session.wait(timeout=30.0)
            assert session.error is None
            answer = session.ask_pilot("¿Cómo voy?")
            accumulator = PilotFingerprintAccumulator()
            accumulator.add(session.pilot_snapshot().recent)
            fingerprints.append(accumulator.fingerprint)
            assert answer.tool_result is not None
            playback_answers.append((answer.text, answer.intent.value, answer.tool_result))
        finally:
            session.stop()

    captured_packets = list(CaptureReader(capture))
    capture_end_s = (
        captured_packets[-1].recv_time_ns - captured_packets[0].recv_time_ns
    ) / 1_000_000_000
    script = tmp_path / "reingesta.jsonl"
    script.write_text(
        json.dumps({"at_session_time_s": capture_end_s, "text": "¿Cómo voy?"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    first = run_pilot_script(capture, script)
    second = run_pilot_script(capture, script)

    assert len(set(fingerprints)) == 1
    assert first.fingerprint == second.fingerprint
    assert len(first.exchanges) == 1
    scripted_answer = first.exchanges[0].answer
    assert scripted_answer.tool_result is not None
    assert (scripted_answer.text, scripted_answer.intent.value, scripted_answer.tool_result) in (
        playback_answers
    )


def test_run_pilot_script_reports_untriggered_steps_beyond_capture_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    script = tmp_path / "guion.jsonl"
    script.write_text(
        '{"at_lap": 2, "text": "¿Cómo voy?"}\n{"at_lap": 99, "text": "¿Y ahora?"}\n',
        encoding="utf-8",
    )

    result = run_pilot_script(capture, script, responder=_FakeResponder())

    assert len(result.exchanges) == 1
    assert len(result.untriggered) == 1
    assert result.untriggered[0].line == 2
    assert result.untriggered[0].at_lap == 99


def test_script_fires_due_steps_behind_a_future_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    script = tmp_path / "guion.jsonl"
    script.write_text(
        '{"at_lap": 99, "text": "¿Y ahora?"}\n{"at_lap": 2, "text": "¿Cómo voy?"}\n',
        encoding="utf-8",
    )

    result = run_pilot_script(capture, script, responder=_FakeResponder())

    assert len(result.exchanges) == 1
    assert result.exchanges[0].line == 2
    assert result.exchanges[0].lap == 2
    assert len(result.untriggered) == 1
    assert result.untriggered[0].line == 1


def test_ask_capture_at_lap_answers_at_target_lap_and_rejects_missing_lap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    responder = _FakeResponder()

    exchange, offline = ask_capture_at_lap(
        capture,
        3,
        "¿Cuánto combustible queda?",
        responder=responder,
    )

    assert exchange.lap == 3
    assert exchange.answer.intent is Intent.FUEL_STATUS
    assert exchange.answer.kind is AnswerKind.ANSWER
    assert offline.packets_parsed > 0

    with pytest.raises(ValueError, match="vuelta 99"):
        ask_capture_at_lap(capture, 99, "¿Cómo voy?", responder=_FakeResponder())


@pytest.mark.parametrize(
    ("content", "expected_line"),
    [
        ('{"at_lap": 2, "text": "ok"}\nesto no es json\n', 2),
        ('["lista"]\n', 1),
        ('{"at_lap": 2}\n', 1),
        ('{"at_lap": 2, "text": "   "}\n', 1),
        ('{"text": "¿Cómo voy?"}\n', 1),
        ('{"at_lap": 2, "at_session_time_s": 10.0, "text": "¿Cómo voy?"}\n', 1),
        ('{"at_lap": 0, "text": "¿Cómo voy?"}\n', 1),
        ('{"at_lap": true, "text": "¿Cómo voy?"}\n', 1),
        ('{"at_session_time_s": -1.0, "text": "¿Cómo voy?"}\n', 1),
        ('{"at_lap": 2, "text": "ok"}\n{"at_lap": 3, "text": "ok", "extra": 1}\n', 2),
    ],
)
def test_invalid_script_reports_line_number(
    tmp_path: Path,
    content: str,
    expected_line: int,
) -> None:
    script = tmp_path / "invalido.jsonl"
    script.write_text(content, encoding="utf-8")

    with pytest.raises(ScriptError) as exc_info:
        load_script(script)

    assert exc_info.value.line == expected_line
    assert f"Línea {expected_line}:" in str(exc_info.value)


def test_empty_script_is_rejected(tmp_path: Path) -> None:
    script = tmp_path / "vacio.jsonl"
    script.write_text("\n   \n", encoding="utf-8")

    with pytest.raises(ScriptError, match="no contiene preguntas"):
        load_script(script)


def test_cli_ask_and_ask_script_json_and_error_handling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    script = tmp_path / "guion.jsonl"
    script.write_text(
        '{"at_lap": 2, "text": "¿Cómo voy?"}\n'
        '{"at_lap": 4, "text": "¿Cuánto combustible queda?"}\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "sessions.db"

    monkeypatch.setattr(
        script_module,
        "DefaultScriptResponder",
        lambda _cfg: _FakeResponder(),
    )

    assert (
        main(
            [
                "ask-script",
                str(capture),
                str(script),
                "--db",
                str(db_path),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["exchanges"]) == 2
    assert len(payload["fingerprint"]) == 64

    with SessionStore.open(db_path) as store:
        sessions = store.list_sessions()
        assert len(sessions) == 1
        session_id = sessions[0].id
        assert session_id is not None
        rows = list(store.iter_pilot_queries(session_id))
        fingerprint = sessions[0].pilot_fingerprint
    assert fingerprint is not None
    assert fingerprint == _expected_fingerprint(rows)
    assert [row.query_id for row in rows] == ["script-1", "script-2"]
    assert [row.text for row in rows] == [
        "¿Cómo voy?",
        "¿Cuánto combustible queda?",
    ]

    assert (
        main(
            [
                "ask-script",
                str(capture),
                str(script),
                "--db",
                str(db_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with SessionStore.open(db_path) as store:
        session_id = store.list_sessions()[0].id
        assert session_id is not None
        assert len(list(store.iter_pilot_queries(session_id))) == 2
        assert store.list_sessions()[0].pilot_fingerprint == fingerprint

    different = tmp_path / "otro.jsonl"
    different.write_text('{"at_lap": 3, "text": "¿Qué neumático llevo?"}\n', encoding="utf-8")
    assert (
        main(
            [
                "ask-script",
                str(capture),
                str(different),
                "--db",
                str(db_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with SessionStore.open(db_path) as store:
        session_id = store.list_sessions()[0].id
        assert session_id is not None
        rows = list(store.iter_pilot_queries(session_id))
        extended_fingerprint = store.list_sessions()[0].pilot_fingerprint
    assert extended_fingerprint is not None
    assert extended_fingerprint == _expected_fingerprint(rows)
    assert extended_fingerprint != fingerprint
    assert [row.query_id for row in rows] == ["script-1", "script-2", "script-1"]
    assert [row.text for row in rows] == [
        "¿Cómo voy?",
        "¿Cuánto combustible queda?",
        "¿Qué neumático llevo?",
    ]

    # Una fila perdida se repara al repetir el guion sin duplicar las demás.
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM pilot_queries WHERE seq = 2")
    assert (
        main(
            [
                "ask-script",
                str(capture),
                str(script),
                "--db",
                str(db_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with SessionStore.open(db_path) as store:
        session_id = store.list_sessions()[0].id
        assert session_id is not None
        rows = list(store.iter_pilot_queries(session_id))
        repaired_fingerprint = store.list_sessions()[0].pilot_fingerprint
    assert repaired_fingerprint is not None
    assert repaired_fingerprint == _expected_fingerprint(rows)
    assert repaired_fingerprint != extended_fingerprint
    assert [row.text for row in rows] == [
        "¿Cómo voy?",
        "¿Qué neumático llevo?",
        "¿Cuánto combustible queda?",
    ]
    assert [row.seq for row in rows] == [1, 3, 4]

    repeated = main(["ask-script", str(capture), str(script), "--db", str(db_path), "--json"])
    capsys.readouterr()
    assert repeated == 0
    with SessionStore.open(db_path) as store:
        assert store.list_sessions()[0].pilot_fingerprint == repaired_fingerprint

    assert main(["ask-script", str(capture), str(script)]) == 0
    human_out = capsys.readouterr().out
    assert "Huella determinista:" in human_out
    assert payload["fingerprint"] in human_out

    assert main(["ask", str(capture), "--lap", "3", "¿Cuánto combustible queda?", "--json"]) == 0
    ask_payload = json.loads(capsys.readouterr().out)
    assert ask_payload["lap"] == 3
    assert ask_payload["answer"]["intent"] == "fuel_status"

    bad_script = tmp_path / "bad.jsonl"
    bad_script.write_text('{"at_lap": 1, "text": "ok"}\n{"at_lap": 0, "text": "mal"}\n', "utf-8")
    assert main(["ask-script", str(capture), str(bad_script)]) == 2
    assert "Línea 2:" in capsys.readouterr().err

    assert main(["ask"]) == 2
    assert "Uso: f1-strategist ask" in capsys.readouterr().err


def test_default_responder_keeps_pending_clarification_between_script_questions() -> None:
    from dataclasses import replace

    from f1_strategist.config import MetricsConfig, StrategyConfig
    from f1_strategist.metrics.engine import MetricsSnapshot
    from f1_strategist.metrics.history import LapHistorySnapshot
    from f1_strategist.pilot.evaluation import build_race_state_from_grid, load_pilot_dataset
    from f1_strategist.pilot.script import DefaultScriptResponder
    from f1_strategist.race_state import PacketFreshness
    from f1_strategist.strategy.types import StrategySnapshot

    dataset = load_pilot_dataset(Path(__file__).parent / "fixtures" / "pilot_questions")
    state = replace(
        build_race_state_from_grid(dataset.default_grid),
        freshness=(PacketFreshness("lap_data", 9.8, 1.5),),
    )
    config = AppConfig()
    responder = DefaultScriptResponder(config)

    first = responder.ask(
        "¿Cómo voy respecto a Halden?",
        inputs=ToolInputs(
            state=state,
            metrics=MetricsSnapshot(),
            strategy=StrategySnapshot(),
            history=LapHistorySnapshot(),
            config=config.pilot,
            now=10.0,
            metrics_config=MetricsConfig(),
            strategy_config=StrategyConfig(),
        ),
    )
    second = responder.ask(
        "Timo",
        inputs=ToolInputs(
            state=state,
            metrics=MetricsSnapshot(),
            strategy=StrategySnapshot(),
            history=LapHistorySnapshot(),
            config=config.pilot,
            now=11.0,
            metrics_config=MetricsConfig(),
            strategy_config=StrategyConfig(),
        ),
    )

    assert first.kind is AnswerKind.CLARIFICATION
    assert second.kind is not AnswerKind.CLARIFICATION
    assert second.intent is Intent.COMPARE_DRIVER
    assert responder.last_routed is not None
    assert [entity.car_index for entity in responder.last_routed.entities] == [12]
    assert "comparación" in second.text or "comparacion" in second.text


def test_session_time_step_uses_game_clock_not_capture_relative_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(
        tmp_path,
        monkeypatch,
        session_time_offset_s=200.0,
        max_observed_s=100.0,
    )
    script = tmp_path / "guion.jsonl"
    script.write_text(
        '{"at_session_time_s": 250.0, "text": "¿Cómo voy?"}\n',
        encoding="utf-8",
    )

    result = run_pilot_script(capture, script, responder=_FakeResponder())

    assert len(result.exchanges) == 1
    exchange = result.exchanges[0]
    assert exchange.trigger == "session_time_s:250.0"
    assert exchange.t_sim_s < 250.0
    assert exchange.t_sim_s <= 100.0


def test_ask_capture_at_lap_requires_exact_lap_in_late_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(
        tmp_path,
        monkeypatch,
        total_laps=12,
        min_observed_s=720.0,
    )
    guided = tmp_path / "guiado.jsonl"
    guided.write_text('{"at_lap": 3, "text": "¿Cómo voy?"}\n', encoding="utf-8")
    guided_result = run_pilot_script(capture, guided, responder=_FakeResponder())
    assert len(guided_result.exchanges) == 1
    assert guided_result.exchanges[0].lap is not None
    assert guided_result.exchanges[0].lap >= 3

    with pytest.raises(ValueError, match="vuelta 3"):
        ask_capture_at_lap(capture, 3, "¿Cómo voy?", responder=_FakeResponder())


def test_session_change_invalidates_clarification_without_resetting_responder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(
        tmp_path,
        monkeypatch,
        session_uid_offset_from_s=90.0,
    )
    script = tmp_path / "guion.jsonl"
    script.write_text(
        '{"at_lap": 1, "text": "¿Cómo voy?"}\n{"at_lap": 3, "text": "¿Y ahora?"}\n',
        encoding="utf-8",
    )
    responder = _TrackingResponder()

    result = run_pilot_script(capture, script, responder=responder)

    assert len(result.exchanges) == 2
    assert responder.cancellations == 1
    assert responder.resets == 1
    assert [item.answer.query_id for item in result.exchanges] == ["script-1", "script-2"]
