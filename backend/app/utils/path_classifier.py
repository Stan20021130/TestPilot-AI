"""TestPilot AI — Architectural path classifier.

Shared by the Impact Agent and the Impact Analyzer service so the two never
drift apart. Classification is structural (directory segments and filename
suffixes), not a substring search over the whole path: ``app/api/v1/sessions.py``
must classify as an API surface, while ``utils/rapid.py`` must not.
"""

from __future__ import annotations

from pathlib import Path

_API_SEGMENTS = {
    "api",
    "apis",
    "routes",
    "routers",
    "router",
    "controllers",
    "controller",
    "views",
    "endpoints",
    "handlers",
    "http",
    "graphql",
}
_SERVICE_SEGMENTS = {"services", "service", "domain", "usecases", "workers", "tasks", "jobs"}
_MODEL_SEGMENTS = {"models", "model", "schemas", "entities", "db", "database", "migrations"}
_TEST_SEGMENTS = {"tests", "test", "__tests__", "spec", "specs"}

_API_SUFFIXES = ("_api", "_routes", "_router", "_controller", "_view", "_endpoint", "_handler")
_SERVICE_SUFFIXES = ("_service", "_usecase", "_worker", "_task", "_manager")
_MODEL_SUFFIXES = ("_model", "_schema", "_entity")


def path_segments(path: str) -> list[str]:
    """Lower-cased directory segments of a repo-relative path."""
    parts = Path(path.replace("\\", "/")).parts
    return [part.lower() for part in parts[:-1]]


def path_stem(path: str) -> str:
    """Lower-cased file name without extension."""
    return Path(path.replace("\\", "/")).stem.lower()


def classify_path(path: str) -> str:
    """Classify a file path as one of: api, service, model, test, module."""
    segments = path_segments(path)
    stem = path_stem(path)

    if any(segment in _TEST_SEGMENTS for segment in segments):
        return "test"
    if any(segment in _API_SEGMENTS for segment in segments) or stem.endswith(_API_SUFFIXES):
        return "api"
    if any(segment in _SERVICE_SEGMENTS for segment in segments) or stem.endswith(
        _SERVICE_SUFFIXES
    ):
        return "service"
    if any(segment in _MODEL_SEGMENTS for segment in segments) or stem.endswith(_MODEL_SUFFIXES):
        return "model"
    return "module"
