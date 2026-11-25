"""La configuración del piloto llega a los comunicadores de las tres rutas."""

from __future__ import annotations

from pathlib import Path

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import AppConfig, PilotConfig
from f1_strategist.offline import OfflineEventPipeline
from f1_strategist.playback import PlaybackSession
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
