"""A business's website as the API shows it."""
from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class DomainChangeOut(BaseModel):
    """How often the website address may change on this plan."""

    #: Days that must pass between two changes (``null`` = may change at will).
    every_days: int | None
    #: When the next change is possible (``null`` = now).
    next_allowed_at: dt.datetime | None


class SiteOut(BaseModel):
    site_id: uuid.UUID
    name: str
    #: The website address (host only, without ``www.``); ``null`` for a site that was set up without one.
    domain: str | None
    #: The browsers' origins the publishable key is accepted from. On a plan that limits domains they follow the address; elsewhere ``[]`` means any origin.
    allowed_origins: list[str]
    #: Whether the origins are derived from the address (a plan that limits domains) or a list the owner keeps.
    origins_follow_domain: bool
    domain_changed_at: dt.datetime | None
    domain_change: DomainChangeOut


class SiteIn(BaseModel):
    """Change the website address (owner). On a plan that limits domains ``allowed_origins`` can only name the same domains (www and non-www are one); elsewhere it replaces the list."""

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=300)
    allowed_origins: list[str] | None = Field(default=None, max_length=50)


class SiteSignalOut(BaseModel):
    """For the platform operator: the site, and the address the plugin last reported next to it. A mismatch changes nothing; it is only worth a look."""

    site_id: uuid.UUID
    domain: str | None
    allowed_origins: list[str]
    domain_changed_at: dt.datetime | None
    last_site_url: str | None
    last_site_url_at: dt.datetime | None
    #: ``true``/``false``: the reported address is / is not one of the site's domains (``true`` for the developer's own machine); ``null``: nothing to compare yet.
    site_url_matches_domain: bool | None
