"""Aggregate every SQLAlchemy model into the deployment metadata.

Domain-owned tables live beside their repositories.  Importing this module is
the single supported way for Alembic and test schema creation to discover all
of them without turning :mod:`qq_ai_bot.persistence.models` into a monolith.
"""

# These imports are intentionally side-effectful: defining each mapped class
# registers its table on ``Base.metadata``.
from qq_ai_bot.conversation import db_models as _conversation_db_models  # noqa: F401
from qq_ai_bot.conversation.history import db_models as _history_db_models  # noqa: F401
from qq_ai_bot.emoji import db_models as _emoji_db_models  # noqa: F401
from qq_ai_bot.memory.dream import db_models as _memory_dream_db_models  # noqa: F401
from qq_ai_bot.model_runtime import db_models as _model_runtime_db_models  # noqa: F401
from qq_ai_bot.persistence.models import Base
from qq_ai_bot.plugin_host import db_models as _plugin_db_models  # noqa: F401
from qq_ai_bot.speech import db_models as _speech_db_models  # noqa: F401

__all__ = ["Base"]
