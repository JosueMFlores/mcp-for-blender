"""Telemetry with and without the release-generated config.py.

config.py holds the project's endpoint and key and is generated at release
time, so source checkouts don't have it. They must still run, with telemetry
off; release builds must keep using the config they ship.
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.util
import json
import sys
import types

import pytest

from blender_mcp import consent_prompt, server, telemetry, trajectory
from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION

CONFIG = "blender_mcp.config"


class FakeBlender:
    def __init__(self):
        self.consent = False
        self.sent = []

    def send_command(self, command, params=None, read_only=False):
        self.sent.append(command)
        if command == "get_addon_info":
            return {"protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION, "addon_version": [1, 0],
                    "capabilities": [], "blender_version": "4.2.0", "premium_generators": []}
        if command == "get_telemetry_consent":
            return {"consent": self.consent}
        if command == "set_telemetry_consent":
            self.consent = params["consent"]
            return {"consent": self.consent}
        return {"enabled": False}


@pytest.fixture(autouse=True)
def posts(monkeypatch, tmp_path):
    """A fresh collector per test, kept off the network and the real home dir."""
    monkeypatch.setattr(telemetry, "_telemetry_collector", None)
    monkeypatch.setattr(telemetry.TelemetryCollector, "_get_data_directory", lambda self: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # remembered consent answers
    for var in ("DISABLE_TELEMETRY", "BLENDER_MCP_DISABLE_TELEMETRY", "MCP_DISABLE_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    consent_prompt.reset_for_tests()
    sent = []
    monkeypatch.setattr(telemetry.httpx, "post", lambda url, **kwargs: sent.append(url))
    return sent


@pytest.fixture
def blender(monkeypatch):
    fake = FakeBlender()
    monkeypatch.setattr(server, "get_blender_connection", lambda: fake)
    monkeypatch.setattr(server, "_addon_handshake", None)
    monkeypatch.setattr(server, "_addon_handshake_checked", False)
    return fake


@pytest.fixture
def no_config(monkeypatch):
    # None in sys.modules fails the import just as a missing file does, even
    # in a checkout that has a real config.py.
    monkeypatch.setitem(sys.modules, CONFIG, None)


@pytest.fixture
def release_config(monkeypatch):
    config = types.SimpleNamespace(
        enabled=True, supabase_url="https://telemetry.invalid", supabase_anon_key="key",
        supabase_bucket="bucket", max_prompt_length=1000, timeout=1.0,
    )
    module = types.ModuleType(CONFIG)
    module.telemetry_config = config
    monkeypatch.setitem(sys.modules, CONFIG, module)
    return config


@pytest.fixture
def disabled_by_env(release_config, monkeypatch):
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")


def _elicitation_ctx(asked: list):
    async def elicit_form(message, requestedSchema, related_request_id=None):
        from mcp.types import ElicitResult

        asked.append(message)
        return ElicitResult(action="decline")

    session = types.SimpleNamespace(elicit_form=elicit_form)
    return types.SimpleNamespace(request_context=types.SimpleNamespace(session=session), request_id=1)


def test_missing_config_turns_telemetry_off(no_config):
    collector = telemetry.get_telemetry()
    assert isinstance(collector.config, telemetry.UnconfiguredTelemetry)
    assert not collector.config.enabled
    assert not telemetry.is_telemetry_enabled()


def test_missing_config_sends_nothing_and_asks_blender_nothing(no_config, blender, posts):
    telemetry.record_tool_usage("get_scene_info", success=True, duration_ms=1.0)
    collector = telemetry.get_telemetry()
    assert collector.upload_screenshot(b"png", "screenshot") == ""
    assert not collector._check_user_consent()
    recorder = trajectory.TrajectoryRecorder()
    assert recorder.record_step(tool_name="execute_blender_code") is False
    assert recorder.record_feedback(feedback="accept") is False
    assert collector._queue.empty()
    assert posts == []
    assert blender.sent == []


def test_addon_status_answers_without_config(no_config, blender):
    blender.consent = True
    reply = asyncio.run(server.get_addon_status(None))
    assert not reply.startswith("Error"), reply
    payload, _ = json.JSONDecoder().raw_decode(reply)
    # The box is ticked in Blender, but a source build has nowhere to send data.
    assert payload["telemetry_consent"] is True
    assert payload["telemetry_enabled"] is False


def test_addon_status_reports_telemetry_enabled_with_a_release_config(release_config, blender):
    reply = asyncio.run(server.get_addon_status(None))
    payload, _ = json.JSONDecoder().raw_decode(reply)
    assert payload["telemetry_enabled"] is True and payload["telemetry_consent"] is False


def test_disable_telemetry_works_without_config(no_config, blender):
    blender.consent = True
    reply = server.disable_telemetry(None)
    assert "now OFF" in reply, reply
    assert blender.consent is False
    # Nothing is sent in a source build, so don't claim usage counts still are.
    assert "usage counts" not in reply and "nothing is sent" in reply


def test_disable_telemetry_mentions_usage_counts_with_a_release_config(release_config, blender):
    reply = server.disable_telemetry(None)
    assert "now OFF" in reply and "usage counts" in reply, reply


@pytest.mark.parametrize("config, asked", [
    ("no_config", False),
    ("disabled_by_env", False),
    ("release_config", True),
])
def test_consent_prompt_only_when_telemetry_can_run(request, blender, monkeypatch, config, asked):
    request.getfixturevalue(config)
    monkeypatch.setattr(consent_prompt, "_client_supports_elicitation", lambda ctx: True)
    prompts = []
    asyncio.run(consent_prompt.maybe_prompt_for_consent(_elicitation_ctx(prompts)))
    assert bool(prompts) is asked
    assert ("get_telemetry_consent" in blender.sent) is asked


def test_release_config_is_used(release_config):
    collector = telemetry.get_telemetry()
    assert collector.config is release_config
    assert telemetry.is_telemetry_enabled()


class _ConfigWithMissingDependency(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        return importlib.util.spec_from_loader(name, self) if name == CONFIG else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        import a_dependency_that_is_not_installed  # noqa: F401


def test_broken_release_config_is_not_mistaken_for_a_missing_one(monkeypatch):
    monkeypatch.delitem(sys.modules, CONFIG, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_ConfigWithMissingDependency(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError, match="a_dependency_that_is_not_installed"):
        telemetry.TelemetryCollector()
