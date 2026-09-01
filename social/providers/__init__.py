"""Provider registry — the one place that maps a provider key to its adapter.

Facebook and Instagram both point at ``meta``: they are two cards in the UI and
one authorization underneath. Nothing outside this package needs to know that.

``X`` and ``YouTube`` are deliberately absent. ``adapter_for`` returns ``None``
for them, which the views turn into "not supported yet" — so their cards render
through the same code path as everything else, and adding an adapter later is
the only change needed to make them work.
"""

from social.models import Provider
from social.providers import linkedin, meta

#: provider key → adapter module. The only mapping in the app.
REGISTRY = {
    Provider.LINKEDIN: linkedin,
    Provider.FACEBOOK: meta,
    Provider.INSTAGRAM: meta,
}

#: Display order for the connect page. Mirrors the trainer panel's card layout.
DISPLAY_ORDER = (
    Provider.LINKEDIN,
    Provider.X,
    Provider.INSTAGRAM,
    Provider.FACEBOOK,
    Provider.YOUTUBE,
)


def adapter_for(provider: str):
    """The adapter for a provider key, or ``None`` if nothing implements it."""
    return REGISTRY.get(provider)


def is_configured(provider: str) -> bool:
    """True when this deployment holds credentials for the network."""
    adapter = adapter_for(provider)
    return bool(adapter and adapter.is_configured())
