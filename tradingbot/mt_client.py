"""Script of MT_Client what it sends commands to MT4/MT5."""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import List, Dict, Union, Callable, Tuple, TYPE_CHECKING, Set
from threading import Thread, Lock
from os.path import join, exists
from random import randrange
from pandas import DataFrame
from pathlib import Path
from time import sleep
import glob
import json

from tradingbot.config import Config
from tradingbot.log import log
from tradingbot.singleton import Singleton
from tradingbot.files import (
    try_load_json, try_remove_file, try_read_file)
from tradingbot.order_operations import OrderOperations
from tradingbot.order_type import get_order_type_from_str
from tradingbot.order import (
    Order, MutableOrderDetails, ImmutableOrderDetails, OrderPrice)
from tradingbot.ohlc import OHLC
from tradingbot.trading_methods import get_pip
from tradingbot.utils import string_to_date_utc
if TYPE_CHECKING:
  from tradingbot.event_handlers.event_handler import EventHandler


# Typing types
attributes_data_type = Dict[str, Dict]
messages_type = Dict[str, List[List[str]]]
account_info_type = Dict[str, Union[float, str]]
historical_data_type = Dict[str, DataFrame]


class MT_Client(metaclass=Singleton):
  """Forex Client MetaTrader class.

  This includes all of the functions needed for communication with MT4/MT5.
  """

  def __init__(self,
               event_handler: Union[EventHandler, None] = None,
               sleep_delay: float = 0.005,
               max_retry_command_seconds: int = 5 * 60
               ):
    """Initialize the attributes."""
    # Parameter attributes
    self.sleep_delay = sleep_delay
    self.max_retry_command_seconds = max_retry_command_seconds
    self.num_command_files = 50

    # Paths to output MT files
    self.set_agent_paths()

    # Control attributes
    self.event_handler = event_handler
    self.lock = Lock()
    self._last_messages_millis = 0
    self.command_id = 0

    # Data attributes
    self.messages: messages_type = {'INFO': [], 'ERROR': []}
    self.open_orders: List[Order] = []
    self.account_info: account_info_type = {}
    self.market_data: attributes_data_type = {}
    self.bar_data: attributes_data_type = {}
    self.historical_data: historical_data_type = {}
    self.historical_trades: attributes_data_type = {}
    self.successful_symbols: Set[str] = set()

    # State attributes
    self.ACTIVE = True

  def set_agent_paths(self) -> None:
    """Set the paths to the files generated by MQL."""
    mt_files_path = Config.mt_files_path
    self.prefix_files_path = 'AgentFiles'
    if exists(mt_files_path):
      self.path_orders = Path(
          join(mt_files_path, self.prefix_files_path, 'Orders.json'))
      self.path_messages = Path(
          join(mt_files_path, self.prefix_files_path, 'Messages.json'))
      self.path_market_data = Path(
          join(mt_files_path, self.prefix_files_path, 'Market_Data.json'))
      self.path_bar_data = Path(
          join(mt_files_path, self.prefix_files_path, 'Bar_Data.json'))
      self.path_historical_data = Path(
          join(mt_files_path, self.prefix_files_path))
      self.path_historical_trades = Path(
          join(mt_files_path, self.prefix_files_path,
               'Historical_Trades.json'))
      self.path_orders_stored = Path(
          join(mt_files_path, self.prefix_files_path,
               'Orders_Stored.json'))
      self.path_messages_stored = Path(
          join(mt_files_path, self.prefix_files_path,
               'Messages_Stored.json'))
      self.path_commands_prefix = Path(
          join(mt_files_path, self.prefix_files_path, 'Commands_'))
    else:
      log.error(f'mt_files_path: {mt_files_path} does not exist!')

  @staticmethod
  def start_thread(target: Callable) -> Thread:
    """To start the thread with a method as target."""
    thread = Thread(target=target, args=(), daemon=True)
    thread.start()
    return thread

  def start(self) -> None:
    """Start the threads."""
    self.START = True
    self.send_reset_command_ids_command()
    # Start the demand threads
    if Config.check_messages_thread:
      self.start_thread(self.start_thread_check_messages)
    if Config.check_market_data_thread:
      self.start_thread(self.start_thread_check_market_data)
    if Config.check_bar_data_thread:
      self.start_thread(self.start_thread_check_bar_data)
    if Config.check_open_orders_thread:
      self.start_thread(self.start_thread_check_open_orders)
    if Config.check_historical_data_thread:
      self.start_thread(self.start_thread_check_historical_data)
    if Config.check_historical_trades_thread:
      self.start_thread(self.start_thread_check_historical_trades)

  def stop(self) -> None:
    """Stop the threads."""
    self.START = False

  def activate(self) -> None:
    """Activate the threads."""
    self.ACTIVE = True

  def deactivate(self) -> None:
    """Deactivate the threads."""
    self.ACTIVE = False

  def start_thread_check_messages(self) -> None:
    """Start the thread to check messages."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_messages()

  def check_messages(self) -> messages_type:
    """Update and return the messages object."""
    data = try_load_json(self.path_messages)

    if len(data) > 0 and data != self.messages:

      for millis, message in sorted(data.items()):
        if int(millis) > self._last_messages_millis:
          self._last_messages_millis = int(millis)

          message_content = list(message.values())
          if 'ERROR' in message_content:
            self.messages['ERROR'].append(message_content[1:])
          else:
            self.messages['INFO'].append(message_content[1:])

          if self.event_handler:
            self.event_handler.on_message(self, message_content)

    return self.messages

  def get_messages(self) -> messages_type:
    """Return the messages object."""
    return self.messages

  def set_messages(self, data: messages_type) -> None:
    """Set manually the messages object."""
    self.messages = data

  def clean_messages(self):
    """Clean the messages object."""
    try_remove_file(self.path_messages)
    self.messages = {'INFO': [], 'ERROR': []}

  def start_thread_check_market_data(self) -> None:
    """Start the thread to check market data."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_market_data()

  def check_market_data(self) -> Dict[str, Dict]:
    """Update, trigger event if needed and return the market data object."""
    data = try_load_json(self.path_market_data)

    if len(data) > 0 and data != self.market_data:

      if self.event_handler:
        for symbol in data.keys():
          cond = symbol not in self.market_data
          cond = cond or data[symbol] != self.market_data.get(symbol)
          if cond:
            self.event_handler.on_tick(
                self,
                symbol,
                data[symbol]['bid'],
                data[symbol]['ask']
            )
      self.market_data = data

    return self.market_data

  def get_bid_ask(self, symbol: str) -> Tuple[float, float]:
    """Return the bid and ask price of a symbol.

    Return 0,0 if symbol not found.

    Bid: sell price
    Ask: buy price
    """
    try:
      bid_ask = self.market_data[symbol]
      return bid_ask['bid'], bid_ask['ask']
    except KeyError:
      log.warning(f'Symbol {symbol} not found in market data.')
      return 0, 0

  def start_thread_check_bar_data(self) -> None:
    """Start the thread to check bar data."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_bar_data()

  def check_bar_data(self) -> Dict[str, Dict]:
    """Update, trigger event if needed and return the bar data object."""
    data = try_load_json(self.path_bar_data)

    if len(data) > 0 and data != self.bar_data:

      if self.event_handler:
        for st in data.keys():
          cond = st not in self.bar_data
          cond = cond or data[st] != self.bar_data[st]
          if cond:
            symbol, time_frame = st.split('_')
            self.event_handler.on_bar_data(
                self,
                symbol,
                time_frame,
                data[st]['time'],
                OHLC(DataFrame({
                    'open': data[st]['open'],
                    'high': data[st]['high'],
                    'low': data[st]['low'],
                    'close': data[st]['close'],
                })),
                data[st]['tick_volume']
            )
      self.bar_data = data

    return self.bar_data

  def start_thread_check_open_orders(self) -> None:
    """Start the thread to check open orders."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_open_orders()

  def check_open_orders(self) -> List[Order]:
    """Update, trigger event if needed and return the open orders object.

    The open orders can be pending or filled.
    """
    data = try_load_json(self.path_orders)
    data_orders = data.get('orders')
    data_account_info = data.get('account_info')

    if (len(data) > 0
        and isinstance(data_orders, Dict)
        and isinstance(data_account_info, Dict)
        and (data_orders != self.open_orders
             or data_account_info != self.account_info)):

      orders = self._transform_json_orders_to_orders(data_orders)

      new_event = False
      # If an existing open order is not in the new data, trigger an event
      for order in self.open_orders:
        if order not in orders:
          new_event = True
          break

      # If a new open order is not in the existing data, trigger an event
      for order in orders:
        if order not in self.open_orders:
          new_event = True
          break

      self.account_info = data_account_info
      self.open_orders = orders

      with open(self.path_orders_stored, 'w') as f:
        f.write(json.dumps(data))

      if new_event and self.event_handler:
        self.event_handler.on_order_event(
            self, self.account_info, self.open_orders
        )

    return self.open_orders

  def _transform_json_orders_to_orders(
          self, json_orders: Dict) -> List[Order]:
    """Return a list of open Order objects."""
    return [
        Order(
            MutableOrderDetails(
                OrderPrice(
                    price=o['open_price'],
                    stop_loss=o['SL'],
                    take_profit=o['TP']
                ), lots=o['lots']
            ),
            ImmutableOrderDetails(
                symbol=o['symbol'],
                order_type=get_order_type_from_str(o['type']),
                magic=o['magic'],
                comment=o['comment']
            ), ticket=int(t), pnl=o['pnl'])
        for t, o in json_orders.items()
    ]

  def start_thread_check_historical_data(self) -> None:
    """Start the thread to check historical data."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_historical_data()

  def check_historical_data(
          self, symbol: Union[str, None] = None) -> Dict:
    """Update historical_data, trigger event if needed and return that data."""
    # "symbol" is None when it comes from "start_thread_check_historical_data"
    # In this case, we need to get a random remaining symbol
    remaining_symbols = self.get_remaining_symbols()
    if len(remaining_symbols) == 0:
      return {}
    if symbol is None:
      symbol = remaining_symbols[randrange(len(remaining_symbols))]

    # We read the symbol file
    file_path = self.path_historical_data.joinpath(
        f'Historical_Data_{symbol}.json')
    data = try_load_json(file_path)

    if len(data) > 0:
      # The dataframe is built
      df = DataFrame.from_dict(
          data[f'{symbol}_{Config.timeframe}'], orient='index')
      self.historical_data[symbol] = df

      # The date and time corresponding to the last load are calculated
      if self._is_historical_data_up_to_date(df):
        log.debug(f'{symbol} -> {(df.index[0], df.index[-1])}')

        self.successful_symbols.add(symbol)
        if self.event_handler:
          self.event_handler.on_historical_data(self, symbol, OHLC(df))

        # We delete the command file(s) in case it hasn't been deleted.
        command_files = self.command_file_exist(symbol)
        for com in command_files:
          try_remove_file(com)

    return data

  @staticmethod
  def _is_historical_data_up_to_date(df: DataFrame) -> bool:
    """Check if the historical data is up to date."""
    now_date = datetime.now(Config.utc_timezone)
    td = timedelta(minutes=now_date.minute % 5,
                   seconds=now_date.second,
                   microseconds=now_date.microsecond)
    rounded_now_date = now_date - td
    start_range = rounded_now_date
    end_range = rounded_now_date + timedelta(minutes=5)
    last_date_from_df = string_to_date_utc(
        str_date=df.index[-1], from_timezone=Config.broker_timezone)

    return (last_date_from_df >= start_range
            and last_date_from_df < end_range)

  def start_thread_check_historical_trades(self) -> None:
    """Start the thread to check historical trades."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_historical_trades()

  def check_historical_trades(self) -> Dict:
    """Update and return the historical trades object."""
    self.historical_trades = try_load_json(self.path_historical_trades)
    return self.historical_trades

  def subscribe_symbols(self, symbols: List[str]) -> None:
    """To send a SUBSCRIBE_SYMBOLS command to subscribe to market (tick) data.

    Args:
        symbols (list[str]): List of symbols to subscribe to.

    Returns:
        None

        The data will be stored in self.market_data.
        On receiving the data the event_handler.on_tick()
        function will be triggered.

    """
    self.send_command('SUBSCRIBE_SYMBOLS', ','.join(symbols))

  def subscribe_symbols_bar_data(
      self,
      symbols: List[List[str]]
  ) -> None:
    """To send a SUBSCRIBE_SYMBOLS_BAR_DATA command to subscribe to bar data.

    Kwargs:
        symbols (list[list[str]]): List of lists containing symbol/time frame
        combinations to subscribe to. For example:
        symbols = [['EURUSD', 'M1'], ['GBPUSD', 'H1']]

    Returns:
        None

        The data will be stored in self.bar_data.
        On receiving the data the event_handler.on_bar_data()
        function will be triggered.

    """
    data = [f'{st[0]},{st[1]}' for st in symbols]
    self.send_command('SUBSCRIBE_SYMBOLS_BAR_DATA',
                      ','.join(str(p) for p in data))

  def get_historical_data(
      self,
      symbol: str,
      time_frame: str
  ) -> None:
    """To send a GET_HISTORIC_DATA command to request historical data.

    Kwargs:
        symbol (str): Symbol to get historical data.
        time_frame (str): Time frame for the requested data.

    Returns:
        None

        The data will be stored in self.historical_data.
        On receiving the data the event_handler.on_historical_data()
        function will be triggered.
    """
    # We add 10 hours because the way the library interprets this input requires
    # overshooting to ensure capturing up to the last record.
    end = datetime.now(Config.broker_timezone) + timedelta(hours=10)
    start = (end - timedelta(days=Config.lookback_days)).timestamp()
    end = end.timestamp()
    data = [symbol, time_frame, int(start), int(end)]
    self.send_command('GET_HISTORICAL_DATA', ','.join(str(p) for p in data))

  def get_historical_trades(self, lookback_days: int = 30) -> None:
    """To send a GET_HISTORIC_TRADES command to request historical trades.

    Kwargs:
        lookback_days (int): Days to look back into the trade history.
        The history must also be visible in MT4.

    Returns:
        None

        The data will be stored in self.historical_trades.
        On receiving the data the event_handler.on_historical_trades()
        function will be triggered.
    """
    self.send_command('GET_HISTORICAL_TRADES', str(lookback_days))

  def create_new_order(self, order: Order) -> None:
    """Create new order."""
    log.debug(f'Creating new order: {order}')

    if order.order_type.pending:
      bid, ask = self.get_bid_ask(order.symbol)
      self._modify_pending_order(order, bid, ask)

    self.send_open_order_command(order)

  def _modify_pending_order(self, order: Order, bid: float, ask: float) -> None:
    """Modify the pending order based on the bid/ask."""
    if bid != 0 and ask != 0:
      ot = order.order_type
      if ot.buy and order.price > ask:
        ot.value = OrderOperations.BUYSTOP
      elif ot.buy and order.price < ask:
        ot.value = OrderOperations.BUYLIMIT
      elif ot.sell and order.price < bid:
        ot.value = OrderOperations.SELLSTOP
      elif ot.sell and order.price > bid:
        ot.value = OrderOperations.SELLLIMIT

  def send_open_order_command(self, order: Order) -> None:
    """To send an OPEN_ORDER command to open an order."""
    data = [
        order.symbol, order.order_type.value.value, order.lots, order.price,
        order.stop_loss, order.take_profit, order.magic, order.comment,
        order.expiration
    ]
    self.send_command('OPEN_ORDER', ','.join(str(p) for p in data))

  def send_modify_order_command(
      self,
      ticket: int,
      mod: MutableOrderDetails
  ) -> None:
    """To send a MODIFY_ORDER command to modify an order.

    Args:
        ticket (int): Ticket of the order that should be modified.

    Kwargs:
        lots (float): Volume in lots
        price (float): Price of the (pending) order. Non-zero only
            works for pending orders.
        stop_loss (float): New stop loss price.
        take_profit (float): New take profit price.
        expiration (int): New expiration time given as timestamp in seconds.
            Can be zero if the order should not have an expiration time.

    """
    data = [
        ticket, mod.lots, mod.price, mod.stop_loss, mod.take_profit,
        mod.expiration
    ]
    self.send_command('MODIFY_ORDER', ','.join(str(p) for p in data))

  def place_break_even(self, order: Order) -> None:
    """Modify the order to place a break even."""
    pip = get_pip(order.symbol)
    buy = order.order_type.buy
    break_even = order.price + pip if buy else order.price - pip
    self.send_modify_order_command(
        order.ticket,
        MutableOrderDetails(
            prices=OrderPrice(
                price=break_even,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit
            ),
            lots=order.lots,
            expiration=order.expiration
        )
    )
    log.debug(f'Break even placed in {order.magic}')

  def send_close_order_command(self, ticket: int, lots: float = 0) -> None:
    """To send a CLOSE_ORDER command to close an order.

    Args:
        ticket (int): Ticket of the order that should be closed.

    Kwargs:
        lots (float): Volume in lots. If lots=0 it will try to
            close the complete position.

    """
    data = [ticket, lots]
    self.send_command('CLOSE_ORDER', ','.join(str(p) for p in data))

  def send_close_all_orders_command(self) -> None:
    """To send a CLOSE_ALL_ORDERS command to close all orders."""
    self.send_command('CLOSE_ALL_ORDERS', '')

  def send_close_orders_by_symbol_command(self, symbol: str) -> None:
    """To send a CLOSE_ORDERS_BY_SYMBOL command to close all orders.

    Args:
        symbol (str): Symbol for which all orders should be closed.

    """
    self.send_command('CLOSE_ORDERS_BY_SYMBOL', symbol)

  def send_close_orders_by_magic_command(self, magic: str) -> None:
    """To send a CLOSE_ORDERS_BY_MAGIC command to close all orders.

    Args:
        magic (str): Magic number for which all orders should
            be closed.

    """
    self.send_command('CLOSE_ORDERS_BY_MAGIC', magic)

  def send_reset_command_ids_command(self) -> None:
    """To send a RESET_COMMAND_IDS command to reset stored command IDs.

    This should be used when restarting the python side without restarting
    the mql side.
    """
    self.command_id = 0

    self.send_command('RESET_COMMAND_IDS', '')

    # sleep to make sure it is read before other commands.
    sleep(0.5)

  def send_command(self, command: str, content: str) -> None:
    """To send a command to the MQL side.

    Multiple command files are used to allow for fast execution
    of multiple commands in the correct chronological order.

    """
    # Acquire lock so that different threads do not use the same
    # command_id or write at the same time.
    self.lock.acquire()

    self.command_id = (self.command_id + 1) % 100000

    end_time = datetime.now(Config.utc_timezone) + timedelta(
        seconds=self.max_retry_command_seconds)
    now = datetime.now(Config.utc_timezone)

    # trying again for X seconds in case all files exist or are
    # currently read from mql side.
    while now < end_time:
      # using 10 different files to increase the execution speed
      # for multiple commands.
      success = False
      for i in range(self.num_command_files):
        # only send commend if the file does not exists so that we
        # do not overwrite all commands.
        file_path = f'{self.path_commands_prefix}{i}.txt'
        if not exists(file_path):
          with open(file_path, 'w') as f:
            f.write(f'<:{self.command_id}|{command}|{content}:>')
          success = True
          break
      if success:
        break
      sleep(self.sleep_delay)
      now = datetime.now(Config.utc_timezone)

    # release lock again
    self.lock.release()

  def clean_all_command_files(self) -> None:
    """Clean command files."""
    for i in range(self.num_command_files):
      file_path = Path(f'{self.path_commands_prefix}{i}.txt')
      if not try_remove_file(file_path):
        break

  def clean_all_historical_files(self) -> None:
    """Clean historical files."""
    for symbol in Config.symbols:
      file_path = self.path_historical_data.joinpath(
          f'Historical_Data_{symbol}.json')
      try_remove_file(file_path)

  def command_file_exist(self, symbol: str) -> List[Path]:
    """Return the command files that match request hist. data from symbol."""
    g = glob.glob(f'{self.path_commands_prefix}*')
    pattern = f'GET_HISTORICAL_DATA|{symbol}'
    return [
        Path(f) for f in g if pattern in try_read_file(Path(f))
    ]

  def get_balance(self) -> float:
    """Return the balance of the account."""
    balance = self.account_info.get('balance')
    if isinstance(balance, float):
      return balance
    else:
      log.warning(f'Balance is not a float: {balance}')
      return -1.0

  def get_remaining_symbols(self) -> List[str]:
    """Return the list of remaining symbols."""
    all_symbols = set(Config.symbols)
    successful_symbols = self.successful_symbols
    return list(all_symbols - successful_symbols)
