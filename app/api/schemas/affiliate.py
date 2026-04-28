from __future__ import annotations

from pydantic import BaseModel


class AffiliateResolveResponse(BaseModel):
    target_url: str
    is_affiliate: bool
