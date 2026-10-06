"""ReplyDesk: async email auto-reply with sentiment + product detection.

Public surface is intentionally tiny: import ``run_once`` / ``run_forever``
from ``replydesk.main`` to drive the system, or call ``decide`` /
``analyze`` / ``reply`` directly to embed in a larger pipeline.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
