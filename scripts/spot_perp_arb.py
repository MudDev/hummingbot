from decimal import Decimal
from enum import Enum
from typing import Dict, List

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    PositionModeChangeEvent,
    SellOrderCompletedEvent,
)
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class StrategyState(Enum):
    Closed = 0  # static state
    Opening = 1  # in flight state
    Opened = 2  # static state
    Closing = 3  # in flight state


# TODO: handle corner cases -- spot price and perp price never cross again after position is opened
class SpotPerpArb(ScriptStrategyBase):
    """
    PRECHECK:
    1. enough base and quote balance in spot (base is optional if you do one side only), enough quote balance in perp
    2. better to empty your position in perp
    3. check you have set one way mode (instead of hedge mode) in your futures account

    REFERENCE: hummingbot/strategy/spot_perpetual_arbitrage
    """

    spot_connector = "kucoin"
    perp_connector = "kucoin_perpetual"
    trading_pair = "LINA-USDT"
    markets = {spot_connector: {trading_pair}, perp_connector: {trading_pair}}

    leverage = 2
    is_position_mode_ready = False

    base_order_amount = Decimal("100")
    buy_spot_short_perp_profit_margin_bps = 20
    sell_spot_long_perp_profit_margin_bps = 20
    # buffer to account for slippage when placing limit taker orders
    slippage_buffer_bps = 5

    strategy_state = StrategyState.Closed
    completed_order_ids = []
    # last_arbitrage_reported_ts = 0
    next_arbitrage_opening_ts = 0
    next_arbitrage_opening_delay = 5
    in_flight_state_start_ts = 0
    in_flight_state_tolerance = 60
    opened_state_start_ts = 0
    opened_state_tolerance = 60 * 60 * 2

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.set_leverage()

    def set_leverage(self) -> None:
        perp_connector = self.connectors[self.perp_connector]
        perp_connector.set_position_mode(PositionMode.ONEWAY)
        perp_connector.set_leverage(
            trading_pair=self.trading_pair, leverage=self.leverage
        )
        self.logger().info(
            f"Setting leverage to {self.leverage}x for {self.perp_connector} on {self.trading_pair}"
        )

    def on_tick(self) -> None:
        # precheck before running any trading logic
        if not self.is_position_mode_ready:
            return

        # skip if orders are pending for completion
        self.update_in_flight_state()
        if self.strategy_state in (StrategyState.Opening, StrategyState.Closing):
            if (
                self.current_timestamp
                > self.in_flight_state_start_ts + self.in_flight_state_tolerance
            ):
                self.logger().warning(
                    f"Orders has been submitted but not completed yet "
                    f"for more than {self.in_flight_state_tolerance} seconds. Please check your orders!"
                )
            return

        # skip if its still in buffer time before next arbitrage opportunity
        if (
            self.strategy_state == StrategyState.Closed
            and self.current_timestamp < self.next_arbitrage_opening_ts
        ):
            return

        # flag out if position waits too long without any sign of closing
        if (
            self.strategy_state == StrategyState.Opened
            and self.current_timestamp
            > self.opened_state_start_ts + self.opened_state_tolerance
        ):
            self.logger().warning(
                f"Position has been opened for more than {self.opened_state_tolerance} seconds without any sign of closing. "
                f"Consider undoing the position manually or lower the profitability margin."
            )

        # TODO: change to async on order execution
        # find opportunity and trade
        if self.should_buy_spot_short_perp() and self.can_buy_spot_short_perp():
            self.buy_spot_short_perp()
            self.update_static_state()
        elif self.should_sell_spot_long_perp() and self.can_sell_spot_long_perp():
            self.sell_spot_long_perp()
            self.update_static_state()

    def update_in_flight_state(self) -> None:
        if (
            self.strategy_state == StrategyState.Opening
            and len(self.completed_order_ids) == 2
        ):
            self.strategy_state = StrategyState.Opened
            self.logger().info(
                f"Position is opened with order_ids: {self.completed_order_ids}. "
                "Changed the state from Opening to Opened."
            )
            self.completed_order_ids.clear()
            self.opened_state_start_ts = self.current_timestamp
        elif (
            self.strategy_state == StrategyState.Closing
            and len(self.completed_order_ids) == 2
        ):
            self.strategy_state = StrategyState.Closed
            self.next_arbitrage_opening_ts = (
                self.current_timestamp + self.next_arbitrage_opening_ts
            )
            self.logger().info(
                f"Position is closed with order_ids: {self.completed_order_ids}. "
                f"Changed the state from Closing to Closed.\n"
                f"No arbitrage opportunity will be opened after {self.next_arbitrage_opening_ts}"
            )
            self.completed_order_ids.clear()
        return

    def update_static_state(self) -> None:
        if self.strategy_state == StrategyState.Closed:
            self.strategy_state = StrategyState.Opening
            self.logger().info("The state changed from Closed to Opening")
        elif self.strategy_state == StrategyState.Opened:
            self.strategy_state = StrategyState.Closing
            self.logger().info("The state changed from Opened to Closing")
        self.in_flight_state_start_ts = self.current_timestamp
        return

    def should_buy_spot_short_perp(self) -> bool:
        spot_buy_price = self.limit_taker_price(self.spot_connector, is_buy=True)
        perp_sell_price = self.limit_taker_price(self.perp_connector, is_buy=False)
        ret_pbs = float((perp_sell_price - spot_buy_price) / spot_buy_price) * 10000
        is_profitable = ret_pbs >= self.buy_spot_short_perp_profit_margin_bps
        return is_profitable

    # TODO: check if balance is deducted when it has position
    def can_buy_spot_short_perp(self) -> bool:
        spot_balance = self.get_balance(self.spot_connector, is_base=False)
        buy_price_with_slippage = self.limit_taker_price_with_slippage(
            self.spot_connector, is_buy=True
        )
        spot_required = buy_price_with_slippage * self.base_order_amount
        is_spot_enough = Decimal(spot_balance) >= spot_required
        if not is_spot_enough:
            _, quote = split_hb_trading_pair(self.trading_pair)
            float_spot_required = float(spot_required)
            self.logger().info(
                f"Insufficient balance in {self.spot_connector}: {spot_balance} {quote}. "
                f"Required {float_spot_required:.4f} {quote}."
            )
        perp_balance = self.get_balance(self.perp_connector, is_base=False)
        # short order WITHOUT any splippage takes more capital
        short_price = self.limit_taker_price(self.perp_connector, is_buy=False)
        perp_required = short_price * self.base_order_amount
        is_perp_enough = Decimal(perp_balance) >= perp_required
        if not is_perp_enough:
            _, quote = split_hb_trading_pair(self.trading_pair)
            float_perp_required = float(perp_required)
            self.logger().info(
                f"Insufficient balance in {self.perp_connector}: {perp_balance:.4f} {quote}. "
                f"Required {float_perp_required:.4f} {quote}."
            )
        return is_spot_enough and is_perp_enough

    # TODO: use OrderCandidate and check for budget
    def buy_spot_short_perp(self) -> None:
        spot_buy_price_with_slippage = self.limit_taker_price_with_slippage(
            self.spot_connector, is_buy=True
        )
        perp_short_price_with_slippage = self.limit_taker_price_with_slippage(
            self.perp_connector, is_buy=False
        )
        self.buy(
            self.spot_connector,
            self.trading_pair,
            amount=self.base_order_amount,
            order_type=OrderType.LIMIT,
            price=spot_buy_price_with_slippage,
        )
        self.logger().info(
            f"Submitted buy order in {self.spot_connector} for {self.trading_pair} "
            f"at price {spot_buy_price_with_slippage:.06f}@{self.base_order_amount}"
        )
        position_action = (
            PositionAction.CLOSE if self.is_perp_in_long else PositionAction.OPEN
        )
        self.sell(
            self.perp_connector,
            self.trading_pair,
            amount=self.base_order_amount,
            order_type=OrderType.LIMIT,
            price=perp_short_price_with_slippage,
            position_action=position_action,
        )
        self.logger().info(
            f"Submitted short order in {self.perp_connector} for {self.trading_pair} "
            f"at price {perp_short_price_with_slippage:.06f}@{self.base_order_amount}"
        )

        self.last_submit_order_ts = self.current_timestamp
        return

    def should_sell_spot_long_perp(self) -> bool:
        spot_sell_price = self.limit_taker_price(self.spot_connector, is_buy=False)
        perp_buy_price = self.limit_taker_price(self.perp_connector, is_buy=True)
        ret_pbs = float((spot_sell_price - perp_buy_price) / perp_buy_price) * 10000
        is_profitable = ret_pbs >= self.sell_spot_long_perp_profit_margin_bps
        return is_profitable

    def can_sell_spot_long_perp(self) -> bool:
        spot_balance = self.get_balance(self.spot_connector, is_base=True)
        spot_required = self.base_order_amount
        is_spot_enough = Decimal(spot_balance) >= spot_required
        if not is_spot_enough:
            base, _ = split_hb_trading_pair(self.trading_pair)
            float_spot_required = float(spot_required)
            self.logger().info(
                f"Insufficient balance in {self.spot_connector}: {spot_balance} {base}. "
                f"Required {float_spot_required:.4f} {base}."
            )
        perp_balance = self.get_balance(self.perp_connector, is_base=False)
        # long order WITH any splippage takes more capital
        long_price_with_slippage = self.limit_taker_price(
            self.perp_connector, is_buy=True
        )
        perp_required = long_price_with_slippage * self.base_order_amount
        is_perp_enough = Decimal(perp_balance) >= perp_required
        if not is_perp_enough:
            _, quote = split_hb_trading_pair(self.trading_pair)
            float_perp_required = float(perp_required)
            self.logger().info(
                f"Insufficient balance in {self.perp_connector}: {perp_balance:.4f} {quote}. "
                f"Required {float_perp_required:.4f} {quote}."
            )
        return is_spot_enough and is_perp_enough

    def sell_spot_long_perp(self) -> None:
        perp_long_price_with_slippage = self.limit_taker_price_with_slippage(
            self.perp_connector, is_buy=True
        )
        spot_sell_price_with_slippage = self.limit_taker_price_with_slippage(
            self.spot_connector, is_buy=False
        )
        position_action = (
            PositionAction.CLOSE if self.is_perp_in_short else PositionAction.OPEN
        )
        self.buy(
            self.perp_connector,
            self.trading_pair,
            amount=self.base_order_amount,
            order_type=OrderType.LIMIT,
            price=perp_long_price_with_slippage,
            position_action=position_action,
        )
        self.logger().info(
            f"Submitted long order in {self.perp_connector} for {self.trading_pair} "
            f"at price {perp_long_price_with_slippage:.06f}@{self.base_order_amount}"
        )
        self.sell(
            self.spot_connector,
            self.trading_pair,
            amount=self.base_order_amount,
            order_type=OrderType.LIMIT,
            price=spot_sell_price_with_slippage,
        )
        self.logger().info(
            f"Submitted sell order in {self.spot_connector} for {self.trading_pair} "
            f"at price {spot_sell_price_with_slippage:.06f}@{self.base_order_amount}"
        )

        self.last_submit_order_ts = self.current_timestamp
        return

    def limit_taker_price_with_slippage(
        self, connector_name: str, is_buy: bool
    ) -> Decimal:
        price = self.limit_taker_price(connector_name, is_buy)
        slippage = (
            Decimal(1 + self.slippage_buffer_bps / 10000)
            if is_buy
            else Decimal(1 - self.slippage_buffer_bps / 10000)
        )
        return price * slippage

    def limit_taker_price(self, connector_name: str, is_buy: bool) -> Decimal:
        limit_taker_price_result = self.connectors[connector_name].get_price_for_volume(
            self.trading_pair, is_buy, self.base_order_amount
        )
        return limit_taker_price_result.result_price

    def get_balance(self, connector_name: str, is_base: bool) -> float:
        if connector_name == self.perp_connector:
            assert not is_base, "Perpetual connector does not have base asset"
        base, quote = split_hb_trading_pair(self.trading_pair)
        balance = self.connectors[connector_name].get_available_balance(
            base if is_base else quote
        )
        return float(balance)

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."

        lines: List[str] = []
        self._append_buy_spot_short_perp_status(lines)
        lines.extend(["", ""])
        self._append_sell_spot_long_perp_status(lines)
        lines.extend(["", ""])
        self._append_balances_status(lines)
        lines.extend(["", ""])
        self._append_bot_states(lines)
        lines.extend(["", ""])
        return "\n".join(lines)

    def _append_buy_spot_short_perp_status(self, lines: List[str]) -> None:
        spot_buy_price = self.limit_taker_price(self.spot_connector, is_buy=True)
        perp_short_price = self.limit_taker_price(self.perp_connector, is_buy=False)
        return_pbs = (
            float((perp_short_price - spot_buy_price) / spot_buy_price) * 100 * 100
        )
        lines.append("Buy Spot Short Perp Opportunity:")
        lines.append(f"Buy Spot: {spot_buy_price}")
        lines.append(f"Short Perp: {perp_short_price}")
        lines.append(f"Return (bps): {return_pbs:.1f}%")
        return

    def _append_sell_spot_long_perp_status(self, lines: List[str]) -> None:
        perp_long_price = self.limit_taker_price(self.perp_connector, is_buy=True)
        spot_sell_price = self.limit_taker_price(self.spot_connector, is_buy=False)
        return_pbs = (
            float((spot_sell_price - perp_long_price) / perp_long_price) * 100 * 100
        )
        lines.append("Long Perp Sell Spot Opportunity:")
        lines.append(f"Long Perp: {perp_long_price}")
        lines.append(f"Sell Spot: {spot_sell_price}")
        lines.append(f"Return (bps): {return_pbs:.1f}%")
        return

    def _append_balances_status(self, lines: List[str]) -> None:
        base, quote = split_hb_trading_pair(self.trading_pair)
        spot_base_balance = self.get_balance(self.spot_connector, is_base=True)
        spot_quote_balance = self.get_balance(self.spot_connector, is_base=False)
        perp_quote_balance = self.get_balance(self.perp_connector, is_base=False)
        lines.append("Balances:")
        lines.append(f"Spot Base Balance: {spot_base_balance:.04f} {base}")
        lines.append(f"Spot Quote Balance: {spot_quote_balance:.04f} {quote}")
        lines.append(f"Perp Balance: {perp_quote_balance:04f} USDT")
        return

    def _append_bot_states(self, lines: List[str]) -> None:
        lines.append("Bot States:")
        lines.append(f"Current Timestamp: {self.current_timestamp}")
        lines.append(f"Strategy State: {self.strategy_state.name}")
        lines.append(f"Open Next Opportunity after: {self.next_arbitrage_opening_ts}")
        lines.append(f"Last In Flight State at: {self.in_flight_state_start_ts}")
        lines.append(f"Last Opened State at: {self.last_submit_order_ts}")
        lines.append(f"Completed Ordered IDs: {self.completed_order_ids}")
        return

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent) -> None:
        self.completed_order_ids.append(event.order_id)

    def did_complete_sell_order(self, event: SellOrderCompletedEvent) -> None:
        self.completed_order_ids.append(event.order_id)

    def did_change_position_mode_succeed(self, _):
        self.logger().info(
            f"Completed setting position mode to ONEWAY for {self.perp_connector}"
        )
        self._position_mode_ready = True

    def did_change_position_mode_fail(
        self, position_mode_changed_event: PositionModeChangeEvent
    ):
        self.logger().error(
            f"Failed to set position mode to ONEWAY. "
            f"Reason: {position_mode_changed_event.message}."
        )
        self.logger().warning(
            "Cannot continue. Please resolve the issue in the account."
        )
