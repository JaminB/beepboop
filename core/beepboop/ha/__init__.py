"""Home Assistant integration (HOME_ASSISTANT.md).

The only code in the repo that talks to HA or holds the HA token (hard rule 2:
modules never import this package; they get the policy-wrapped ``ctx.ha``).

:mod:`~beepboop.ha.onboarding` and :mod:`~beepboop.ha.ws` (M0, headless onboarding);
:mod:`~beepboop.ha.service` (``HAService``) owns :mod:`~beepboop.ha.client`
(``HAConnection``), :mod:`~beepboop.ha.cache`, :mod:`~beepboop.ha.index`,
:mod:`~beepboop.ha.inbox`, :mod:`~beepboop.ha.plan` and :mod:`~beepboop.ha.wrapper`.
"""
