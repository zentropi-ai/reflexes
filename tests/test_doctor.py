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


def test_missing_default_key_is_warn_not_fail(tmp_path, monkeypatch):
    """Product decision: a missing key for the ZERO-CONFIG DEFAULT (CoPE via
    Parasail — a config with no `evaluators:` block) is a clean WARN (get a key
    or swap the evaluator), NOT a hard FAIL. The install shouldn't read as a
    crash — the doctor runs, warns, and exits 1, not 2."""
    from reflexes.doctor import _check_evaluator
    monkeypatch.delenv("PARASAIL_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    pol = tmp_path / "policies"
    pol.mkdir()
    (pol / "p.md").write_text("# Criteria\n\n## Overview\nAlways fires.\n")
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(  # reflexes only, NO evaluators block → zero-config default
        "reflexes:\n  default:\n    - policy: ./policies/p.md\n"
        "      name: p\n      trigger: before_tool_call\n      action: deny\n"
    )
    status, msg = _check_evaluator(cfg)
    assert status == "warn", f"expected warn, got {status!r}: {msg}"
    assert "parasail" in msg.lower()  # tells the user how to get a key
    assert "swap" in msg.lower()       # and the alternative


def test_missing_explicit_zentropi_key_is_fail(tmp_path, monkeypatch):
    """An explicitly chosen evaluator (even Zentropi) missing its key is now a
    config error to fix → FAIL with the specific cause. Only the zero-config
    default gets the softer WARN."""
    from reflexes.doctor import _check_evaluator
    monkeypatch.delenv("ZENTROPI_API_KEY", raising=False)
    cfg = _write_config(tmp_path)  # explicit type: zentropi, no key available
    status, msg = _check_evaluator(cfg)
    assert status == "fail", f"expected fail, got {status!r}: {msg}"
    assert "ZENTROPI_API_KEY" in msg  # names the specific missing env var


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
    """Codex loads hooks only when `hooks = true` is under [features]; a bare
    top-level key is silently ignored (the fail-open the doctor must catch)."""
    from reflexes.doctor import _codex_hooks_enabled
    good = tmp_path / "good.toml"
    good.write_text("model = \"gpt-5\"\n[features]\nhooks = true\n")
    assert _codex_hooks_enabled(good) is True
    bare = tmp_path / "bare.toml"                            # bare top-level -> ignored
    bare.write_text("hooks = true\nmodel = \"gpt-5\"\n")
    assert _codex_hooks_enabled(bare) is False
    off = tmp_path / "off.toml"
    off.write_text("[features]\nhooks = false\n")
    assert _codex_hooks_enabled(off) is False
    assert _codex_hooks_enabled(tmp_path / "missing.toml") is None


def _codex_host(tmp_path, monkeypatch, config_toml_text):
    from reflexes.doctor import _check_hosts
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "hooks.json").write_text('{"hooks":[{"command":"reflexes-hook --host codex"}]}')
    (codex / "config.toml").write_text(config_toml_text)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("HOME", str(tmp_path))  # avoid detecting other real hosts
    hosts = {name: (status, msg) for name, status, msg in _check_hosts()}
    assert "OpenAI Codex" in hosts
    return hosts["OpenAI Codex"]


def test_codex_host_warns_when_feature_disabled(tmp_path, monkeypatch):
    """hooks.json wired + [features] enable flag missing => Codex silently doesn't
    enforce. Doctor must NOT report OK."""
    status, msg = _codex_host(tmp_path, monkeypatch, "hooks = true\n")  # bare (no [features]) = ignored
    assert status == "warn", f"expected warn, got {status}: {msg}"
    assert "features" in msg.lower()


def test_codex_host_warns_on_trust_even_when_configured_and_enabled(tmp_path, monkeypatch):
    """Even fully wired + enabled, the doctor cannot verify Codex hook TRUST, so
    it must warn (not claim enforcing) and point at `/hooks`. (The bug report.)"""
    status, msg = _codex_host(tmp_path, monkeypatch, "[features]\nhooks = true\n")
    assert status == "warn", f"expected warn (trust unverifiable), got {status}: {msg}"
    assert "trust" in msg.lower() and "/hooks" in msg


def test_doctor_reports_disabled_loudly(tmp_path, monkeypatch, capsys):
    """A globally disabled system (`enabled: false`) must be reported LOUDLY and
    must NOT read as healthy/enforcing — the whole point of the doctor."""
    pol = tmp_path / "policies"
    pol.mkdir()
    (pol / "p.md").write_text("# Criteria\n\n## Overview\nx\n")
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "enabled: false\nreflexes:\n  default:\n    - policy: ./policies/p.md\n"
        "      name: p\n      trigger: before_tool_call\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = run_doctor(config=str(cfg))
    out = capsys.readouterr().out
    assert "DISABLED" in out, "doctor must say DISABLED when enabled: false"
    assert "All checks passed" not in out, "disabled must NOT read as healthy"
    assert rc != 0, "disabled must not be a green exit"


def test_check_logging_debug_is_warn(tmp_path):
    """`logging.level: debug` captures message content — the doctor must WARN
    (privacy footgun), not report it as clean."""
    from reflexes.doctor import _check_logging
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text("logging:\n  level: debug\nreflexes:\n  default: []\n")
    status, msg = _check_logging(cfg)
    assert status == "warn"
    assert "content" in msg.lower()


def test_check_logging_default_is_ok_no_content(tmp_path):
    from reflexes.doctor import _check_logging
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text("reflexes:\n  default: []\n")  # no logging block → warning default
    status, msg = _check_logging(cfg)
    assert status == "ok"
    assert "no content" in msg.lower()


def test_check_logging_off_is_ok(tmp_path):
    from reflexes.doctor import _check_logging
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text("logging:\n  level: off\nreflexes:\n  default: []\n")
    status, msg = _check_logging(cfg)
    assert status == "ok"
    assert "disabled" in msg.lower()


def test_run_doctor_exit_codes(tmp_path, monkeypatch, capsys):
    # Broken config must exit 2 even if evaluator/hosts are fine or absent.
    cfg = _write_config(tmp_path, broken=True)
    monkeypatch.setenv("HOME", str(tmp_path))  # no hosts detected
    rc = run_doctor(config=str(cfg))
    assert rc == 2
    out = capsys.readouterr().out
    assert "fail open" in out
