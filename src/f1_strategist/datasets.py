"""Catálogo de capturas de referencia y verificación de sus expectativas."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .capture import CaptureError
from .offline import run_capture_offline
from .parser import FinalClassificationPacket, ParsedPacket
from .race_state import RaceState

DEFAULT_MANIFEST = Path("tests/fixtures/real/manifest.toml")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_KEYS = (
    "track",
    "session_type",
    "total_laps",
    "cars",
    "pit_stops_total",
    "best_lap_ms",
    "game_version",
)


class ManifestError(ValueError):
    """El manifiesto de escenarios no tiene el formato esperado."""


@dataclass(frozen=True, slots=True)
class ScenarioExpectations:
    """Valores que debe reproducir una captura al procesarla offline."""

    track: str
    session_type: str
    total_laps: int
    cars: int
    pit_stops_total: int
    best_lap_ms: int
    game_version: str


@dataclass(frozen=True, slots=True)
class Scenario:
    """Entrada del manifiesto con la captura y sus expectativas."""

    id: str
    file: Path
    relative_file: str
    sha256: str
    bytes: int
    description: str
    tags: tuple[str, ...]
    expected: ScenarioExpectations

    @property
    def available(self) -> bool:
        """Indica si el archivo de la captura está presente."""
        return self.file.is_file()


@dataclass(frozen=True, slots=True)
class Manifest:
    """Manifiesto cargado desde disco."""

    path: Path
    scenarios: tuple[Scenario, ...]

    def get(self, scenario_id: str) -> Scenario:
        """Devuelve un escenario por identificador."""
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise KeyError(scenario_id)


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    """Valores extraídos de una captura al ejecutarla offline."""

    track: str | None
    session_type: str | None
    total_laps: int | None
    cars: int | None
    pit_stops_total: int | None
    best_lap_ms: int | None
    game_version: str | None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Resultado de verificar un escenario del manifiesto."""

    scenario: Scenario
    available: bool
    problems: tuple[str, ...]
    observation: ScenarioObservation | None = None

    @property
    def ok(self) -> bool:
        """La verificación solo es correcta si hay archivo y no hay problemas."""
        return self.available and not self.problems


class _Collector:
    def __init__(self) -> None:
        self.state = RaceState()
        self.classification: FinalClassificationPacket | None = None

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        if isinstance(packet, FinalClassificationPacket):
            self.classification = packet


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    """Carga y valida un manifiesto TOML de escenarios."""
    manifest_path = Path(path)
    try:
        with manifest_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"manifiesto TOML no válido en {manifest_path}: {exc}") from exc

    raw_scenarios = data.get("scenario")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ManifestError(f"{manifest_path}: falta al menos una entrada [[scenario]]")

    base = manifest_path.parent
    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_scenarios):
        where = f"{manifest_path}: escenario #{index + 1}"
        if not isinstance(raw, Mapping):
            raise ManifestError(f"{where}: se esperaba una tabla TOML")
        scenario = _parse_scenario(raw, base, where)
        if scenario.id in seen:
            raise ManifestError(f"{where}: identificador duplicado {scenario.id!r}")
        seen.add(scenario.id)
        scenarios.append(scenario)
    return Manifest(path=manifest_path, scenarios=tuple(scenarios))


def verify_scenario(scenario: Scenario) -> VerificationResult:
    """Comprueba el archivo, su hash y las expectativas declaradas."""
    if not scenario.available:
        return VerificationResult(
            scenario=scenario,
            available=False,
            problems=(f"archivo no disponible: {scenario.file}",),
        )

    problems: list[str] = []
    actual_size = scenario.file.stat().st_size
    if actual_size != scenario.bytes:
        problems.append(
            f"tamaño no coincide: esperado {scenario.bytes} bytes, obtenido {actual_size}"
        )
    actual_hash = _file_sha256(scenario.file)
    if actual_hash != scenario.sha256:
        problems.append(f"sha256 no coincide: esperado {scenario.sha256}, obtenido {actual_hash}")

    collector = _Collector()
    try:
        run_capture_offline(scenario.file, [collector])
    except CaptureError as exc:
        problems.append(f"no se pudo leer la captura: {exc}")
        return VerificationResult(
            scenario=scenario,
            available=True,
            problems=tuple(problems),
        )
    observation = _observe(collector)
    problems.extend(_compare(scenario.expected, observation))
    return VerificationResult(
        scenario=scenario,
        available=True,
        problems=tuple(problems),
        observation=observation,
    )


def _parse_scenario(raw: Mapping[str, object], base: Path, where: str) -> Scenario:
    scenario_id = _required_str(raw, "id", where)
    relative_file = _required_str(raw, "file", where)
    sha256 = _required_str(raw, "sha256", where).lower()
    if not _SHA256_RE.fullmatch(sha256):
        raise ManifestError(f"{where}: sha256 debe tener 64 dígitos hexadecimales")
    size = _required_int(raw, "bytes", where)
    if size < 0:
        raise ManifestError(f"{where}: bytes no puede ser negativo")
    description = _required_str(raw, "description", where)
    tags = _optional_tags(raw, where)
    expected_raw = raw.get("expected")
    if not isinstance(expected_raw, Mapping):
        raise ManifestError(f"{where}: falta la tabla [scenario.expected]")
    expected = _parse_expectations(expected_raw, where)
    return Scenario(
        id=scenario_id,
        file=(base / relative_file).resolve(),
        relative_file=relative_file,
        sha256=sha256,
        bytes=size,
        description=description,
        tags=tags,
        expected=expected,
    )


def _parse_expectations(raw: Mapping[str, object], where: str) -> ScenarioExpectations:
    values: dict[str, object] = {}
    for key in _EXPECTED_KEYS:
        if key not in raw:
            raise ManifestError(f"{where}: falta la expectativa {key!r}")
        values[key] = raw[key]
    return ScenarioExpectations(
        track=_required_str(values, "track", where),
        session_type=_required_str(values, "session_type", where),
        total_laps=_required_int(values, "total_laps", where),
        cars=_required_int(values, "cars", where),
        pit_stops_total=_required_int(values, "pit_stops_total", where),
        best_lap_ms=_required_int(values, "best_lap_ms", where),
        game_version=_required_str(values, "game_version", where),
    )


def _required_str(raw: Mapping[str, object], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: {key!r} debe ser una cadena no vacía")
    return value


def _required_int(raw: Mapping[str, object], key: str, where: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(f"{where}: {key!r} debe ser un entero no negativo")
    return value


def _optional_tags(raw: Mapping[str, object], where: str) -> tuple[str, ...]:
    value = raw.get("tags", [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ManifestError(f"{where}: 'tags' debe ser una lista de cadenas")
    return tuple(value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _observe(collector: _Collector) -> ScenarioObservation:
    state = collector.state.snapshot()
    session = state.session
    final = collector.classification
    if final is None:
        return ScenarioObservation(
            track=session.track,
            session_type=session.session_type,
            total_laps=session.total_laps,
            cars=None,
            pit_stops_total=None,
            best_lap_ms=None,
            game_version=None,
        )
    classified = tuple(car for car in final.classification if car is not None)
    pit_stops = sum(car.pit_stops for car in classified)
    best_laps = [car.best_lap_time_ms for car in classified if car.best_lap_time_ms > 0]
    header = final.header
    return ScenarioObservation(
        track=session.track,
        session_type=session.session_type,
        total_laps=session.total_laps,
        cars=final.num_cars,
        pit_stops_total=pit_stops,
        best_lap_ms=min(best_laps) if best_laps else None,
        game_version=f"{header.game_major_version}.{header.game_minor_version}",
    )


def _compare(
    expected: ScenarioExpectations,
    observation: ScenarioObservation,
) -> list[str]:
    problems: list[str] = []
    for key in _EXPECTED_KEYS:
        wanted = getattr(expected, key)
        actual = getattr(observation, key)
        if actual is None:
            problems.append(f"no se pudo obtener la expectativa {key!r} de la captura")
        elif actual != wanted:
            problems.append(
                f"expectativa {key!r} incumplida: esperado {wanted!r}, obtenido {actual!r}"
            )
    return problems
