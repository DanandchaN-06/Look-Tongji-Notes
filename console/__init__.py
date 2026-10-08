"""Local graphical console for Look Tongji Notes.

This package adds a local-only web console on top of the existing
`scripts/look_tongji.py` CLI. It never replaces the CLI: every action either
shells out to the CLI, or prepares an instruction for the agent.

Nothing here talks to the network except the CLI itself.
"""

__all__ = ["__version__"]

__version__ = "0.4.4"
