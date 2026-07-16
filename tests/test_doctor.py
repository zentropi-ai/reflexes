"""Tests for `reflexes doctor` — the anti-silent-fail-open check."""
import json
from pathlib import Path

import pytest

from reflexes.doctor import _check_config, _find_config, run_doctor


def _write_config(tmp_path, broken=False):
    pol = tmp_path / "policies"
    pol.mkdir()
    (pol / "p.md").write_text("# Criteria\n\n## Overview\nAlways fires.\n")
    cfg = tmp_path / "reflexes.yaml"
    # A "broken" config points a binding at a policy file that isn't on disk —
    # the doctor must catch it (it would otherwise fail open at runtime).
    ref = "./policies/missing.md" if broken else "./policies/p.md"
    cfg.write_text(
        f"reflexes:\n  default:\n    - policy: {ref}\n"
        f"      name: {Path(ref).stem}\n"
        "      trigger: before_tool_call\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n"
    )
    return cfg


def test_valid_config_is_ok(tmp_path):
    status, msg, bindings = _check_config(_write_config(tmp_path), [])
    assert status == "ok"
    assert len(bindings) == 1


def test_broken_policy_is_fail_not_silent(tmp_path):
    cfg = _write_config(tmp_path, broken=True)  # binding points at a missing file
    status, msg, _ = _check_config(cfg, [])
    assert status == "fail"
    assert "fail open" in msg


def test_missing_config_is_fail():
    status, msg, _ = _check_config(None, [])
    assert status == "fail"


def test_find_config_prefers_explicit(tmp_path):
    cfg = _write_config(tmp_path)
    assert _find_config(str(cfg)) == cfg
    assert _find_config(str(tmp_path / "nope.yaml")) is None


def test_missing_zentropi_key_is_warn_not_fail(tmp_path, monkeypatch):
    """Product decision: a missing Zentropi API key is a clean WARN (get a
    free key or swap the evaluator), NOT a hard FAIL. The install shouldn't
    read as a crash — the doctor runs, warns, and exits 1, not 2."""
    from reflexes.doctor import _check_evaluator
    monkeypatch.delenv("ZENTROPI_API_KEY", raising=False)
    cfg = _write_config(tmp_path)  # type: zentropi, no key available
    status, msg = _check_evaluator(cfg)
    assert status == "warn", f"expected warn, got {status!r}: {msg}"
    assert "zentropi.ai" in msg.lower()  # tells the user how to get a key
    assert "swap" in msg.lower()          # and the alternative


def test_missing_remote_evaluator_key_names_the_specific_cause(tmp_path, monkeypatch):
    """When a non-Zentropi evaluator can't configure (e.g. missing key), the
    doctor must surface the SPECIFIC reason configure() stashed — naming the
    exact env var — not a generic 'failed to configure' line."""
    from reflexes.doctor import _check_evaluator
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pol = tmp_path / "policies"
    pol.mkdir()
    (pol / "p.md").write_text("# Criteria\n\n## Overview\nAlways fires.\n")
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n  default:\n    - policy: ./policies/p.md\n"
        "      name: p\n      trigger: before_tool_call\n      action: deny\n"
        "evaluators:\n  default:\n    type: anthropic\n"
    )
    status, msg = _check_evaluator(cfg)
    assert status == "fail"
    assert "ANTHROPIC_API_KEY" in msg, f"doctor should name the missing env var: {msg}"


def test_evaluator_probe_error_is_fail_not_crash(tmp_path, monkeypatch):
    """safe_acheck RETURNS an EvaluatorError on a transient failure (it doesn't
    raise). The doctor must treat that as a clean FAIL (evaluator unreachable =
    fail-open risk), never crash accessing .label on it."""
    from reflexes.doctor import _check_evaluator
    import reflexes.core as core
    from reflexes.core import EvaluatorError

    monkeypatch.setenv("ZENTROPI_API_KEY", "test-key")  # past the no-key WARN branch
    cfg = _write_config(tmp_path)

    async def _boom(*a, **k):
        return EvaluatorError(policy_name="probe", error="503 upstream overloaded")

    monkeypatch.setattr(core, "safe_acheck", _boom)
    status, msg = _check_evaluator(cfg)
    assert status == "fail"
    assert "unreachable" in msg


def test_codex_hooks_enabled_detection(tmp_path):
    """Codex only loads hooks when `codex_hooks = true` is under [features]; a
    bare top-level key is silently ignored (the fail-open the doctor must catch)."""
    from reflexes.doctor import _codex_hooks_enabled
    good = tmp_path / "good.toml"
    good.write_text("model = \"gpt-5\"\n[features]\ncodex_hooks = true\n")
    assert _codex_hooks_enabled(good) is True
    bare = tmp_path / "bare.toml"          # bare top-level key -> ignored by Codex
    bare.write_text("codex_hooks = true\nmodel = \"gpt-5\"\n")
    assert _codex_hooks_enabled(bare) is False
    off = tmp_path / "off.toml"
    off.write_text("[features]\ncodex_hooks = false\n")
    assert _codex_hooks_enabled(off) is False
    assert _codex_hooks_enabled(tmp_path / "missing.toml") is None


def test_codex_host_warns_when_hooks_json_wired_but_feature_disabled(tmp_path, monkeypatch):
    """The regression from the round-trip test: hooks.json wired + config.toml
    missing [features] codex_hooks => Codex silently doesn't enforce. Doctor must
    NOT report this Codex host as OK."""
    from reflexes.doctor import _check_hosts
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "hooks.json").write_text('{"hooks":[{"command":"reflexes-hook --host codex"}]}')
    (codex / "config.toml").write_text("codex_hooks = true\n")  # bare = ignored by Codex
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("HOME", str(tmp_path))  # avoid detecting other real hosts
    hosts = {name: (status, msg) for name, status, msg in _check_hosts()}
    assert "OpenAI Codex" in hosts
    status, msg = hosts["OpenAI Codex"]
    assert status == "warn", f"expected warn (not OK) for disabled codex hooks, got {status}: {msg}"
    assert "features" in msg.lower()


def test_run_doctor_exit_codes(tmp_path, monkeypatch, capsys):
    # Broken config must exit 2 even if evaluator/hosts are fine or absent.
    cfg = _write_config(tmp_path, broken=True)
    monkeypatch.setenv("HOME", str(tmp_path))  # no hosts detected
    rc = run_doctor(config=str(cfg))
    assert rc == 2
    out = capsys.readouterr().out
    assert "fail open" in out
