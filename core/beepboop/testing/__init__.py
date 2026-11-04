"""``beepboop.testing``: the fake ``ToolContext`` and the evals runner (MODULES §12).

::

    from beepboop.testing import fake_context
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().track(ctx)
    assert ctx.effects == []
"""

from beepboop.testing.context import FakeToolContext, fake_context, profile_fixture
from beepboop.testing.evals import (
    CaseResult,
    EvalsReport,
    effects_subset,
    policy_state_for,
    run_evals,
    run_evals_async,
    subset_match,
    value_matches,
)
from beepboop.testing.evals_model import (
    DEFAULT_PROFILES,
    EvalCase,
    EvalsError,
    EvalsFile,
    Expect,
    Fixtures,
    load_evals,
    parse_evals,
)
from beepboop.testing.fakes import (
    AllowAllGrants,
    Effect,
    FakeAnnounce,
    FakeHA,
    FakeHttp,
    FakeKV,
    FakeResponse,
    FakeSecrets,
    HAStateFixture,
    HttpFixture,
)

__all__ = [
    "DEFAULT_PROFILES",
    "AllowAllGrants",
    "CaseResult",
    "Effect",
    "EvalCase",
    "EvalsError",
    "EvalsFile",
    "EvalsReport",
    "Expect",
    "FakeAnnounce",
    "FakeHA",
    "FakeHttp",
    "FakeKV",
    "FakeResponse",
    "FakeSecrets",
    "FakeToolContext",
    "Fixtures",
    "HAStateFixture",
    "HttpFixture",
    "effects_subset",
    "fake_context",
    "load_evals",
    "parse_evals",
    "policy_state_for",
    "profile_fixture",
    "run_evals",
    "run_evals_async",
    "subset_match",
    "value_matches",
]
