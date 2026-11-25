"""Pruebas de la entrada de preguntas de la consola y del panel del piloto."""

from __future__ import annotations

import threading
from io import StringIO
from typing import Any, cast

import pytest
from rich.console import Console

import f1_strategist.cli as cli_module
from f1_strategist.config import AppConfig
from f1_strategist.console import _PilotConsole, render
from f1_strategist.console_messages import (
    PILOT_ACK_TEXT,
    PILOT_CLARIFICATION_LABEL,
    PILOT_EMPTY,
    PILOT_INPUT_BUSY,
    PILOT_INPUT_ERROR,
    PILOT_INPUT_PROMPT,
    PILOT_PANEL_TITLE,
    PILOT_PLAY_HELP,
)
from f1_strategist.pilot.console_input import (
    HISTORY_DOWN,
    HISTORY_UP,
    LineEditor,
)
from f1_strategist.pilot.types import (
    AnswerKind,
    Clarification,
    DriverCandidate,
    Intent,
    PilotAnswer,
    PilotExchange,
    PilotQuery,
    PilotSnapshot,
)
from f1_strategist.session import CaptureSession


class _RecordingPilot:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def submit(self, text: str) -> None:
        self.questions.append(text)


class _PlaybackStub:
    def __init__(self) -> None:
        self.paused = False
        self.speed = 1.0
        self.calls: list[str] = []

    def pause(self) -> None:
        self.paused = True
        self.calls.append("pause")

    def resume(self) -> None:
        self.paused = False
        self.calls.append("resume")

    def set_speed(self, speed: float) -> None:
        self.speed = speed
        self.calls.append(f"speed:{speed}")


def _render_text(config: AppConfig, session: Any, **kwargs: Any) -> str:
    output = StringIO()
    Console(file=output, width=180, no_color=True).print(
        render(config, session, now=100.0, race_now=100.0, **kwargs)
    )
    return output.getvalue()


def _exchange(question: str = "¿Cuánto combustible queda?", answer: str = "32 vueltas."):
    query = PilotQuery(
        query_id="q1",
        text=question,
        language="es",
        received_at=10.0,
    )
    return PilotExchange(
        query=query,
        answer=PilotAnswer(
            query_id="q1",
            intent=Intent.FUEL_STATUS,
            kind=AnswerKind.ANSWER,
            text=answer,
            message_key="fuel_status",
        ),
    )


def test_line_editor_types_unicode_and_deletes_with_backspace() -> None:
    editor = LineEditor()
    for char in "¿Peña? ñandú":
        editor.feed(char)
    assert editor.text == "¿Peña? ñandú"

    editor.feed("\x7f")
    editor.feed("\b")
    assert editor.text == "¿Peña? ñan"

    assert editor.feed("\r") == "¿Peña? ñan"
    assert editor.text == ""


def test_line_editor_escape_clears_without_submitting() -> None:
    editor = LineEditor()
    editor.type_text("borrador")
    assert editor.feed("\x1b") is None
    assert editor.text == ""
    assert editor.history == []


def test_line_editor_history_navigation_restores_draft() -> None:
    editor = LineEditor()
    editor.type_text("primera")
    assert editor.feed("\r") == "primera"
    editor.type_text("segunda")
    assert editor.feed("\r") == "segunda"

    editor.type_text("borrador")
    editor.feed(HISTORY_UP)
    assert editor.text == "segunda"
    editor.feed(HISTORY_UP)
    assert editor.text == "primera"
    editor.feed(HISTORY_DOWN)
    assert editor.text == "segunda"
    editor.feed(HISTORY_DOWN)
    assert editor.text == "borrador"


def test_line_editor_truncates_pasted_text_to_max_chars() -> None:
    editor = LineEditor(max_chars=5)
    editor.type_text("abcdefgh")
    assert editor.text == "abcde"

    editor.feed("\r")
    assert editor.history == ["abcde"]


def test_line_editor_ignores_empty_submit() -> None:
    editor = LineEditor()
    editor.type_text("   ")
    assert editor.feed("\r") is None
    assert editor.history == []


def test_render_pilot_panel_shows_exchange_and_clarification() -> None:
    config = AppConfig()
    session = CaptureSession(config)
    clarification = Clarification(
        message_key="driver_ambiguous",
        options=(
            DriverCandidate(car_index=1, name="Peña"),
            DriverCandidate(car_index=2, name="Nadal"),
        ),
        expires_at=30.0,
    )
    snapshot = PilotSnapshot(
        recent=(_exchange(),),
        pending_clarification=clarification,
    )
    session.pilot_snapshot = lambda: snapshot  # type: ignore[method-assign]

    text = _render_text(config, session)

    assert PILOT_PANEL_TITLE in text
    assert "¿Cuánto combustible queda?" in text
    assert "32 vueltas." in text
    assert "Respuesta" in text
    assert PILOT_CLARIFICATION_LABEL in text
    assert "Peña" in text and "Nadal" in text


def test_render_pilot_panel_shows_ack_and_input_line() -> None:
    config = AppConfig()
    session = CaptureSession(config)

    text = _render_text(
        config,
        session,
        pilot_pending="¿Y el neumático?",
        pilot_draft="¿Y el neumático?",
        pilot_input_warning="Entrada de preguntas desactivada.",
        pilot_error=PILOT_INPUT_ERROR,
    )

    assert PILOT_ACK_TEXT in text
    assert f"{PILOT_INPUT_PROMPT}¿Y el neumático?" in text
    assert "Entrada de preguntas desactivada." in text
    assert PILOT_INPUT_ERROR in text


def test_render_pilot_panel_empty_state() -> None:
    config = AppConfig()
    session = CaptureSession(config)

    text = _render_text(config, session)

    assert PILOT_PANEL_TITLE in text
    assert PILOT_EMPTY in text


def test_render_play_help_keeps_text_mode_hint() -> None:
    config = AppConfig()
    session = CaptureSession(config)

    text = _render_text(config, session, pilot_help=PILOT_PLAY_HELP)

    assert "/: preguntar" in text


def test_pilot_console_shows_ack_while_ask_is_in_flight() -> None:
    release = threading.Event()
    started = threading.Event()
    calls: list[str] = []

    class SlowSession:
        def ask_pilot(self, text: str) -> PilotAnswer:
            calls.append(text)
            started.set()
            release.wait(5)
            return PilotAnswer(
                query_id="q1",
                intent=Intent.UNKNOWN,
                kind=AnswerKind.ANSWER,
                text="listo",
                message_key="pilot_answer",
            )

    pilot = _PilotConsole(cast(Any, SlowSession()))
    try:
        pilot.submit("¿Combustible?")
        assert pilot.pending_text == "¿Combustible?"
        assert started.wait(2)

        pilot.submit("otra pregunta")
        assert calls == ["¿Combustible?"]
        assert pilot.error_text == PILOT_INPUT_BUSY

        release.set()
        assert pilot._future is not None
        pilot._future.result(timeout=5)
        pilot.poll()
        assert pilot.pending_text is None
        assert pilot.error_text is None
    finally:
        pilot.close()


def test_pilot_console_reports_ask_failure() -> None:
    class BrokenSession:
        def ask_pilot(self, text: str) -> PilotAnswer:
            raise RuntimeError("boom")

    pilot = _PilotConsole(cast(Any, BrokenSession()))
    try:
        pilot.submit("¿Combustible?")
        assert pilot._future is not None
        assert pilot._future.exception(timeout=5) is not None
        pilot.poll()
        assert pilot.pending_text is None
        assert pilot.error_text == PILOT_INPUT_ERROR
    finally:
        pilot.close()


def test_play_keys_keep_existing_controls() -> None:
    session = _PlaybackStub()
    editor = LineEditor()
    pilot = _RecordingPilot()

    def apply(key: str, editing: bool = False) -> tuple[bool, bool]:
        return cli_module._apply_play_key(
            key,
            cast(Any, session),
            editor,
            cast(Any, pilot),
            editing,
        )

    assert apply(" ") == (False, False)
    assert session.paused is True
    assert apply(" ") == (False, False)
    assert session.paused is False

    session.speed = 1.0
    assert apply("+") == (False, False)
    assert session.speed == 2.0
    assert apply("-") == (False, False)
    assert session.speed == 1.0

    assert apply("q") == (False, True)


def test_play_slash_opens_text_mode_and_enter_submits() -> None:
    session = _PlaybackStub()
    editor = LineEditor()
    pilot = _RecordingPilot()

    def apply(key: str, editing: bool = False) -> tuple[bool, bool]:
        return cli_module._apply_play_key(
            key,
            cast(Any, session),
            editor,
            cast(Any, pilot),
            editing,
        )

    editing, quit_ = apply("/")
    assert (editing, quit_) == (True, False)

    for char in "¿Peña?":
        editing, quit_ = apply(char, editing)
    assert editor.text == "¿Peña?"
    assert session.calls == []

    editing, quit_ = apply("\r", editing)
    assert (editing, quit_) == (False, False)
    assert pilot.questions == ["¿Peña?"]
    assert editor.text == ""


def test_play_escape_cancels_text_mode_and_q_is_typed() -> None:
    session = _PlaybackStub()
    editor = LineEditor()
    pilot = _RecordingPilot()

    def apply(key: str, editing: bool = False) -> tuple[bool, bool]:
        return cli_module._apply_play_key(
            key,
            cast(Any, session),
            editor,
            cast(Any, pilot),
            editing,
        )

    editing, quit_ = apply("/")
    assert editing is True
    editing, quit_ = apply("q", editing)
    assert (editing, quit_) == (True, False)
    assert editor.text == "q"

    editing, quit_ = apply("\x1b", editing)
    assert (editing, quit_) == (False, False)
    assert editor.text == ""
    assert pilot.questions == []


@pytest.mark.parametrize("command", ["run", "play"])
def test_cli_accepts_no_input(command: str) -> None:
    argv = [command]
    if command == "play":
        argv.append("captura.f1cap")
    argv.append("--no-input")

    args = cli_module._build_parser().parse_args(argv)

    assert args.no_input is True
