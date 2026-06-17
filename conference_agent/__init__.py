"""conference_agent — compile conferences into a table and sync to Google Calendar.

The public surface is intentionally small while the project is scaffolded. As
modules are implemented, re-export the key entry points here.
"""

from conference_agent.models import Conference, ConferenceTier, RemoteOption

__all__ = ["Conference", "ConferenceTier", "RemoteOption"]

__version__ = "0.1.0"
