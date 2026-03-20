import os
import sys
import logging
import asyncio
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

ODDS_TRACKER = {}

def detect_spike(match_id, current_odds):
    prev = ODDS_TRACKER.get(match_id)

    # First time seeing this match
    if prev is None:
        ODDS_TRACKER[match_id] = current_odds
        return False

    # Calculate percentage change
    change = (current_odds - prev) / prev

    # Update stored odds
    ODDS_TRACKER[match_id] = current_odds

    # Spike threshold (20%)
    return change > 0.2

# python-telegram-bot v20+ (async)
try:
    from telegram import Update, Bot
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        JobQueue,
        filters
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("❌ python-telegram-bot not installed. Run: pip install python-telegram-bot")
    sys.exit(1)

# Optional: AI Analysis
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Optional: Environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= CONFIGURATION =================

@dataclass
class Config:
    """Configuration with environment variable fallbacks"""
    odds_api_key: str = field(default_factory=lambda: os.getenv('ODDS_API_KEY', ''))
    telegram_token: str = field(default_factory=lambda: os.getenv('TELEGRAM_TOKEN', ''))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv('TELEGRAM_CHAT_ID', ''))
    openai_api_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY', ''))
    
    bankroll: Decimal = field(default_factory=lambda: Decimal(os.getenv('BANKROLL', '10000')))
    max_stake: Decimal = field(default_factory=lambda: Decimal(os.getenv('MAX_STAKE', '500')))
    min_profit_percent: Decimal = Decimal('0.02')  # 2%
    max_exposure: Decimal = Decimal('0.05')  # 5% of bankroll
    
    poll_interval: int = 30  # seconds
    cooldown_minutes: int = 30
    
    enable_ai: bool = field(default_factory=lambda: os.getenv('ENABLE_AI', 'true').lower() == 'true')
    bookmakers: List[str] = field(default_factory=lambda: [
        "pinnacle", "betfair_ex_eu", "bet365", "williamhill", 
        "unibet", "betway", "1xbet"
    ])

# ================= DATA MODELS =================

@dataclass
class ArbitrageOpportunity:
    match_id: str
    home_team: str
    away_team: str
    bookmaker1: str
    bookmaker2: str
    odds1: Decimal
    odds2: Decimal
    profit_percent: Decimal
    stake1: Decimal
    stake2: Decimal
    total_stake: Decimal
    guaranteed_profit: Decimal
    detected_at: datetime
    ai_verdict: Optional[str] = None
    
    def to_message(self) -> str:
        emoji = "💰" if self.profit_percent >= 5 else "🎾"
        return f"""
{emoji} <b>TENNIS ARBITRAGE ALERT</b> {emoji}

<b>Match:</b> {self.home_team} vs {self.away_team}
<b>Profit:</b> +{self.profit_percent:.2f}%

<b>Odds:</b>
• {self.odds1} @ {self.bookmaker1}
• {self.odds2} @ {self.bookmaker2}

<b>Recommended Stakes:</b>
• <code>{self.stake1:.2f}</code> on {self.home_team}
• <code>{self.stake2:.2f}</code> on {self.away_team}
<b>Total:</b> <code>{self.total_stake:.2f}</code>
<b>Guaranteed Profit:</b> <code>{self.guaranteed_profit:.2f}</code>

<b>AI Analysis:</b> {self.ai_verdict or 'Disabled'}

<i>Detected: {self.detected_at.strftime('%H:%M:%S UTC')}</i>
        """

# ================= API CLIENT =================

class OddsAPIClient:
    """Client for The Odds API"""
    BASE_URL = "https://api.the-odds-api.com/v4"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
    
    def get_live_tennis(self) -> List[Dict]:
        """Fetch live tennis events"""
        url = f"{self.BASE_URL}/sports/tennis/odds"
        params = {
            'apiKey': self.api_key,
            'regions': 'eu,uk',
            'markets': 'h2h',
            'oddsFormat': 'decimal'
        }
        
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API Error: {e}")
            return []

# ================= ARBITRAGE CALCULATOR =================

class ArbitrageCalculator:
    @staticmethod
    def calculate_stakes(odds1: Decimal, odds2: Decimal, total: Decimal) -> Tuple[Decimal, Decimal, Decimal]:
        """Calculate optimal stakes for guaranteed profit"""
        if odds1 <= 1 or odds2 <= 1:
            return Decimal('0'), Decimal('0'), Decimal('0')
        
        prob1 = Decimal('1') / odds1
        prob2 = Decimal('1') / odds2
        total_prob = prob1 + prob2
        
        if total_prob >= 1:
            return Decimal('0'), Decimal('0'), Decimal('0')
        
        stake1 = (total * prob2 / total_prob).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        stake2 = (total * prob1 / total_prob).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        actual_total = stake1 + stake2
        profit = (stake1 * odds1) - actual_total
        
        return stake1, stake2, profit
    
    @staticmethod
    def find_opportunities(event: Dict, config: Config) -> List[ArbitrageOpportunity]:
        """Find arbitrage opportunities across bookmakers"""
        opportunities = []
        bookmakers = event.get('bookmakers', [])
        
        if len(bookmakers) < 2:
            return opportunities
        
        # Collect all odds
        all_odds = []
        for bm in bookmakers:
            if bm['key'] not in config.bookmakers:
                continue
            
            for market in bm.get('markets', []):
                if market['key'] != 'h2h':
                    continue
                
                outcomes = market.get('outcomes', [])
                if len(outcomes) != 2:
                    continue
                
                try:
                    home_price = outcomes[0].get('price')
                    away_price = outcomes[1].get('price')
                    if home_price is None or away_price is None:
                        continue
                    
                    all_odds.append({
                        'bookmaker': bm['title'],
                        'key': bm['key'],
                        'home': Decimal(str(home_price)),
                        'away': Decimal(str(away_price)),
                        'home_name': outcomes[0].get('name', 'Player 1'),
                        'away_name': outcomes[1].get('name', 'Player 2')
                    })
                except (InvalidOperation, ValueError):
                    continue
        
        # Compare combinations
        for i, odds1 in enumerate(all_odds):
            for odds2 in all_odds[i+1:]:
                # Scenario 1: odds1.home + odds2.away
                if odds1['home'] > 2 and odds2['away'] > 2:
                    opp = ArbitrageCalculator._create_opp(
                        event, odds1, odds2, 'home', 'away', config
                    )
                    if opp:
                        opportunities.append(opp)
                
                # Scenario 2: odds1.away + odds2.home
                if odds1['away'] > 2 and odds2['home'] > 2:
                    opp = ArbitrageCalculator._create_opp(
                        event, odds1, odds2, 'away', 'home', config
                    )
                    if opp:
                        opportunities.append(opp)
        
        return opportunities
    
    @staticmethod
    def _create_opp(event, odds1, odds2, side1, side2, config):
        """Create opportunity if profitable"""
        price1 = odds1[side1]
        price2 = odds2[side2]
        
        margin = (Decimal('1') / price1) + (Decimal('1') / price2)
        if margin >= 1:
            return None
        
        max_stake = min(config.max_stake, config.bankroll * config.max_exposure)
        stake1, stake2, profit = ArbitrageCalculator.calculate_stakes(price1, price2, max_stake)
        
        if profit <= 0:
            return None
        
        profit_pct = (profit / (stake1 + stake2)) * 100
        if profit_pct < config.min_profit_percent * 100:
            return None
        
        return ArbitrageOpportunity(
            match_id=event['id'],
            home_team=event.get('home_team', 'Unknown'),
            away_team=event.get('away_team', 'Unknown'),
            bookmaker1=odds1['bookmaker'],
            bookmaker2=odds2['bookmaker'],
            odds1=price1,
            odds2=price2,
            profit_percent=profit_pct,
            stake1=stake1,
            stake2=stake2,
            total_stake=stake1 + stake2,
            guaranteed_profit=profit,
            detected_at=datetime.utcnow()
        )

# ================= BOT STATE =================

class BotState:
    """Manages bot state and monitoring"""
    def __init__(self):
        self.config = Config()
        self.client = None
        self.monitoring = False
        self.opportunity_history: Dict[str, datetime] = {}
        self.stats = {
            'started_at': None,
            'opportunities_found': 0,
            'alerts_sent': 0,
            'cycles': 0,
            'errors': 0
        }
        self.ai_analyzer = None
        
        if self.config.odds_api_key:
            self.client = OddsAPIClient(self.config.odds_api_key)
        
        if OPENAI_AVAILABLE and self.config.enable_ai and self.config.openai_api_key:
            self.ai_analyzer = OpenAI(api_key=self.config.openai_api_key)
    
    def is_cooldown(self, match_id: str) -> bool:
        if match_id not in self.opportunity_history:
            return False
        return datetime.utcnow() - self.opportunity_history[match_id] < timedelta(minutes=self.config.cooldown_minutes)

# Global state
bot_state = BotState()

# ================= TELEGRAM HANDLERS =================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_text = """
🎾 <b>Welcome to Tennis Arbitrage Bot!</b>

I monitor live tennis matches for arbitrage opportunities where you can bet on both players with >2.0 odds and guarantee profit.

<b>Available Commands:</b>
/run - Start monitoring for opportunities
/stop - Stop monitoring
/status - Check monitoring status
/stats - View bot statistics
/settings - View current configuration
/test - Test API connection
/help - Show this help message

<b>⚠️ Important:</b>
• I only DETECT opportunities - I don't place bets
• You must act quickly when alerts arrive
• Requires The Odds API key (get free at the-odds-api.com)

Use /run to start monitoring!
    """
    await update.message.reply_html(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    await start_command(update, context)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check monitoring status"""
    if bot_state.monitoring:
        runtime = datetime.utcnow() - bot_state.stats['started_at'] if bot_state.stats['started_at'] else timedelta(0)
        text = f"""
✅ <b>Bot is ACTIVE</b>

Runtime: {runtime}
Cycles completed: {bot_state.stats['cycles']}
Opportunities found: {bot_state.stats['opportunities_found']}
Alerts sent: {bot_state.stats['alerts_sent']}
Errors: {bot_state.stats['errors']}
        """
    else:
        text = "❌ <b>Bot is STOPPED</b>\\n\\nUse /run to start monitoring."
    
    await update.message.reply_html(text)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current settings"""
    cfg = bot_state.config
    text = f"""
⚙️ <b>Current Configuration</b>

<b>Betting:</b>
• Bankroll: {cfg.bankroll}
• Max Stake: {cfg.max_stake}
• Min Profit: {cfg.min_profit_percent * 100}%
• Max Exposure: {cfg.max_exposure * 100}% of bankroll

<b>Monitoring:</b>
• Poll Interval: {cfg.poll_interval}s
• Cooldown: {cfg.cooldown_minutes} minutes
• Bookmakers: {', '.join(cfg.bookmakers[:4])}...

<b>Features:</b>
• AI Analysis: {'✅' if cfg.enable_ai and bot_state.ai_analyzer else '❌'}
• API Key: {'✅ Set' if cfg.odds_api_key else '❌ Missing'}
    """
    await update.message.reply_html(text)

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test API connection"""
    await update.message.reply_text("🔄 Testing API connection...")
    
    if not bot_state.client:
        await update.message.reply_text("❌ No API key configured! Set ODDS_API_KEY environment variable.")
        return
    
    try:
        events = bot_state.client.get_live_tennis()
        if events is not None:
            now = datetime.now(timezone.utc)

            live = sum(
                1 for e in events
                if datetime.fromisoformat(
                    e.get('commence_time', '').replace('Z', '+00:00')
                ) <= now + timedelta(minutes=5)
            ) if events else 0
            await update.message.reply_html(f"""
✅ <b>API Connection Successful!</b>

Retrieved {len(events)} tennis events
Live/In-progress: ~{live}

Bot is ready to monitor!
            """)
        else:
            await update.message.reply_text("❌ API returned no data. Check your API key.")
    except Exception as e:
        await update.message.reply_text(f"❌ API Error: {str(e)}")

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start monitoring"""
    if bot_state.monitoring:
        await update.message.reply_text("⚠️ Monitoring is already running! Use /status to check.")
        return
    
    if not bot_state.client:
        await update.message.reply_text("❌ Cannot start: No API key configured. Set ODDS_API_KEY.")
        return
    
    # Test connection first
    await update.message.reply_text("🔄 Testing connection...")
    try:
        test_data = bot_state.client.get_live_tennis()
        if test_data is None:
            await update.message.reply_text("❌ API connection failed. Check your API key.")
            return
    except Exception as e:
        await update.message.reply_text(f"❌ Connection error: {e}")
        return
    
    # Start monitoring
    bot_state.monitoring = True
    bot_state.stats['started_at'] = datetime.utcnow()
    bot_state.stats['cycles'] = 0
    
    await update.message.reply_html(f"""
✅ <b>Monitoring Started!</b>

Checking every {bot_state.config.poll_interval} seconds for arbitrage opportunities.
You will receive alerts when opportunities are found.

Use /stop to stop monitoring.
    """)
    
    # Start the background job
    context.job_queue.run_repeating(
        monitoring_job,
        interval=bot_state.config.poll_interval,
        first=5,
        name="arbitrage_monitor",
        chat_id=update.effective_chat.id
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop monitoring"""
    if not bot_state.monitoring:
        await update.message.reply_text("⚠️ Monitoring is not running.")
        return
    
    bot_state.monitoring = False
    
    # Stop the job
    current_jobs = context.job_queue.get_jobs_by_name("arbitrage_monitor")
    for job in current_jobs:
        job.schedule_removal()
    
    runtime = datetime.utcnow() - bot_state.stats['started_at'] if bot_state.stats['started_at'] else timedelta(0)
    
    await update.message.reply_html(f"""
🛑 <b>Monitoring Stopped</b>

Runtime: {runtime}
Total cycles: {bot_state.stats['cycles']}
Opportunities found: {bot_state.stats['opportunities_found']}
    """)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics"""
    stats = bot_state.stats
    runtime = datetime.utcnow() - stats['started_at'] if stats['started_at'] else timedelta(0)
    
    text = f"""
📊 <b>Bot Statistics</b>

<b>Session:</b>
• Status: {'🟢 Running' if bot_state.monitoring else '🔴 Stopped'}
• Runtime: {runtime}
• Cycles: {stats['cycles']}

<b>Performance:</b>
• Opportunities Found: {stats['opportunities_found']}
• Alerts Sent: {stats['alerts_sent']}
• Errors: {stats['errors']}

<b>Efficiency:</b>
• Opportunities/Cycle: {stats['opportunities_found'] / max(stats['cycles'], 1):.3f}
    """
    await update.message.reply_html(text)

# ================= MONITORING JOB =================

async def monitoring_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job that checks for arbitrage opportunities"""
    if not bot_state.monitoring:
        return
    
    try:
        bot_state.stats['cycles'] += 1
        
        # Fetch data
        events = bot_state.client.get_live_tennis()
        if not events:
            return
        
        opportunities = []
        
        for event in events:
            # Time filtering
            commence_str = event.get('commence_time', '')
            if not commence_str:
                continue
            
            try:
                from datetime import timezone

                commence = datetime.fromisoformat(commence_str.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)

                # Skip if not started or too old
                if commence > now + timedelta(minutes=5):
                    continue
                if commence < now - timedelta(hours=3):
                    continue
            except:
                continue
            
            # Find opportunities
            opps = ArbitrageCalculator.find_opportunities(event, bot_state.config)
            opportunities.extend(opps)
        
        # Process opportunities
        if opportunities:
            # Sort by profit
            opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
            
            for opp in opportunities:
                # ---- MOMENTUM SPIKE DETECTION ----
                if detect_spike(opp.match_id, float(opp.odds1)):
                    await context.bot.send_message(
                        chat_id=context.job.chat_id,
                        text=f"""
                        🔥 MOMENTUM SPIKE DETECTED

                        Match: {opp.home_team} vs {opp.away_team}
                        Odds: {opp.odds1} vs {opp.odds2}

                        👉 Possible overreaction — check immediately
                        """
                    )
                # Check cooldown
                if bot_state.is_cooldown(opp.match_id):
                    continue
                
                opp.ai_verdict = "Manual decision (AI disabled for speed)"
                
                # Send alert
                await context.bot.send_message(
                    chat_id=context.job.chat_id,
                    text=opp.to_message(),
                    parse_mode=ParseMode.HTML
                )
                
                bot_state.opportunity_history[opp.match_id] = datetime.utcnow()
                bot_state.stats['opportunities_found'] += 1
                bot_state.stats['alerts_sent'] += 1
                
                logger.info(f"Alert sent: {opp.home_team} vs {opp.away_team} (+{opp.profit_percent:.2f}%)")
    
    except Exception as e:
        bot_state.stats['errors'] += 1
        logger.error(f"Monitoring error: {e}", exc_info=True)

# ================= MAIN =================

def main():
    """Start the bot"""
    # Validate
    if not bot_state.config.telegram_token:
        print("❌ Error: TELEGRAM_TOKEN not set!")
        print("Get it from @BotFather on Telegram")
        sys.exit(1)
    
    # Build application
    application = ApplicationBuilder().token(
        bot_state.config.telegram_token
    ).concurrent_updates(True).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("test", test_command))
    
    # Start
    print("🎾 Tennis Arbitrage Bot starting...")
    print("Send /start to your bot on Telegram")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
