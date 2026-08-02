import pytest

from freqtrade.exceptions import OperationalException
from freqtrade.resolvers.exchange_resolver import ExchangeResolver


def test_product_cannot_resolve_xcoin_exchange(default_conf):
    default_conf["exchange"]["name"] = "xcoin"

    with pytest.raises(OperationalException, match=r"Exchange xcoin is not supported"):
        ExchangeResolver.load_exchange(default_conf, validate=False)
