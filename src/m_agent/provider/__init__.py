"""Removed 0.2 provider namespace."""

raise ImportError(
    "m_agent.provider was removed in 0.3; import m_agent.adapters.provider "
    "instead (see docs/migrating-to-0.3.md)"
)
