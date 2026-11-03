"""Home Assistant integration (HOME_ASSISTANT.md).

The only code in the repo that talks to HA or holds the HA token (hard rule 2:
modules never import this package; they get the policy-wrapped ``ctx.ha``).

M0 contents: :mod:`~beepboop.ha.onboarding` (headless onboarding, V2) and
:mod:`~beepboop.ha.ws` (the minimal WebSocket session it needs). The full
``HAConnection``, state cache and registry index arrive in v0.1.
"""
