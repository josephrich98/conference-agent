"""Allow ``python -m conference_agent`` as an alias for the ``conference-agent`` CLI."""

import sys

from conference_agent.cli import main

sys.exit(main())
