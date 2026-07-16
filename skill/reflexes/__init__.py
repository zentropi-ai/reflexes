"""Reflexes — System 1 for AI agents."""

# Version comes from the bundled _version.py (works for the on-disk bundled
# runtime, which has no package metadata) and falls back to dist metadata.
try:
    from reflexes._version import __version__
except Exception:  # pragma: no cover
    try:
        from importlib.metadata import version as _pkg_version
        __version__ = _pkg_version("reflexes")
    except Exception:
        __version__ = "0.0.0"

from reflexes.core import check, acheck, configure, Verdict, ReflexDenied, build_feedback
from reflexes.document import load_policy, resolve_policy, parse, Policy
from reflexes.policy_ref import PolicyRef, parse_ref
from reflexes.policy_sources import PolicyNotFound
from reflexes.config import load_config, configure_from_file, ReflexBinding
from reflexes.api import with_reflexes, middleware_from_config

__all__ = [
    "check", "acheck", "configure", "Verdict", "ReflexDenied",
    "load_policy", "resolve_policy", "parse", "Policy",
    "PolicyRef", "parse_ref", "PolicyNotFound",
    "load_config", "configure_from_file", "ReflexBinding",
    "with_reflexes", "middleware_from_config", "__version__",
]
