"""Script of MT_Client what it sends commands to MT4/MT5."""
import json
from time import sleep
from threading import Thread, Lock
from os.path import join, exists
from datetime import datetime, timedelta
from pandas import DataFrame
import subprocess
from .config import Config
from .log import log
import typing as ty
from random import randrange
from tradingbot.files import get_successful_symbols
from .files import try_load_json, try_remove_file
from .utils import stringToDateUTC
from pathlib import Path


class MT_Client():
  """Forex Client MetaTrader class.

  This includes all of the functions needed for communication with MT4/MT5.
  """

  def __init__(self, event_handler=None,
               sleep_delay=0.005,
               max_retry_command_seconds=5*60,
               start=True
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
    self.messages = {'INFO': [], 'ERROR': []}
    self.open_orders = {}
    self.account_info = {}
    self.market_data = {}
    self.bar_data = {}
    self.historic_data = {}
    self.historic_trades = {}

    # State attributes
    self.ACTIVE = True
    self.START = start

    # Call initial methods
    if start:
      self.reset_command_ids()
      self.start_thread(self.check_open_orders)

  def set_agent_paths(self) -> None:
    """Set the paths to the files generated by MQL."""
    mt_files_path = Config.mt_files_path
    if not exists(mt_files_path):
      log.error(f'mt_files_path: {mt_files_path} does not exist!')
      exit()

    self.path_orders = Path(join(mt_files_path,
                            'AgentFiles', 'Orders.json'))
    self.path_messages = Path(join(mt_files_path,
                              'AgentFiles', 'Messages.json'))
    self.path_market_data = Path(join(mt_files_path,
                                 'AgentFiles', 'Market_Data.json'))
    self.path_bar_data = Path(join(mt_files_path,
                              'AgentFiles', 'Bar_Data.json'))
    self.path_historic_data = Path(join(mt_files_path,
                                   'AgentFiles'))
    self.path_historic_trades = Path(join(mt_files_path,
                                     'AgentFiles', 'Historic_Trades.json'))
    self.path_orders_stored = Path(join(mt_files_path,
                                   'AgentFiles', 'Orders_Stored.json'))
    self.path_messages_stored = Path(join(mt_files_path,
                                     'AgentFiles', 'Messages_Stored.json'))
    self.path_commands_prefix = Path(join(mt_files_path,
                                     'AgentFiles', 'Commands_'))

  @staticmethod
  def start_thread(target: ty.Callable) -> Thread:
    """To start the thread with a method as target."""
    thread = Thread(target=target, args=(), daemon=True)
    thread.start()
    return thread

  def start(self) -> None:
    """Start the threads."""
    self.START = True

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

  def check_messages(self) -> ty.Dict[str, ty.List]:
    """Update and return the messages object."""
    data = try_load_json(self.path_messages)

    if len(data) > 0 and data != self.messages:

      for millis, message in sorted(data.items()):
        if int(millis) > self._last_messages_millis:
          self._last_messages_millis = int(millis)

          message = list(message.values())
          if 'ERROR' in message:
            self.messages['ERROR'].append(message[1:])
          else:
            self.messages['INFO'].append(message[1:])

    return self.messages

  def get_messages(self) -> ty.Dict[str, ty.List[str]]:
    """Return the messages object."""
    return self.messages

  def set_messages(self, data={'INFO': [], 'ERROR': []}) -> None:
    """Set manually the messages object."""
    self.messages = data

  def start_thread_check_market_data(self) -> None:
    """Start the thread to check market data."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_market_data()

  def check_market_data(self) -> ty.Dict[str, ty.Dict]:
    """Update, trigger event if needed and return the market data object."""
    data = try_load_json(self.path_market_data)

    if len(data) > 0 and data != self.market_data:

      if self.event_handler is not None:
        for symbol in data.keys():
          cond1 = symbol not in self.market_data
          cond2 = data[symbol] != self.market_data[symbol]
          if cond1 or cond2:
            # TODO: event handler
            pass
      self.market_data = data

    return self.market_data

  def start_thread_check_bar_data(self) -> None:
    """Start the thread to check bar data."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_bar_data()

  def check_bar_data(self) -> ty.Dict[str, ty.Dict]:
    """Update, trigger event if needed and return the bar data object."""
    data = try_load_json(self.path_bar_data)

    if len(data) > 0 and data != self.bar_data:

      if self.event_handler is not None:
        for st in data.keys():
          cond1 = st not in self.bar_data
          cond2 = self.bar_data[st] != self.bar_data[st]
          if cond1 or cond2:
            # TODO: event handler
            pass
      self.bar_data = data

    return self.bar_data

  def start_thread_check_open_orders(self) -> None:
    """Start the thread to check open orders."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_open_orders()

  def check_open_orders(self) -> ty.Dict[str, ty.Dict]:
    """Update, trigger event if needed and return the open orders object."""
    data = try_load_json(self.path_orders)
    data_orders = data.get('orders')
    data_account_info = data.get('account_info')

    if (len(data) > 0
        and isinstance(data_orders, ty.Dict)
        and isinstance(data_account_info, ty.Dict)
        and (data_orders != self.open_orders
             or data_account_info != self.account_info)):

      new_event = False
      for order_id, order in self.open_orders.items():
        # also triggers if a pending order got filled?
        if order_id not in data_orders.keys():
          new_event = True

      for order_id, order in data_orders.items():
        if order_id not in self.open_orders:
          new_event = True

      self.account_info = data_account_info
      self.open_orders = data_orders

      with open(self.path_orders_stored, 'w') as f:
        f.write(json.dumps(data))

      if self.event_handler is not None and new_event:
        # TODO: event handler
        pass

    return self.open_orders

  def start_thread_check_historic_data(self) -> None:
    """Start the thread to check historic data."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_historic_data()

  def check_historic_data(
          self, symbol: ty.Union[str, None] = None) -> ty.Dict:
    """Update historic_data, trigger event if needed and return that data."""
    # "symbol" is None when it comes from "start_thread_check_historic_data"
    # In this case, we need to get a random remaining symbol
    if symbol is None:
      all_symbols = set(Config.symbols)
      successful_symbols = set(get_successful_symbols())
      remaining_symbols = list(all_symbols - successful_symbols)
      symbol = remaining_symbols[randrange(len(remaining_symbols))]

    # We read the symbol file
    file_path = self.path_historic_data.joinpath(
        f'Historic_Data_{symbol}.json')
    data = try_load_json(file_path)

    if len(data) > 0:
      # The dataframe is built
      df = DataFrame.from_dict(
          data[f'{symbol}_{Config.timeframe}'], orient='index')
      self.historic_data[symbol] = df

      # The date and time corresponding to the last load are calculated
      if self._is_historic_data_up_to_date(df):
        log.info(f'{symbol} -> {(df.index[0], df.index[-1])}')

        # TODO: call to event handler

        # We delete the command file(s) in case it hasn't been deleted.
        command_files = self.command_file_exist(symbol)
        for com in command_files:
          try_remove_file(com)

    return data

  @staticmethod
  def _is_historic_data_up_to_date(df: DataFrame) -> bool:
    """Check if the historic data is up to date."""
    now_date = datetime.now(Config.utc_timezone)
    td = timedelta(minutes=now_date.minute % 5,
                   seconds=now_date.second,
                   microseconds=now_date.microsecond)
    rounded_now_date = now_date - td
    start_range = rounded_now_date
    end_range = rounded_now_date + timedelta(minutes=5)
    last_date_from_df = stringToDateUTC(
        str_date=df.index[-1], timezone=Config.broker_timezone)

    return (last_date_from_df >= start_range and
            last_date_from_df < end_range)

  def start_thread_check_historic_trades(self) -> None:
    """Start the thread to check historic trades."""
    while self.ACTIVE:
      sleep(self.sleep_delay)
      if self.START:
        self.check_historic_trades()

  def check_historic_trades(self) -> ty.Dict:
    """Update and return the historic trades object."""
    self.historic_trades = try_load_json(self.path_historic_trades)
    return self.historic_trades

  def subscribe_symbols(self, symbols) -> None:
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

  def subscribe_symbols_bar_data(self, symbols=[['EURUSD', 'M1']]) -> None:
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

  def get_historic_data(self,
                        symbol='EURUSD',
                        time_frame='D1',
                        start=(datetime.utcnow() -
                               timedelta(days=30)).timestamp(),
                        end=datetime.utcnow().timestamp()) -> None:
    """To send a GET_HISTORIC_DATA command to request historic data.

    Kwargs:
        symbol (str): Symbol to get historic data.
        time_frame (str): Time frame for the requested data.
        start (int): Start timestamp of the requested data.
        end (int): End timestamp of the requested data.

    Returns:
        None

        The data will be stored in self.historic_data.
        On receiving the data the event_handler.on_historic_data()
        function will be triggered.
    """
    # We add 10 hours because the way the library interprets this input requires
    # overshooting to ensure capturing up to the last record.
    end = datetime.now(Config.forex_timezone) + timedelta(hours=10)
    start = (end - timedelta(days=Config.lookback_days)).timestamp()
    end = end.timestamp()
    data = [symbol, time_frame, int(start), int(end)]
    self.send_command('GET_HISTORIC_DATA', ','.join(str(p) for p in data))

  def get_historic_trades(self, lookback_days=30) -> None:
    """To send a GET_HISTORIC_TRADES command to request historic trades.

    Kwargs:
        lookback_days (int): Days to look back into the trade history.
        The history must also be visible in MT4.

    Returns:
        None

        The data will be stored in self.historic_trades.
        On receiving the data the event_handler.on_historic_trades()
        function will be triggered.
    """
    self.send_command('GET_HISTORIC_TRADES', str(lookback_days))

  def open_order(self, symbol='EURUSD',
                 order_type='buy',
                 lots=0.01,
                 price=0,
                 stop_loss=0,
                 take_profit=0,
                 magic=0,
                 comment='',
                 expiration=0) -> None:
    """To send an OPEN_ORDER command to open an order.

    Kwargs:
        symbol (str): Symbol for which an order should be opened.
        order_type (str): Order type. Can be one of:
            'buy', 'sell', 'buylimit', 'selllimit', 'buystop', 'sellstop'
        lots (float): Volume in lots
        price (float): Price of the (pending) order. Can be zero
            for market orders.
        stop_loss (float): SL as absolute price. Can be zero
            if the order should not have an SL.
        take_profit (float): TP as absolute price. Can be zero
            if the order should not have a TP.
        magic (int): Magic number
        comment (str): Order comment
        expiration (int): Expiration time given as timestamp in seconds.
            Can be zero if the order should not have an expiration time.

    """
    data = [symbol, order_type, lots, price, stop_loss,
            take_profit, magic, comment, expiration]
    self.send_command('OPEN_ORDER', ','.join(str(p) for p in data))

  def modify_order(self, ticket: int,
                   lots=0.01,
                   price=0,
                   stop_loss=0,
                   take_profit=0,
                   expiration=0) -> None:
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
    data = [ticket, lots, price, stop_loss, take_profit, expiration]
    self.send_command('MODIFY_ORDER', ','.join(str(p) for p in data))

  def close_order(self, ticket: int, lots: float = 0) -> None:
    """To send a CLOSE_ORDER command to close an order.

    Args:
        ticket (int): Ticket of the order that should be closed.

    Kwargs:
        lots (float): Volume in lots. If lots=0 it will try to
            close the complete position.

    """
    data = [ticket, lots]
    self.send_command('CLOSE_ORDER', ','.join(str(p) for p in data))

  def close_all_orders(self) -> None:
    """To send a CLOSE_ALL_ORDERS command to close all orders."""
    self.send_command('CLOSE_ALL_ORDERS', '')

  def close_orders_by_symbol(self, symbol: str) -> None:
    """To send a CLOSE_ORDERS_BY_SYMBOL command to close all orders.

    Args:
        symbol (str): Symbol for which all orders should be closed.

    """
    self.send_command('CLOSE_ORDERS_BY_SYMBOL', symbol)

  def close_orders_by_magic(self, magic) -> None:
    """To send a CLOSE_ORDERS_BY_MAGIC command to close all orders.

    Args:
        magic (str): Magic number for which all orders should
            be closed.

    """
    self.send_command('CLOSE_ORDERS_BY_MAGIC', magic)

  def reset_command_ids(self) -> None:
    """To send a RESET_COMMAND_IDS command to reset stored command IDs.

    This should be used when restarting the python side without restarting
    the mql side.
    """
    self.command_id = 0

    self.send_command("RESET_COMMAND_IDS", "")

    # sleep to make sure it is read before other commands.
    sleep(0.5)

  def send_command(self, command, content) -> None:
    """To send a command to the MQL side.

    Multiple command files are used to allow for fast execution
    of multiple commands in the correct chronological order.

    """
    # Acquire lock so that different threads do not use the same
    # command_id or write at the same time.
    self.lock.acquire()

    self.command_id = (self.command_id + 1) % 100000

    end_time = datetime.utcnow() + timedelta(
        seconds=self.max_retry_command_seconds)
    now = datetime.utcnow()

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
      now = datetime.utcnow()

    # release lock again
    self.lock.release()

  def clean_all_command_files(self) -> None:
    """Clean command files."""
    for i in range(self.num_command_files):
      file_path = Path(f'{self.path_commands_prefix}{i}.txt')
      if not try_remove_file(file_path):
        break

  def clean_all_historic_files(self) -> None:
    """Clean historic files."""
    for symbol in Config.symbols:
      file_path = self.path_historic_data.joinpath(
          f'Historic_Data_{symbol}.json')
      try_remove_file(file_path)

  def command_file_exist(self, symbol: str) -> ty.List[Path]:
    """Return the command files that match request historic data from symbol."""
    e = f'grep "GET_HISTORIC_DATA|{symbol}" "{self.path_commands_prefix}"*'
    r = subprocess.run(e, shell=True, capture_output=True, text=True)

    # Found
    if r.returncode == 0:
      found_files = r.stdout.split('\n')
      return [Path(ff.split(':<:')[0]) for ff in found_files]
    # Not found
    else:
      return []
