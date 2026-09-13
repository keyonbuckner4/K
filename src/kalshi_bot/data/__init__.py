"""External data feeds for the model strategies.

* ``nws``: National Weather Service grid forecasts for the weather strategy.
* ``crypto_feed``: Kraken spot and realized volatility (or Deribit DVOL) for the crypto strategy.

Every feed raises ``DataUnavailable`` when it cannot get real data. Nothing here ever substitutes
a placeholder value (CLAUDE.md: fail loudly instead).
"""
