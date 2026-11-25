"""La configuración del piloto llega a los comunicadores de las tres rutas."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import AppConfig, MetricsConfig, PilotConfig, StrategyConfig
from f1_strategist.metrics.types import Quality
from f1_strategist.offline import OfflineEventPipeline
from f1_strategist.pilot.engine import PilotEngine
from f1_strategist.pilot.types import Intent, ToolInputs, ToolResult
from f1_strategist.playback import PlaybackSession
from f1_strategist.race_state import PacketFreshness
from f1_strategist.session import CaptureSession
from tests.conftest import make_packet


def _capture(path: Path) -> Path:
    with CaptureWriter(path) as writer:
        writer.write(CapturedPacket(0, "127.0.0.1", 20777, make_packet(1)))
    return path


def test_pilot_config_reaches_live_playback_and_offline_communicators(tmp_path: Path) -> None:
    pilot = PilotConfig(llm_followup=False, followup_timeout_s=3.0, history_size=7)
    config = AppConfig(pilot=pilot)

    live = CaptureSession(config)
    assert live._agent._pilot is pilot

    playback = PlaybackSession(_capture(tmp_path / "play.f1cap"), pilot_config=pilot)
    assert playback._agent._pilot is pilot

    offline = OfflineEventPipeline(config)
    try:
        assert offline._agent._pilot is pilot
    finally:
        offline.stop()


def test_real_pilot_tools_receive_strategy_and_metrics_config(tmp_path: Path) -> None:
    metrics = MetricsConfig(traffic_gap_s=2.5)
    strategy = StrategyConfig(max_pit_in_laps=4)
    config = AppConfig(metrics=metrics, strategy=strategy)
    live = CaptureSession(config)
    playback = PlaybackSession(
        _capture(tmp_path / "strategy.f1cap"), config=metrics, strategy_config=strategy
    )
    offline = OfflineEventPipeline(config)
    try:
        for session in (live, playback, offline):
            _assert_tool_config(session._pilot, metrics, strategy)
    finally:
        offline.stop()


def _assert_tool_config(
    engine: PilotEngine, metrics: MetricsConfig, strategy: StrategyConfig
) -> None:
    inputs = engine._snapshots()
    state = replace(inputs.state, freshness=(PacketFreshness("lap_data", 9.8, 1.5),))
    engine._snapshots = lambda: replace(inputs, state=state)
    received: list[ToolInputs] = []

    def tool(_query: object, tool_inputs: ToolInputs) -> ToolResult:
        received.append(tool_inputs)
        return ToolResult(intent=Intent.FUEL_STATUS, quality=Quality.INSUFFICIENT)

    engine._tools = {Intent.FUEL_STATUS: tool}
    engine.ask("¿Cuánto combustible queda?", now=10.0)
    assert len(received) == 1
    assert received[0].metrics_config is metrics
    assert received[0].strategy_config is strategy
