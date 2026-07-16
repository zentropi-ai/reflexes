"""Public API — the with_reflexes() and middleware_from_config() entry points."""

from __future__ import annotations

from pathlib import Path

from reflexes.config import load_config, configure_from_file, ReflexBinding


def _detect_framework(agent):
    """Detect the agent framework from the agent object's type."""
    type_name = type(agent).__module__ + "." + type(agent).__qualname__

    # Check most specific first to avoid false matches
    if "google.adk" in type_name or "google_adk" in type_name:
        return "adk"
    if "smolagents" in type_name:
        return "smolagents"
    if "pydantic_ai" in type_name:
        return "pydantic_ai"
    if "agents." in type_name or "openai" in type_name:
        return "openai"

    raise TypeError(
        f"Could not detect agent framework from type {type_name}. "
        f"Supported: OpenAI Agents SDK, Google ADK, PydanticAI, Smolagents, LangGraph."
    )


def with_reflexes(
    agent,
    reflexes: list[str] | None = None,
    *,
    config_file: str | Path = "reflexes.yaml",
):
    """Apply reflexes to an agent.

    Loads the named reflex groups from the config file (plus 'default'
    automatically), detects the agent framework, and wires the policies
    into the framework's native hook mechanism.

    Supported frameworks:
      - OpenAI Agents SDK (Agent)
      - Google ADK (LlmAgent)
      - PydanticAI (Agent)
      - Smolagents (ToolCallingAgent, CodeAgent)

    For LangGraph, use middleware_from_config() instead.
    For Claude Agent SDK, use create_hooks() from
    reflexes.integrations.claude_agent_sdk instead.

    Args:
        agent: An agent instance from a supported framework.
        reflexes: List of config group names from reflexes.yaml.
                  If None, only 'default' is applied.
        config_file: Path to reflexes.yaml. Defaults to ./reflexes.yaml.

    Returns:
        The agent, with reflexes wired in.
    """
    configure_from_file(config_file)

    reflexes_list = reflexes or []
    framework = _detect_framework(agent)

    # Adapters that take pre-loaded bindings
    if framework in ("openai", "adk"):
        bindings = load_config(reflexes=reflexes_list, config_file=config_file)
        if framework == "openai":
            from reflexes.integrations.openai_agents import apply_reflexes
            return apply_reflexes(agent, bindings)
        else:
            from reflexes.integrations.google_adk import apply_reflexes as apply_adk
            return apply_adk(agent, bindings)

    # Adapters that handle their own config loading
    if framework == "pydantic_ai":
        from reflexes.integrations.pydantic_ai import apply_reflexes as apply_pydantic
        return apply_pydantic(agent, reflexes=reflexes_list, config_file=config_file)
    elif framework == "smolagents":
        from reflexes.integrations.smolagents import apply_reflexes as apply_smol
        return apply_smol(agent, reflexes=reflexes_list, config_file=config_file)

    raise ValueError(f"Unsupported framework: {framework}")


def middleware_from_config(
    reflexes: list[str] | None = None,
    *,
    config_file: str | Path = "reflexes.yaml",
):
    """Create a LangGraph/LangChain middleware from reflexes config.

    Returns an AgentMiddleware instance for use with create_agent():

        from langchain.agents import create_agent
        from reflexes import middleware_from_config

        agent = create_agent(
            model="openai:gpt-4o",
            tools=[...],
            middleware=[middleware_from_config(reflexes=["safety"])],
        )

    Args:
        reflexes: List of config group names from reflexes.yaml.
                  If None, only 'default' is applied.
        config_file: Path to reflexes.yaml. Defaults to ./reflexes.yaml.

    Returns:
        An AgentMiddleware instance.
    """
    configure_from_file(config_file)

    reflexes = reflexes or []
    bindings = load_config(reflexes=reflexes, config_file=config_file)

    from reflexes.integrations.langgraph import create_reflex_middleware
    return create_reflex_middleware(bindings)
