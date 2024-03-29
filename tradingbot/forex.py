"""This script is one of the possible entry points of the project."""
from datetime import datetime, timedelta
from tradingbot.mt_client import MT_Client
from .config import Config
from .files import Files
from . import files as f
from .utils import get_remaining_symbols, reboot_mt
from tradingbot.strategies.strategy_factory import strategy_factory
from random import randrange
from .log import log
import traceback


def handle():
  """Entry point of the forex bot."""
  if not is_locked() and check_time_viability():
    try:
      main()
    except Exception:  # noqa
      # Finish the bot
      finish()
      # Log the error
      log.error(traceback.format_exc())


def main():
  """Execute forex bot."""
  # First of all, we lock the execution of the forex bot.
  # To prevent another execution to run at the same time.
  f.lock(Files.FOREX_LOCK)

  # Start the MT Client
  mt_client = MT_Client()
  mt_client.start()

  # Clean all files
  mt_client.clean_all_command_files()
  mt_client.clean_all_historical_files()
  mt_client.clean_messages()
  f.reset_successful_symbols_file()

  # Dates
  utc_date = datetime.now(Config.utc_timezone)
  local_date = datetime.now(Config.local_timezone)

  # The instant of time that executed this main
  execution_time = timedelta(
      minutes=utc_date.minute % 5,
      seconds=utc_date.second,
      microseconds=utc_date.microsecond
  )

  # Send profit message
  _send_profit_message(local_date)

  # Send commands to obtain the historical data
  [mt_client.get_historical_data(s, Config.timeframe) for s in Config.symbols]

  # Send commands to obtain bid/ask
  mt_client.subscribe_symbols(Config.symbols)

  # Handle the existing trades
  handle_trades(mt_client)

  # Process the result of "get_historical_data"
  handle_new_historical_data(utc_date, execution_time)

  # Finish the main
  finish()


def handle_trades(mt_client: MT_Client) -> None:
  """Handle the existing trades."""
  orders = mt_client.check_open_orders()
  for order in orders:
    strategy = strategy_factory(order.comment)

    if order.order_type.pending:
      strategy.handle_pending_orders(order)
    elif order.order_type.market:
      strategy.handle_filled_orders(order)

  len_orders = len(orders)
  message = f'Number of open orders: {len_orders}'
  if len_orders > 900:
    log.warning(message)
  else:
    log.debug(message)


def handle_new_historical_data(
        utc_date: datetime, execution_time: timedelta) -> None:
  """Handle the new historical data."""
  # Initialize the remaining symbols
  rs = get_remaining_symbols()

  # The execution will take up to 4 minutes
  stop_condition = utc_date - execution_time + timedelta(minutes=4)

  while len(rs) > 0 and datetime.now(Config.utc_timezone) < stop_condition:
    # Get randomly the next symbol
    next_symbol = rs[randrange(len(rs))]

    # Check if JSON data is available to trigger the event
    mt_client = MT_Client()
    mt_client.check_historical_data(next_symbol)

    # Update the remaining symbols
    rs = get_remaining_symbols()

  # Check if there are remaining symbols to process
  if len(rs) > 0:
    log.warning(f'{len(rs)} remaining symbols to process.')
  else:
    f.reset_consecutive_times_down_file()

  # Check if MT needs to restart
  check_mt_needs_to_restart(len(rs))


def _send_profit_message(local_date: datetime) -> bool:
  """Get the balance of the account."""
  mt_client = MT_Client()
  balance = mt_client.get_balance()
  last_balance = f.get_last_balance()
  difference = balance - last_balance
  emoji = '🚀' if difference >= 0 else '☔'
  message_condition = local_date.hour % 12 == 0 and local_date.minute == 5
  if message_condition:
    log.info(f'{emoji} {difference:.2f} €')
    return True
  else:
    return False


def check_mt_needs_to_restart(n_remaining_symbols: int) -> None:
  """Check if MT needs to restart."""
  ctd = f.get_consecutive_times_down()
  symbols_len = len(Config.symbols)
  if n_remaining_symbols > int(symbols_len / 2) and ctd > 4:
    reboot_mt()
  else:
    f.increment_consecutive_times_down()


def is_locked() -> bool:
  """Return True if the forex-bot is running."""
  return f.file_exists(Files.FOREX_LOCK.value)


def check_time_viability() -> bool:
  """Check if the forex bot is viable to run."""
  now_date = datetime.now(Config.broker_timezone)
  # Monday (0) -> Sunday (6)
  is_weekday = now_date.weekday() in [0, 1, 2, 3, 4]
  # TODO: When executions are performed with the real account,
  # we need to consider testing the removal of this condition.
  is_not_on_the_hour = now_date.minute != 0
  return is_weekday and is_not_on_the_hour


def finish() -> None:
  """Finish the forex bot."""
  mt_client = MT_Client()
  mt_client.stop()
  f.unlock(Files.FOREX_LOCK)
