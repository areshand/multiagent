from __future__ import annotations

try:
    from .swe_prod_evidence import *  # noqa: F403
except ImportError:  # pragma: no cover - direct execution in task containers
    from swe_prod_evidence import *  # type: ignore  # noqa: F403

try:
    from .swe_prod_validation import *  # noqa: F403
except ImportError:  # pragma: no cover - direct execution in task containers
    from swe_prod_validation import *  # type: ignore  # noqa: F403
