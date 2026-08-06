import os
import logging
from queue import Queue
import requests
import betfairlightweight
from betfairlightweight.filters import streaming_market_filter, streaming_market_data_filter

# Set up clean terminal logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 1. Telegram Configuration
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8435489741")

def send_telegram_alert(text):
    """Dispatches a synchronized, raw text message alert directly to your Telegram chat."""
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logging.info("Telegram notification delivered successfully.")
        else:
            logging.error(f"Telegram API error: Status code {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Failed to transmit Telegram network request: {e}")

# 2. Initialize Betfair Client
trading = betfairlightweight.APIClient(
    username="YOUR_BETFAIR_USERNAME",
    password="YOUR_BETFAIR_PASSWORD",
    app_key="YOUR_DELAYED_FREE_APP_KEY",
    certs="/path/to/your/certs/directory"
)

# Authenticate with the API
trading.login()

# 3. Configure the Output Queue to handle incoming ticks
output_queue = Queue()
listener = betfairlightweight.StreamListener(output_q=output_queue)

# Create the persistent TCP network connection
betfair_socket = trading.streaming.create_stream(listener=listener)

# 4. Filter for UK & Irish Horse Racing Win Markets
market_filter = streaming_market_filter(
    event_type_ids=['7'],        # 7 = Horse Racing
    country_codes=['GB', 'IE'],   # UK & Ireland
    market_types=['WIN']         # Win markets only
)

# Ask explicitly for the Market Definition metadata.
market_data_filter = streaming_market_data_filter(fields=['EX_MARKET_DEF'])

# Subscribe to the stream array
betfair_socket.subscribe_to_markets(
    market_filter=market_filter,
    market_data_filter=market_data_filter,
    conflate_ms=500  # Pushes bundled updates every 500 milliseconds
)

# 5. Spin up the background socket listener thread
betfair_socket.start(async_=True)
logging.info("Betfair stream socket connected. Listening for structural market updates...")
send_telegram_alert("🚀 *Betfair Non-Runner Bot Connected!* \nMonitoring UK & IE markets live.")

# 6. Process the pushed updates continuously
known_non_runners = set()

try:
    while True:
        # Block and wait until Betfair pushes a fresh packet down the pipe
        market_books = output_queue.get()
        
        for market_book in market_books:
            market_id = market_book.market_id
            
            # Check if the packet includes the definitive market structure block
            if market_book.market_definition:
                definition = market_book.market_definition
                
                # Iterate through all historical and active runners in the market
                for runner in definition.runners:
                    runner_id = runner.selection_id
                    unique_key = f"{market_id}_{runner_id}"
                    
                    # Detect status change flag
                    if runner.status == "REMOVED" and unique_key not in known_non_runners:
                        known_non_runners.add(unique_key)
                        
                        # Format structural message block
                        alert_msg = (
                            f"🚨 *NON-RUNNER ANNOUNCED!*\n\n"
                            f"• *Market ID:* `{market_id}`\n"
                            f"• *Selection ID:* `{runner_id}`\n"
                            f"• *Reduction Factor:* `{runner.adjustment_factor}%`\n"
                            f"• *Removal Date:* `{runner.removal_date}`"
                        )
                        
                        logging.warning(f"Non-runner found for Selection {runner_id}. Sending alert...")
                        send_telegram_alert(alert_msg)

except KeyboardInterrupt:
    logging.info("Stopping connection...")
    betfair_socket.stop()
    trading.logout()
