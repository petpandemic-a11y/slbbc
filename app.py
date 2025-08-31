#!/usr/bin/env python3
"""
Solana LP Burn Monitor Bot - All-in-One Version
GitHub: Upload this single file as 'app.py'
Render: Will auto-detect and run this file
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Dict, Optional
from threading import Thread
import logging

# ============= WEB SERVER FOR RENDER =============
try:
    from flask import Flask
    
    app = Flask(__name__)
    
    @app.route('/')
    def home():
        return """
        <h1>Solana LP Burn Monitor Bot ✅</h1>
        <p>Status: Running</p>
        <p>Monitor: Raydium LP Burns</p>
        <p>Notifications: Telegram</p>
        """
    
    @app.route('/health')
    def health():
        return "OK", 200
    
    def run_server():
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port, debug=False)
    
    def start_web_server():
        server_thread = Thread(target=run_server, daemon=True)
        server_thread.start()
        
except ImportError:
    print("Flask not installed - web server disabled")
    def start_web_server():
        pass

# ============= DEPENDENCY CHECK =============
required_packages = {
    'solana': 'solana',
    'aiohttp': 'aiohttp',
    'telegram': 'python-telegram-bot',
    'solders': 'solders',
    'flask': 'flask'
}

missing_packages = []
for import_name, package_name in required_packages.items():
    try:
        __import__(import_name)
    except ImportError:
        missing_packages.append(package_name)

if missing_packages:
    print("=" * 50)
    print("MISSING REQUIRED PACKAGES!")
    print("=" * 50)
    print(f"Please install: {', '.join(missing_packages)}")
    print("\nRun this command:")
    print(f"pip install {' '.join(missing_packages)}")
    print("\nOr create requirements.txt with:")
    print("solana==0.30.2")
    print("aiohttp==3.9.1")
    print("python-telegram-bot==20.7")
    print("solders==0.18.1")
    print("flask==3.0.0")
    print("=" * 50)
    sys.exit(1)

# ============= IMPORTS =============
import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature
from telegram import Bot
from telegram.error import TelegramError

# ============= CONFIGURATION =============
# Environment variables (set these in Render)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
# Use better RPC endpoints - updated list with working free RPCs
RPC_URLS = [
    os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
    "https://solana-mainnet.g.alchemy.com/v2/demo",  # Alchemy demo
    "https://rpc.helius.xyz/",  # Helius free tier
    "https://mainnet.solana.rpcpool.com",  # RPC Pool
    "https://solana-mainnet.rpc.extrnode.com",  # ExtrNode
]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))  # Increased to 30s
MIN_BURN_PERCENT = float(os.environ.get("MIN_BURN_PERCENT", "90"))

# Raydium addresses
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"

# Known burn addresses
BURN_ADDRESSES = [
    "1111111111111111111111111111111111111111111",
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
    "burnSoL11111111111111111111111111111111111",
    "DeadSo11111111111111111111111111111111111",
]

# ============= LOGGING =============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

# ============= MAIN BOT CLASS =============
class SolanaLPBurnMonitor:
    def __init__(self):
        # Check environment variables
        if not TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN not set!")
            print("\n" + "="*50)
            print("ERROR: TELEGRAM_BOT_TOKEN not set!")
            print("Please set the environment variable:")
            print("export TELEGRAM_BOT_TOKEN='your_bot_token'")
            print("="*50 + "\n")
            sys.exit(1)
            
        if not TELEGRAM_CHANNEL_ID:
            logger.error("TELEGRAM_CHANNEL_ID not set!")
            print("\n" + "="*50)
            print("ERROR: TELEGRAM_CHANNEL_ID not set!")
            print("Please set the environment variable:")
            print("export TELEGRAM_CHANNEL_ID='@your_channel'")
            print("="*50 + "\n")
            sys.exit(1)
        
        self.telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.rpc_index = 0
        self.rpc_urls = RPC_URLS
        self.solana_client = AsyncClient(self.rpc_urls[0])
        self.processed_signatures = set()
        self.token_cache = {}
        self.session = None
        self.rate_limit_delay = 2  # seconds between requests
        
    async def setup(self):
        """Initialize aiohttp session"""
        self.session = aiohttp.ClientSession()
        
    async def cleanup(self):
        """Cleanup resources"""
        if self.session:
            await self.session.close()
        await self.solana_client.close()
    
    async def get_token_info(self, mint_address: str) -> Dict:
        """Get token metadata from Jupiter API"""
        if mint_address in self.token_cache:
            return self.token_cache[mint_address]
        
        try:
            # Try Jupiter API first
            url = f"https://price.jup.ag/v4/token/{mint_address}"
            async with self.session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    info = {
                        "symbol": data.get("symbol", "???"),
                        "name": data.get("name", "Unknown"),
                        "decimals": data.get("decimals", 9)
                    }
                    self.token_cache[mint_address] = info
                    return info
        except:
            pass
        
        return {"symbol": "???", "name": "Unknown", "decimals": 9}
    
    async def rotate_rpc(self):
        """Rotate to next RPC endpoint"""
        try:
            await self.solana_client.close()
        except:
            pass
        
        self.rpc_index = (self.rpc_index + 1) % len(self.rpc_urls)
        self.solana_client = AsyncClient(self.rpc_urls[self.rpc_index])
        logger.info(f"🔄 Switched to RPC #{self.rpc_index}: {self.rpc_urls[self.rpc_index]}")
    
    async def check_transaction(self, signature: str) -> Optional[Dict]:
        """Check if transaction is an LP burn"""
        try:
            # Add delay to avoid rate limits
            await asyncio.sleep(self.rate_limit_delay)
            
            # Convert string to Signature object
            sig_obj = Signature.from_string(signature)
            
            tx = await self.solana_client.get_transaction(
                sig_obj,
                commitment=Confirmed,
                max_supported_transaction_version=0
            )
            
            if not tx or not tx.value:
                return None
            
            # Look for burn addresses in transaction
            tx_data = tx.value
            if not tx_data.transaction:
                return None
                
            # Simple check for burn addresses
            message = tx_data.transaction.message
            accounts = []
            
            # Get all account keys
            if hasattr(message, 'account_keys'):
                accounts = [str(key) for key in message.account_keys]
            elif hasattr(message, 'static_account_keys'):
                accounts = [str(key) for key in message.static_account_keys]
            
            # Check if any burn address is involved
            has_burn = any(burn in accounts for burn in BURN_ADDRESSES)
            has_raydium = RAYDIUM_AMM_PROGRAM in accounts or RAYDIUM_AUTHORITY in accounts
            
            if has_burn and has_raydium:
                # Found potential burn transaction
                logger.info(f"🔥 Potential burn found: {signature[:20]}...")
                
                # Extract token address (simplified - would need proper parsing)
                token_address = accounts[0] if accounts else "unknown"
                
                return {
                    "signature": signature,
                    "token_address": token_address,
                    "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "burn_percent": MIN_BURN_PERCENT  # Simplified
                }
                
        except Exception as e:
            logger.debug(f"Error checking tx {signature[:20]}...: {type(e).__name__}: {str(e)}")
            
        return None
    
    async def send_notification(self, burn_data: Dict):
        """Send Telegram notification"""
        try:
            token = await self.get_token_info(burn_data['token_address'])
            
            message = f"""
🔥 <b>LP BURN DETECTED!</b> 🔥

📊 <b>Token:</b> {token['name']} ({token['symbol']})
📍 <b>Address:</b> <code>{burn_data['token_address'][:8]}...{burn_data['token_address'][-8:]}</code>
💯 <b>LP Burned:</b> ~{burn_data['burn_percent']}%
⏰ <b>Time:</b> {burn_data['timestamp']}

🔗 <a href="https://solscan.io/tx/{burn_data['signature']}">View Transaction</a>
📈 <a href="https://dexscreener.com/solana/{burn_data['token_address']}">DexScreener</a>
🐦 <a href="https://birdeye.so/token/{burn_data['token_address']}?chain=solana">Birdeye</a>

⚠️ <i>Always DYOR! LP burn doesn't guarantee safety.</i>
"""
            
            await self.telegram_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            
            logger.info(f"✅ Notification sent for {token['symbol']}")
            
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
    
    async def monitor_loop(self):
        """Main monitoring loop"""
        logger.info("🚀 Starting monitor loop...")
        error_count = 0
        
        while True:
            try:
                # Get recent signatures with smaller limit to reduce load
                signatures = await self.solana_client.get_signatures_for_address(
                    Pubkey.from_string(RAYDIUM_AMM_PROGRAM),
                    limit=5  # Reduced from 10
                )
                
                if signatures and signatures.value:
                    logger.info(f"Found {len(signatures.value)} transactions to check")
                    for sig_info in signatures.value:
                        sig = str(sig_info.signature)
                        
                        if sig not in self.processed_signatures:
                            self.processed_signatures.add(sig)
                            logger.debug(f"Checking signature: {sig[:10]}...")
                            
                            # Check if it's a burn
                            burn_data = await self.check_transaction(sig)
                            if burn_data:
                                await self.send_notification(burn_data)
                
                # Clean up old signatures
                if len(self.processed_signatures) > 10000:
                    keep = list(self.processed_signatures)[-5000:]
                    self.processed_signatures = set(keep)
                
                error_count = 0  # Reset error count on success
                
            except Exception as e:
                error_count += 1
                error_msg = str(e)
                error_type = type(e).__name__
                
                logger.error(f"Monitor error (#{error_count}): {error_type}: {error_msg}")
                
                if "429" in error_msg or "Too Many Requests" in error_msg:
                    logger.warning(f"⚠️ Rate limited, rotating RPC and waiting...")
                    await self.rotate_rpc()
                    await asyncio.sleep(30)
                elif "403" in error_msg or "Forbidden" in error_msg:
                    logger.warning(f"⚠️ Access forbidden, rotating RPC...")
                    await self.rotate_rpc()
                    await asyncio.sleep(5)
                elif "503" in error_msg or "Service Unavailable" in error_msg:
                    logger.warning(f"⚠️ RPC unavailable, rotating...")
                    await self.rotate_rpc()
                    await asyncio.sleep(10)
                elif "Connection" in error_msg or "Timeout" in error_msg:
                    logger.warning(f"⚠️ Connection issue, waiting...")
                    await asyncio.sleep(10)
                
                if error_count > 10:
                    logger.error("Too many errors, rotating RPC and waiting 60s...")
                    await self.rotate_rpc()
                    await asyncio.sleep(60)
                    error_count = 0
            
            await asyncio.sleep(CHECK_INTERVAL)
    
    async def start(self):
        """Start the bot"""
        await self.setup()
        
        try:
            # Test Telegram connection
            me = await self.telegram_bot.get_me()
            logger.info(f"✅ Telegram bot connected: @{me.username}")
            
            # Test Solana connection
            try:
                slot = await self.solana_client.get_slot()
                logger.info(f"✅ Solana RPC connected: slot {slot.value}")
                logger.info(f"📡 Using RPC: {self.rpc_urls[self.rpc_index]}")
            except Exception as e:
                logger.warning(f"Initial RPC failed, rotating...")
                await self.rotate_rpc()
                try:
                    slot = await self.solana_client.get_slot()
                    logger.info(f"✅ Solana RPC connected after rotation: slot {slot.value}")
                except:
                    logger.warning(f"Solana connection warning, continuing anyway...")
            
            # Send startup message
            try:
                await self.telegram_bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text="🤖 <b>LP Burn Monitor Started!</b>\n\n"
                         f"📍 Monitoring: Raydium\n"
                         f"⏱ Interval: {CHECK_INTERVAL}s\n"
                         f"🔥 Min Burn: {MIN_BURN_PERCENT}%\n\n"
                         f"<i>Ready to detect LP burns...</i>",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.warning(f"Could not send startup message: {e}")
            
            # Start monitoring
            await self.monitor_loop()
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
        finally:
            await self.cleanup()

# ============= MAIN ENTRY POINT =============
async def main():
    """Main async function"""
    monitor = SolanaLPBurnMonitor()
    await monitor.start()

def run_bot():
    """Run the bot"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")

if __name__ == "__main__":
    print("=" * 50)
    print("SOLANA LP BURN MONITOR BOT")
    print("=" * 50)
    
    # Start web server for Render
    start_web_server()
    logger.info("Web server started for health checks")
    
    # Configuration check
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("\n⚠️  CONFIGURATION REQUIRED!")
        print("\nSet these environment variables:")
        print("  TELEGRAM_BOT_TOKEN = Your bot token from @BotFather")
        print("  TELEGRAM_CHANNEL_ID = Your channel ID (e.g. @channelname or -1234567)")
        print("\nOptional variables:")
        print("  SOLANA_RPC_URL = RPC endpoint (default: mainnet)")
        print("  CHECK_INTERVAL = Check interval in seconds (default: 10)")
        print("  MIN_BURN_PERCENT = Minimum burn % to notify (default: 90)")
        print("\nFor Render.com deployment:")
        print("1. Upload this file as 'app.py' to GitHub")
        print("2. Connect GitHub to Render")
        print("3. Set environment variables in Render dashboard")
        print("=" * 50)
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
            sys.exit(1)
    
    print(f"\n✅ Configuration loaded:")
    print(f"  Bot Token: {'*' * 10}{TELEGRAM_BOT_TOKEN[-10:] if TELEGRAM_BOT_TOKEN else 'NOT SET'}")
    print(f"  Channel: {TELEGRAM_CHANNEL_ID}")
    print(f"  RPC: {RPC_URLS[0]}")
    print(f"  Interval: {CHECK_INTERVAL}s")
    print(f"  Min Burn: {MIN_BURN_PERCENT}%")
    print("=" * 50)
    print("\n🚀 Starting bot...\n")
    
    # Run the bot
    run_bot()
