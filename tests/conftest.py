"""Isolate process-global application loggers and keep tests out of host log paths."""

import logging

import pytest

from atlas_core import logging as atlas_logging


@pytest.fixture
def application_logging(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas_logging, "_DEFAULT_DIRECTORY", tmp_path)
    for key in (
        "ATLAS_LOG_LEVEL",
        "ATLAS_PROGRAM_NAME",
        "ATLAS_HOST_ID",
        "ATLAS_RUN_ID",
        "ATLAS_OPERATION_ID",
        "ATLAS_CONTEXT_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    original = dict(logging.Logger.manager.loggerDict)
    yield
    for name in tuple(atlas_logging._FILES):
        logger = logging.getLogger(name)
        for handler in tuple(logger.handlers):
            if isinstance(
                handler, (atlas_logging._FileHandler, atlas_logging._StderrHandler)
            ):
                logger.removeHandler(handler)
                handler.close()
    atlas_logging._FILES.clear()
    atlas_logging._FAILED_PATHS.clear()
    for name in set(logging.Logger.manager.loggerDict) - original.keys():
        del logging.Logger.manager.loggerDict[name]
