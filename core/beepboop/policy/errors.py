"""The two typed exceptions every front door turns into speech or an HTTP error."""

from __future__ import annotations

from typing import TYPE_CHECKING

from beepboop.policy.types import DenyReason, Factor, Risk

if TYPE_CHECKING:
    from beepboop.policy.approvals import ApprovalRecord


class PolicyDenied(Exception):  # noqa: N818 - name fixed by CLAUDE.md §5 and MODULES §5
    """``authorize()`` returned ``deny`` (IDENTITY_AND_TRUST §7.2).

    ``reason`` is one of the eleven ``DenyReason`` strings; ``resource`` is the entity
    UUID that lacked a grant, when that is the cause.
    """

    def __init__(
        self,
        reason: DenyReason,
        risk: Risk = "read",
        *,
        resource: str | None = None,
        tool: str | None = None,
    ) -> None:
        super().__init__(f"policy denied ({reason}) for {tool or 'call'} at {risk}")
        self.reason: DenyReason = reason
        self.risk: Risk = risk
        self.resource = resource
        self.tool = tool


class NeedsConfirmation(Exception):  # noqa: N818 - name fixed by CLAUDE.md §5 and MODULES §5
    """``authorize()`` returned ``confirm``: the registry created an approval row (§8).

    ``nonce`` is the single-use secret that travels only inside the SSE event.
    """

    def __init__(self, approval: ApprovalRecord, factors: tuple[Factor, ...], nonce: str) -> None:
        super().__init__(f"approval {approval.id} needed via {', '.join(factors)}")
        self.approval = approval
        self.factors = factors
        self.nonce = nonce
