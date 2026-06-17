"""AWS Lambda entry point.

Mangum adapts the ASGI FastAPI app to the Lambda event/response contract. The
same ``handler`` serves a Lambda Function URL (payload format v2.0). Locally you
do not need this module — run ``uvicorn web.app:app`` (or ``conference-agent
serve``) instead.

Referenced by the SAM template as ``Handler: web.handler.handler``.
"""

from __future__ import annotations

from mangum import Mangum

from web.app import app

# Lambda containers are ephemeral, so skip ASGI lifespan events.
handler = Mangum(app, lifespan="off")
