"""Re-export shared PPTXSchemaValidator (single source of truth)."""

from __future__ import annotations

import sys
from pathlib import Path

_skills_root = Path(__file__).resolve().parents[4]
if str(_skills_root) not in sys.path:
    sys.path.insert(0, str(_skills_root))

from office.validators._reexport import import_office_validator

PPTXSchemaValidator = import_office_validator("pptx", "PPTXSchemaValidator")

__all__ = ["PPTXSchemaValidator"]
