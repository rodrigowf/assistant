"""The accounts data model: one ``AccountService`` per thing Archie signs in to.

A service reports a :class:`ServiceStatus` (signed in?, how, as whom, until when) and lists its
sign-in :class:`Method`s. Method kinds are generic so every client renders them the same way:

``link``         the backend runs the CLI's own login, hands the client the URL (and a device
                 code) to open on any device, and — when the flow wants it — takes a pasted-back
                 code (:mod:`.flows`)
``credentials``  paste a credentials file (JSON) copied from another machine — validated, written
                 atomically with mode 0600 and a backup of the previous file — or a secret handed
                 to the CLI's own login (``codex login --with-api-key``)
``env``          one or more keys in ``context/.env`` (API keys, tokens, mode switches), set
                 through the env manager (:mod:`.envfile`)
``signout``      the CLI's logout (or moving its credentials file aside)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .flows import FlowSpec

Group = Literal["harness", "api", "other"]
State = Literal["signed_in", "signed_out", "expired", "unavailable", "unknown"]
MethodKind = Literal["link", "credentials", "env", "signout"]


class AccountError(Exception):
    """A request the service can't satisfy; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class EnvField:
    """One ``context/.env`` key a method sets."""

    name: str
    label: str
    secret: bool = True
    help: str = ""
    placeholder: str = ""
    # For mode switches (e.g. ARCHIE_GEMINI_AUTH_TYPE): the allowed values; "" = unset.
    choices: list[dict[str, str]] | None = None
    # Filled in by the registry from the env file.
    set: bool = False
    preview: str = ""
    value: str | None = None  # only for non-secret choice fields


@dataclass
class Method:
    id: str
    kind: MethodKind
    label: str
    description: str = ""
    recommended: bool = False
    active: bool = False  # this is how the service is signed in right now
    available: bool = True
    unavailable_reason: str = ""
    # link
    needs_code: bool = False
    code_label: str = ""
    code_help: str = ""
    # credentials: "json" (a file's contents, multi-line) or "secret" (one masked line, e.g. an
    # API key handed to the CLI's own login)
    input: Literal["json", "secret"] = "json"
    path: str = ""  # where the server stores it
    source_hint: str = ""  # where to copy it from on a signed-in machine
    placeholder: str = ""
    warning: str = ""
    # env
    fields: list[EnvField] = field(default_factory=list)


@dataclass
class ServiceStatus:
    id: str
    label: str
    group: Group
    description: str
    state: State = "unknown"
    method: str | None = None  # human label of the active credential ("Claude Max subscription")
    account: str | None = None  # email / org when known
    plan: str | None = None
    expires_at: str | None = None  # ISO-8601 UTC
    detail: str | None = None  # one extra line (where the credential lives, last refresh, …)
    warnings: list[str] = field(default_factory=list)
    methods: list[Method] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)  # what in Archie depends on it
    docs: str | None = None
    flow: dict[str, Any] | None = None  # the active / last sign-in flow (filled by the registry)
    verified: dict[str, Any] | None = None  # result of an explicit "Test" (API keys)
    can_verify: bool = False  # the service implements verify() (filled by the registry)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AccountService:
    """Base class: override what the service supports."""

    id: str = ""
    label: str = ""
    group: Group = "other"
    description: str = ""

    def new_status(self, **kw: Any) -> ServiceStatus:
        return ServiceStatus(id=self.id, label=self.label, group=self.group, description=self.description, **kw)

    async def status(self) -> ServiceStatus:
        raise NotImplementedError

    def flow_spec(self, method: str) -> "FlowSpec":
        raise AccountError(f"{self.label} has no sign-in link method {method!r}.", 404)

    async def save_credentials(self, method: str, content: str) -> str:
        """Validate and store a pasted credentials file; returns a short confirmation."""
        raise AccountError(f"{self.label} doesn't take a credentials file.", 404)

    async def sign_out(self) -> str:
        raise AccountError(f"{self.label} has no sign-out.", 404)

    async def verify(self) -> dict[str, Any]:
        """Test the credential against the provider (cheap, free endpoint). ``{ok, message}``."""
        raise AccountError(f"{self.label} can't be tested from here.", 404)
