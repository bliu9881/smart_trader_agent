"""
Smart money data providers.

Each provider implements the DataProvider interface and fetches trade data
from a specific external source.

Providers are re-exported best-effort: not every provider module ships in every
build (some are optional/experimental). A missing module must NOT break the whole
package — importing any single provider submodule triggers this __init__, so a
hard import failure here would take down the entire scanner. Missing providers
are simply omitted from the namespace; callers gate on config toggles anyway.
"""
import logging as _logging

_logger = _logging.getLogger(__name__)

_OPTIONAL_PROVIDERS = {
    "CapitolTradesProvider": "capitol_trades",
    "SECEdgarProvider": "sec_edgar",
    "BerkshireProvider": "berkshire_13f",
    "ARKProvider": "ark_invest",
    "InsiderClusterProvider": "insider_cluster",
}

__all__ = []

for _cls_name, _module in _OPTIONAL_PROVIDERS.items():
    try:
        _mod = __import__(
            f"smart_trader.core.smart_money_providers.{_module}",
            fromlist=[_cls_name],
        )
        globals()[_cls_name] = getattr(_mod, _cls_name)
        __all__.append(_cls_name)
    except Exception as _e:  # ImportError or anything at module import time
        _logger.debug("smart_money_providers: %s unavailable (%s)", _module, _e)
