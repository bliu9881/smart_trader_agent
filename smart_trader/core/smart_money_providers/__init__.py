"""
Smart money data providers.

Each provider implements the DataProvider interface and fetches trade data
from a specific external source.
"""
from smart_trader.core.smart_money_providers.capitol_trades import CapitolTradesProvider
from smart_trader.core.smart_money_providers.sec_edgar import SECEdgarProvider
from smart_trader.core.smart_money_providers.berkshire_13f import BerkshireProvider
from smart_trader.core.smart_money_providers.ark_invest import ARKProvider
from smart_trader.core.smart_money_providers.insider_cluster import InsiderClusterProvider

__all__ = [
    "CapitolTradesProvider",
    "SECEdgarProvider",
    "BerkshireProvider",
    "ARKProvider",
    "InsiderClusterProvider",
]
