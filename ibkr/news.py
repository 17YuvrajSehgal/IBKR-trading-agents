"""
News data access for Interactive Brokers TWS/Gateway.

This module provides an async interface for fetching historical headlines,
retrieving full article bodies, and subscribing to live news headlines.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ib_async import Contract, IB, NewsProvider, NewsTick, Ticker

from ibkr.exceptions import IBKRConnectionError, IBKRDataError
from ibkr.utils import create_stock_contract

logger = logging.getLogger(__name__)


@dataclass
class NewsHeadline:
    """
    Normalized headline record from IBKR news APIs.

    Attributes:
        time: Headline timestamp (UTC)
        provider_code: News provider identifier, e.g. "BZ", "DJNL"
        article_id: Unique article ID from provider
        headline: Headline text
        source: "historical" or "live"
        symbol: Optional resolved symbol
        con_id: Optional resolved IB contract id
        extra_data: Optional provider metadata from live stream
    """

    time: datetime
    provider_code: str
    article_id: str
    headline: str
    source: str
    symbol: str | None = None
    con_id: int | None = None
    extra_data: str = ""


@dataclass
class NewsArticle:
    """
    Full article body fetched from IBKR by provider/article id.

    Attributes:
        provider_code: News provider code
        article_id: Provider article id
        article_type: Provider format type
        body: Raw article content
    """

    provider_code: str
    article_id: str
    article_type: int
    body: str

    @property
    def is_text(self) -> bool:
        """True when IB returns plain text payload."""
        return self.article_type == 0


HeadlineCallback = Callable[[NewsHeadline], None]


class NewsManager:
    """
    Fetches and streams IBKR news through a connected ``ib_async.IB`` session.

    Typical workflow:
      1. fetch_historical_headlines("AAPL")
      2. fetch_article(provider_code, article_id) for selected headlines
      3. subscribe_symbol_headlines("AAPL") to receive live ticks
    """

    _CONID_PATTERN = re.compile(r"(?:conid|con_id|conId)\s*[:=]\s*(\d+)")
    _SYMBOL_PATTERN = re.compile(r"(?:symbol|sym)\s*[:=]\s*([A-Za-z0-9._-]+)")

    def __init__(self, ib: IB) -> None:
        """
        Initialize news manager.

        Args:
            ib: Connected IB instance (from IBKRConnection.ib)

        Raises:
            IBKRConnectionError: If IB instance is not connected
        """
        if not ib.isConnected():
            raise IBKRConnectionError("IB instance is not connected")

        self._ib = ib
        self._callbacks: list[HeadlineCallback] = []
        self._live_headlines: list[NewsHeadline] = []
        self._seen_live_keys: set[tuple[str, str]] = set()

        # Live subscriptions by symbol
        self._live_contracts: dict[str, Contract] = {}
        self._live_tickers: dict[str, Ticker] = {}
        self._con_id_to_symbol: dict[int, str] = {}

        self._ib.tickNewsEvent += self._on_tick_news
        logger.info("NewsManager initialized")

    async def get_news_providers(self) -> list[NewsProvider]:
        """
        Fetch all available news providers for this account/session.

        Returns:
            List of NewsProvider objects
        """
        providers = await self._ib.reqNewsProvidersAsync()
        if not providers:
            logger.warning("No news providers returned by IBKR")
            return []
        return providers

    async def fetch_historical_headlines(
        self,
        symbol: str,
        *,
        exchange: str = "SMART",
        currency: str = "USD",
        provider_codes: Optional[list[str]] = None,
        lookback_hours: int = 24,
        total_results: int = 50,
    ) -> list[NewsHeadline]:
        """
        Fetch historical headlines for a symbol.

        Args:
            symbol: Ticker symbol, e.g. "AAPL"
            exchange: Exchange route (default SMART)
            currency: Currency (default USD)
            provider_codes: Optional provider code filter (e.g. ["BZ", "DJNL"])
            lookback_hours: Window size for lookback
            total_results: Max headline count (1-300)

        Returns:
            List of NewsHeadline records sorted by publish time (newest first)

        Raises:
            IBKRDataError: If request fails or params are invalid
        """
        if lookback_hours <= 0:
            raise IBKRDataError(f"lookback_hours must be > 0, got {lookback_hours}")
        if total_results < 1 or total_results > 300:
            raise IBKRDataError(
                f"total_results must be in [1, 300], got {total_results}"
            )

        contract = await self._qualify_stock_contract(symbol, exchange, currency)
        provider_string = await self._resolve_provider_codes(provider_codes)

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=lookback_hours)

        logger.info(
            f"Requesting historical news for {symbol.upper()} "
            f"(conId={contract.conId}, providers={provider_string}, "
            f"lookback_hours={lookback_hours}, total_results={total_results})"
        )

        raw_headlines = await self._ib.reqHistoricalNewsAsync(
            conId=contract.conId,
            providerCodes=provider_string,
            startDateTime=start_dt,
            endDateTime=end_dt,
            totalResults=total_results,
            historicalNewsOptions=[],
        )

        if raw_headlines is None:
            return []

        # reqHistoricalNewsAsync currently returns a list in ib_async.
        items = raw_headlines if isinstance(raw_headlines, list) else [raw_headlines]

        headlines: list[NewsHeadline] = []
        for item in items:
            published = item.time
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)

            headlines.append(
                NewsHeadline(
                    time=published.astimezone(timezone.utc),
                    provider_code=item.providerCode,
                    article_id=item.articleId,
                    headline=item.headline,
                    source="historical",
                    symbol=symbol.upper(),
                    con_id=contract.conId,
                )
            )

        headlines.sort(key=lambda x: x.time, reverse=True)
        return headlines

    async def fetch_article(self, provider_code: str, article_id: str) -> NewsArticle:
        """
        Fetch full article body by provider/article id.

        Args:
            provider_code: Provider code, e.g. "BZ"
            article_id: Provider article id from headline stream

        Returns:
            NewsArticle payload

        Raises:
            IBKRDataError: If article retrieval fails
        """
        if not provider_code or not article_id:
            raise IBKRDataError("provider_code and article_id are required")

        try:
            article = await self._ib.reqNewsArticleAsync(
                providerCode=provider_code,
                articleId=article_id,
                newsArticleOptions=[],
            )
        except Exception as exc:
            raise IBKRDataError(
                f"Failed to fetch article {provider_code}:{article_id}: {exc}"
            ) from exc

        return NewsArticle(
            provider_code=provider_code,
            article_id=article_id,
            article_type=article.articleType,
            body=article.articleText,
        )

    async def subscribe_symbol_headlines(
        self,
        symbol: str,
        *,
        exchange: str = "SMART",
        currency: str = "USD",
        provider_codes: Optional[list[str]] = None,
    ) -> None:
        """
        Start live headline stream for a symbol.

        Uses ``reqMktData(..., genericTickList="mdoff,292")`` as required by IBKR.

        Args:
            symbol: Ticker to subscribe
            exchange: Exchange route
            currency: Currency
            provider_codes: Optional list of provider codes
        """
        norm_symbol = symbol.upper()
        if norm_symbol in self._live_contracts:
            logger.debug(f"Already subscribed to live news: {norm_symbol}")
            return

        contract = await self._qualify_stock_contract(norm_symbol, exchange, currency)
        provider_string = await self._resolve_provider_codes(provider_codes)

        tick_list = "mdoff,292"
        if provider_string:
            tick_list = f"{tick_list}:{provider_string}"

        ticker = self._ib.reqMktData(
            contract,
            genericTickList=tick_list,
            snapshot=False,
            regulatorySnapshot=False,
        )

        self._live_contracts[norm_symbol] = contract
        self._live_tickers[norm_symbol] = ticker
        self._con_id_to_symbol[contract.conId] = norm_symbol

        logger.info(
            f"Subscribed to live news headlines for {norm_symbol} "
            f"(conId={contract.conId}, ticks='{tick_list}')"
        )

    def unsubscribe_symbol_headlines(self, symbol: str) -> None:
        """
        Cancel live headline stream for one symbol.

        Args:
            symbol: Ticker to unsubscribe
        """
        norm_symbol = symbol.upper()
        contract = self._live_contracts.get(norm_symbol)
        if contract is None:
            logger.warning(f"No active live news subscription for {norm_symbol}")
            return

        try:
            self._ib.cancelMktData(contract)
        finally:
            self._live_contracts.pop(norm_symbol, None)
            self._live_tickers.pop(norm_symbol, None)
            if contract.conId in self._con_id_to_symbol:
                self._con_id_to_symbol.pop(contract.conId, None)

        logger.info(f"Unsubscribed live news headlines for {norm_symbol}")

    def unsubscribe_all(self) -> None:
        """Cancel all live headline subscriptions."""
        for symbol in list(self._live_contracts.keys()):
            self.unsubscribe_symbol_headlines(symbol)

    def subscribe_headline_updates(self, callback: HeadlineCallback) -> None:
        """
        Register callback for each incoming live headline.

        Args:
            callback: Callable that receives ``NewsHeadline``
        """
        self._callbacks.append(callback)

    def unsubscribe_headline_updates(self, callback: HeadlineCallback) -> None:
        """Remove previously registered live headline callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def get_live_headlines(self, limit: Optional[int] = None) -> list[NewsHeadline]:
        """
        Return cached live headlines.

        Args:
            limit: Optional max rows from most-recent backward
        """
        if limit is None or limit <= 0:
            return list(self._live_headlines)
        return self._live_headlines[-limit:]

    async def _resolve_provider_codes(self, provider_codes: Optional[list[str]]) -> str:
        """
        Resolve and validate provider list against session providers.

        Returns a '+' separated string for IB API requests.
        """
        providers = await self.get_news_providers()
        available = {p.code for p in providers}

        if not available:
            raise IBKRDataError(
                "No API news providers are available. "
                "Verify your IBKR API news subscriptions."
            )

        if provider_codes:
            requested = [code.strip() for code in provider_codes if code.strip()]
            missing = [code for code in requested if code not in available]
            if missing:
                raise IBKRDataError(
                    f"Requested provider codes not available: {missing}. "
                    f"Available providers: {sorted(available)}"
                )
            return "+".join(requested)

        return "+".join(sorted(available))

    async def _qualify_stock_contract(
        self,
        symbol: str,
        exchange: str,
        currency: str,
    ) -> Contract:
        """Create and qualify stock contract, returning resolved contract."""
        contract = create_stock_contract(symbol=symbol, exchange=exchange, currency=currency)
        qualified = await self._ib.qualifyContractsAsync(contract)

        if not qualified or qualified[0] is None:
            raise IBKRDataError(f"Failed to qualify contract for symbol {symbol}")

        return contract

    def _on_tick_news(self, tick: NewsTick) -> None:
        """
        IBKR live headline event handler.

        Note: ib_async's event payload does not include reqId, so symbol
        resolution uses ``extraData`` + known conId mappings when present.
        """
        symbol = None
        con_id = None

        if tick.extraData:
            match = self._CONID_PATTERN.search(tick.extraData)
            if match:
                try:
                    con_id = int(match.group(1))
                    symbol = self._con_id_to_symbol.get(con_id)
                except ValueError:
                    con_id = None

            if symbol is None:
                smatch = self._SYMBOL_PATTERN.search(tick.extraData)
                if smatch:
                    symbol = smatch.group(1).upper()

        # Fallback: if only one symbol is subscribed, attribute headlines to it.
        if symbol is None and len(self._live_contracts) == 1:
            symbol = next(iter(self._live_contracts.keys()))
            only_contract = self._live_contracts[symbol]
            con_id = only_contract.conId

        ts = tick.timeStamp
        if ts > 10_000_000_000:  # Some sources send epoch milliseconds
            ts = ts / 1000

        published = datetime.fromtimestamp(ts, tz=timezone.utc)
        headline = NewsHeadline(
            time=published,
            provider_code=tick.providerCode,
            article_id=tick.articleId,
            headline=tick.headline,
            source="live",
            symbol=symbol,
            con_id=con_id,
            extra_data=tick.extraData or "",
        )

        key = (headline.provider_code, headline.article_id)
        if key in self._seen_live_keys:
            return

        self._seen_live_keys.add(key)
        self._live_headlines.append(headline)

        for callback in self._callbacks:
            try:
                callback(headline)
            except Exception as exc:
                logger.error(f"News callback failed: {exc}")

