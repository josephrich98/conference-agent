"""AWS Lambda entry point.

Mangum adapts the ASGI FastAPI app to the Lambda event/response contract. The
same ``handler`` serves a Lambda Function URL (payload format v2.0). Locally you
do not need this module — run ``uvicorn conference_agent.web.app:app`` (or ``conference-agent
serve``) instead.

Referenced by the SAM template as ``Handler: conference_agent.web.handler.handler``.
"""

from __future__ import annotations

from mangum import Mangum

from conference_agent.web.app import app

# Lambda containers are ephemeral, so skip ASGI lifespan events.
handler = Mangum(app, lifespan="off")
