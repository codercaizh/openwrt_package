"""HTTP request models for the Web control plane.

Keeping transport validation separate from route handlers makes the API
surface easy to review and leaves the build core independent of Pydantic.
The classes are re-exported by :mod:`owrt_builder.web` for compatibility with
existing integrations that import the request models from the application
module.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator


class StrictBody(BaseModel):
    """Reject client supplied build/runtime knobs that the server owns."""

    model_config = {"extra": "forbid"}


class LoginBody(StrictBody):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class SettingsBody(StrictBody):
    """Account and PushPlus updates accepted by the settings page.

    Optional fields are inspected through ``model_fields_set`` so changing a
    username does not accidentally clear an existing notification token.
    """

    username: str | None = Field(default=None, min_length=1)
    current_password: str | None = Field(default=None, min_length=1)
    new_password: str | None = Field(default=None, min_length=1)
    pushplus_token: str | None = Field(default=None, max_length=512)
    clear_pushplus: StrictBool = False


class PackageOptionsBody(StrictBody):
    packages: list[str] = Field(default_factory=list, max_length=512)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("packages")
    @classmethod
    def package_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,180}", value):
                raise ValueError("包名包含非法字符")
        return list(dict.fromkeys(values))

    @field_validator("options")
    @classmethod
    def option_names(cls, values: dict[str, Any]) -> dict[str, Any]:
        if len(values) > 1024:
            raise ValueError("子选项数量过多")
        for key in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,220}", str(key)):
                raise ValueError("子选项名称包含非法字符")
            if not isinstance(values[key], (type(None), bool, int, float, str)):
                raise ValueError("子选项值必须是标量")
            if isinstance(values[key], str) and len(values[key]) > 4096:
                raise ValueError("子选项字符串过长")
        return values


class JobBody(PackageOptionsBody):
    device: str = Field(min_length=1, max_length=80)
    # ``StrictInt`` rejects bools and numeric strings before the dynamic CPU
    # ceiling is checked below.
    parallel_jobs: StrictInt | None = Field(default=None, ge=1)
    reuse_cache: StrictBool = True


class DefaultsBody(PackageOptionsBody):
    pass


class ValidateBody(JobBody):
    pass


__all__ = [
    "DefaultsBody",
    "JobBody",
    "LoginBody",
    "PackageOptionsBody",
    "SettingsBody",
    "StrictBody",
    "ValidateBody",
]
