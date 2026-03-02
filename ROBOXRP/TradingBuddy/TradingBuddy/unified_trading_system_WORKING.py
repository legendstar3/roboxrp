#!/usr/bin/env python3
"""
ROBOXRP PRO — OKX Spot Trading System v13.2
Multi-Timeframe EMA/RSI strategy with trailing stops and risk management.
"""

import logging
import asyncio
import time
import random
import math
import concurrent.futures
import threading
import json
import os
import socket
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
from decimal import Decimal, ROUND_DOWN

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    pass

# Configure logging with rotation (max 5MB per file, keep 3 backups)
from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('robocrp_pro_v13.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TradeAction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

@dataclass
class QuantumTradingSignal:
    signal_id: int
    symbol: str
    action: str
    entry_price: float
    targets: List[float]
    stop_loss: float
    confidence: float
    reasoning: str


def _maybe_load_okx_keys_from_file() -> None:
    try:
        # Do not override env if the user already configured it
        if (os.getenv("OKX_API_KEY") or "").strip() and (os.getenv("OKX_API_SECRET") or "").strip():
            return

        here = os.path.abspath(os.path.dirname(__file__))
        repo_root = os.path.abspath(os.path.join(here, "..", ".."))
        info_path = os.path.join(repo_root, "OKX info.txt")
        if not os.path.isfile(info_path):
            return

        try:
            raw = ""
            with open(info_path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read() or ""
        except Exception:
            return

        lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if (ln or "").strip()]
        if not lines:
            return

        api_key = None
        api_secret = None

        # OKX API key commonly looks like a UUID
        for ln in lines:
            if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", ln):
                api_key = ln
                break

        # Secret key often is a 32+ hex string in the export
        for ln in lines:
            if re.fullmatch(r"[0-9A-Fa-f]{32,}", ln):
                api_secret = ln
                break

        if api_key and not (os.getenv("OKX_API_KEY") or "").strip():
            os.environ["OKX_API_KEY"] = str(api_key)
        if api_secret and not (os.getenv("OKX_API_SECRET") or "").strip():
            os.environ["OKX_API_SECRET"] = str(api_secret)
    except Exception:
        return

@dataclass
class PositionSize:
    position_size_percent: float
    risk_amount: float
    max_loss: float

class UnifiedTradingSystem:
    """FIXED Trading System - WORKING VERSION"""

    _PERF_STATS_EMPTY: Dict[str, object] = {
        "profit_factor": 0.0, "expectancy": 0.0, "max_drawdown": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
        "win_count": 0, "loss_count": 0, "avg_trade_duration_sec": 0.0,
        "sharpe_ratio": 0.0, "equity_curve": [],
    }
    
    def __init__(self):
        try:
            from dotenv import load_dotenv
            root_env = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
            cwd_env = os.path.join(os.getcwd(), ".env")

            loaded_any = False
            try:
                if os.path.exists(root_env):
                    # Root .env should be the source of truth for the bot.
                    # Using override=True prevents stale system/session env vars from breaking OKX auth.
                    load_dotenv(dotenv_path=root_env, override=True)
                    logger.info("Loaded .env from: %s", root_env)
                    loaded_any = True
            except Exception:
                pass

            try:
                allow_local = str(os.getenv("ALLOW_LOCAL_ENV", "false")).strip().lower() in {"1", "true", "yes", "on"}
                if allow_local and os.path.exists(cwd_env) and os.path.abspath(cwd_env) != os.path.abspath(root_env):
                    # Optional supplement with any local overrides (without clobbering root)
                    load_dotenv(dotenv_path=cwd_env, override=False)
                    logger.info("Loaded .env from: %s", cwd_env)
                    loaded_any = True
            except Exception:
                pass

            if not loaded_any:
                load_dotenv(override=False)
        except Exception:
            pass

        try:
            _maybe_load_okx_keys_from_file()
        except Exception:
            pass

        self.active_signals = {}
        try:
            self.max_concurrent_trades = int(os.getenv("MAX_CONCURRENT_TRADES", "1") or "1")
        except Exception:
            self.max_concurrent_trades = 1
        self.total_trades = 0
        self.winning_trades = 0
        self.daily_pnl = 0.0
        self.total_pnl = 0.0
        self._daily_pnl_date = datetime.now().date()
        self.session_start = datetime.now()
        self._session_start_ts = time.time()
        self.running = True
        self._price_momentum = {}

        # Fee-aware PnL (top bots always account for exchange fees)
        try:
            self._taker_fee_pct = float(os.getenv("OKX_TAKER_FEE_PCT", "0.10") or "0.10")
        except Exception:
            self._taker_fee_pct = 0.10

        # Consecutive loss streak protection (top bots pause after N losses, then resume)
        try:
            self._max_consecutive_losses = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5") or "5")
        except Exception:
            self._max_consecutive_losses = 5
        try:
            self._loss_streak_cooldown_sec = int(os.getenv("LOSS_STREAK_COOLDOWN_SEC", "1800") or "1800")
        except Exception:
            self._loss_streak_cooldown_sec = 1800
        self._consecutive_losses = 0
        self._consecutive_wins = 0
        self._loss_streak_paused_ts = 0.0
        self._dynamic_stake_enabled = str(os.getenv("OKX_DYNAMIC_STAKE", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}

        # Max portfolio heat — total USD exposure cap (top bots limit total exposure)
        try:
            self._max_portfolio_heat_usd = float(os.getenv("MAX_PORTFOLIO_HEAT_USD", "0") or "0")
        except Exception:
            self._max_portfolio_heat_usd = 0.0

        # Session equity tracking + max drawdown protection (Freqtrade/Jesse standard)
        # Tracks peak balance within session. If drawdown from peak exceeds threshold,
        # pause all new trades until balance recovers or session resets.
        self._session_peak_balance = 0.0  # highest balance seen this session
        self._session_start_balance = 0.0  # balance at session start
        try:
            self._max_drawdown_pct = float(os.getenv("MAX_DRAWDOWN_PCT", "20") or "20")
        except Exception:
            self._max_drawdown_pct = 20.0  # default: pause at 20% drawdown from peak
        self._drawdown_paused = False
        self._drawdown_pause_ts = 0.0

        self._signal_history = deque(maxlen=500)
        self._trade_history = deque(maxlen=500)
        self._verification_audit = deque(maxlen=2000)
        self._data_lock = threading.RLock()

        self._signal_id_state_path = os.path.join(os.path.dirname(__file__), "signal_id_state.json")

        self._reset_state_if_requested()
        self._next_signal_id = self._load_next_signal_id()

        self._candles: Dict[str, deque] = {}
        self._last_prices: Dict[str, float] = {}
        self._last_prices_ts: Dict[str, float] = {}  # symbol -> epoch when price was last fetched live
        try:
            self._stale_price_max_age_sec = float(os.getenv("OKX_STALE_PRICE_MAX_AGE_SEC", "120") or "120")
        except Exception:
            self._stale_price_max_age_sec = 120.0
        self._stale_price_log_ts: Dict[str, float] = {}  # rate-limit stale price warnings
        self._candle_tf_sec = 10
        self._market_thread = None
        self._dashboard_thread = None
        self._dashboard_server = None
        self.dashboard_port = 5001

        self._dashboard_summary_cache: Dict[str, object] = {"status": "...", "ts": int(time.time())}
        self._dashboard_candles_cache: Dict[Tuple[str, int], Dict[str, object]] = {}
        self._dashboard_active_trades_cache: Dict[str, object] = {"trades": []}
        self._dashboard_signals_cache: Dict[str, object] = {"signals": []}
        self._dashboard_trade_history_cache: Dict[str, object] = {"trades": []}
        self._dashboard_audit_cache: Dict[str, object] = {"audit": []}
        self._dashboard_symbols_cache: Dict[str, object] = {"symbols": [], "items": [], "status": "...", "ts": int(time.time())}
        try:
            self._dashboard_symbols_cache_ttl_sec = float(os.getenv("DASHBOARD_SYMBOLS_CACHE_TTL_SEC", "15") or "15")
        except Exception:
            self._dashboard_symbols_cache_ttl_sec = 15.0

        self._last_cycle_ts: float = 0.0
        self._last_cycle_error: str = ""
        self._last_cycle_error_ts: float = 0.0
        self._last_cycle_stage: str = ""
        self._last_cycle_stage_ts: float = 0.0

        self._signal_scan_symbol: str = ""
        self._signal_scan_index: int = 0
        self._signal_scan_total: int = 0
        self._signal_scan_last_ohlcv_ms: Dict[str, float] = {}
        self._signal_scan_last_skip: str = ""
        self._signal_scan_last_raw: int = 0
        self._signal_scan_last_filtered: int = 0
        self._signal_scan_last_gates: Dict[str, object] = {}
        self._signal_scan_skip_dist: Dict[str, int] = {}
        self._cycle_count: int = 0
        self._cycle_times: List[float] = []  # last N cycle durations for avg

        try:
            self._signal_gen_timeout_sec = float(os.getenv("SIGNAL_GEN_TIMEOUT_SEC", "120") or "120")
        except Exception:
            self._signal_gen_timeout_sec = 120.0

        try:
            self._kraken_call_timeout_sec = float(os.getenv("OKX_CALL_TIMEOUT_SEC", "10") or "10")
        except Exception:
            self._kraken_call_timeout_sec = 10.0

        try:
            self._kraken_symbols_per_cycle = int(os.getenv("OKX_SYMBOLS_PER_CYCLE", "86") or "86")
        except Exception:
            self._kraken_symbols_per_cycle = 86
        self._kraken_universe_cursor = 0

        try:
            self._ccxt_pool_workers = int(os.getenv("CCXT_POOL_WORKERS", "4") or "4")
        except Exception:
            self._ccxt_pool_workers = 4
        self._ccxt_exec_lock = threading.Lock()
        self._ccxt_timeout_failures = 0
        self._ccxt_executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(self._ccxt_pool_workers)))

        try:
            self._dash_pool_workers = int(os.getenv("DASH_POOL_WORKERS", "2") or "2")
        except Exception:
            self._dash_pool_workers = 2
        self._dash_executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(self._dash_pool_workers)))

        self._kraken_trading_blocked_reason: str = ""

        self._kraken_readiness_checked: bool = False
        self._kraken_last_readiness: Dict[str, object] = {}
        self._kraken_last_readiness_ts: float = 0.0
        try:
            self._kraken_readiness_refresh_sec = float(os.getenv("OKX_READINESS_REFRESH_SEC", "120") or "120")
        except Exception:
            self._kraken_readiness_refresh_sec = 120.0

        self._telegram_bot_token = None
        self._telegram_chat_id = None
        self._telegram_failure_count = 0
        self._telegram_last_error = ""
        self._telegram_disabled_for_session = False
        self._telegram_startup_test_sent = False

        # Load Telegram config after fields are initialized (avoid overwriting loaded values)
        try:
            self._load_telegram_config()
        except Exception:
            pass

        self._kraken = None
        self._kraken_ticker_cache: Dict[str, Tuple[float, float]] = {}  # pair -> (ts, last)
        self._kraken_ticker_cache_ttl_sec = float(os.getenv("OKX_TICKER_CACHE_TTL_SEC", "2") or "2")
        self._kraken_balance_cache: Optional[Dict[str, object]] = None
        self._kraken_balance_cache_ts: float = 0.0
        self._kraken_balance_cache_ttl_sec = float(os.getenv("OKX_BALANCE_CACHE_TTL_SEC", "10") or "10")
        self._kraken_live_prices_enabled = str(os.getenv("OKX_ENABLE_LIVE_PRICES", "false")).strip().lower() in {"1", "true", "yes", "on"}
        self._kraken_trading_enabled = str(os.getenv("OKX_ENABLE_TRADING", "false")).strip().lower() in {"1", "true", "yes", "on"}
        self._kraken_init_attempted = False

        self._kraken_trading_mode = str(os.getenv("OKX_TRADING_MODE", "paper")).strip().lower()  # paper|live
        try:
            self._kraken_limit_offset_pct = float(os.getenv("OKX_LIMIT_OFFSET_PCT", "0.10") or "0.10")
        except Exception:
            self._kraken_limit_offset_pct = 0.10
        try:
            self._kraken_limit_timeout_sec = int(os.getenv("OKX_LIMIT_TIMEOUT_SEC", "15") or "15")
        except Exception:
            self._kraken_limit_timeout_sec = 15
        self._kraken_fallback_market = str(os.getenv("OKX_ORDER_FALLBACK_MARKET", "false")).strip().lower() in {"1", "true", "yes", "on"}

        # ── Patient Orders System (modern bot standard) ──
        # Instead of cancelling limit orders after 15-20s and falling back to market (bad fill),
        # patient orders stay open for hours/days at a GOOD price (below ask for BUY).
        # The bot monitors pending orders each cycle and amends price if market drifts too far.
        self._patient_orders_enabled = str(os.getenv("OKX_PATIENT_ORDERS", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
        try:
            self._patient_order_max_age_sec = int(float(os.getenv("OKX_PATIENT_ORDER_MAX_SEC", "1800") or "1800"))
        except Exception:
            self._patient_order_max_age_sec = 1800  # 30 minutes default
        try:
            self._patient_order_amend_pct = float(os.getenv("OKX_PATIENT_AMEND_PCT", "1.5") or "1.5")
        except Exception:
            self._patient_order_amend_pct = 1.5  # amend if price drifts >1.5% from order
        try:
            self._patient_price_mode = str(os.getenv("OKX_PATIENT_PRICE_MODE", "mid") or "mid").strip().lower()
        except Exception:
            self._patient_price_mode = "mid"  # mid = midpoint(bid,ask), bid = at bid, ask = at ask
        # Pending orders: symbol -> {order_id, pair, side, amount, price, signal, placed_ts, amend_count}
        self._pending_orders: Dict[str, Dict[str, object]] = {}
        try:
            self._kraken_max_spread_pct = float(os.getenv("OKX_MAX_SPREAD_PCT", "1.0") or "1.0")
        except Exception:
            self._kraken_max_spread_pct = 1.0
        try:
            self._kraken_max_entry_drift_pct = float(os.getenv("OKX_MAX_ENTRY_DRIFT_PCT", "0.8") or "0.8")
        except Exception:
            self._kraken_max_entry_drift_pct = 0.8
        try:
            self._kraken_position_usd = float(os.getenv("OKX_POSITION_USD", "10") or "10")
        except Exception:
            self._kraken_position_usd = 10.0

        # Live warmup sizing: start smaller, then promote to target size after warmup.
        # These are applied only when OKX_TRADING_MODE=live.
        try:
            self._kraken_live_warmup_minutes = float(os.getenv("OKX_LIVE_WARMUP_MINUTES", "0") or "0")
        except Exception:
            self._kraken_live_warmup_minutes = 0.0
        try:
            self._kraken_live_warmup_usd = float(os.getenv("OKX_LIVE_WARMUP_USD", "25") or "25")
        except Exception:
            self._kraken_live_warmup_usd = 25.0
        try:
            self._kraken_live_target_usd = float(os.getenv("OKX_LIVE_TARGET_USD", "15") or "15")
        except Exception:
            self._kraken_live_target_usd = 15.0

        self._kraken_live_start_ts = time.time()
        self._kraken_live_promoted = False
        try:
            self._kraken_fee_buffer_pct = float(os.getenv("OKX_FEE_BUFFER_PCT", "0.30") or "0.30")
        except Exception:
            self._kraken_fee_buffer_pct = 0.30

        # Compound growth: position size = X% of real balance (reinvests profits automatically)
        # Set OKX_COMPOUND_POSITION_PCT=30 to use 30% of balance per trade.
        # 0 = disabled (uses fixed OKX_LIVE_TARGET_USD instead)
        try:
            self._compound_position_pct = float(os.getenv("OKX_COMPOUND_POSITION_PCT", "75") or "75")
        except Exception:
            self._compound_position_pct = 75.0
        try:
            self._compound_min_usd = float(os.getenv("OKX_COMPOUND_MIN_USD", "5") or "5")
        except Exception:
            self._compound_min_usd = 5.0
        try:
            self._compound_max_usd = float(os.getenv("OKX_COMPOUND_MAX_USD", "0") or "0")
        except Exception:
            self._compound_max_usd = 0.0

        # Kraken universe + OHLCV cache (for multi-timeframe strategy)
        self._kraken_universe_symbols: List[str] = []
        self._kraken_ohlcv_cache: Dict[Tuple[str, str, int], Tuple[float, List[List[float]]]] = {}
        self._kraken_universe_last_refresh_ts: float = 0.0
        try:
            self._kraken_universe_refresh_sec = float(os.getenv("OKX_UNIVERSE_REFRESH_SEC", "3600") or "3600")
        except Exception:
            self._kraken_universe_refresh_sec = 3600.0
        try:
            self._kraken_ohlcv_cache_ttl_sec = float(os.getenv("OKX_OHLCV_CACHE_TTL_SEC", "20") or "20")
        except Exception:
            self._kraken_ohlcv_cache_ttl_sec = 20.0

        # Kraken MTF strategy toggles
        self._kraken_mtf_signals_enabled = str(os.getenv("OKX_MTF_SIGNALS", "true")).strip().lower() in {"1", "true", "yes", "on"}
        try:
            self._kraken_mtf_rsi_period = int(os.getenv("OKX_MTF_RSI_PERIOD", "14") or "14")
        except Exception:
            self._kraken_mtf_rsi_period = 14

        try:
            self._kraken_mtf_rsi_buy_max = float(os.getenv("OKX_MTF_RSI_BUY_MAX", "42") or "42")
        except Exception:
            self._kraken_mtf_rsi_buy_max = 42.0
        try:
            self._kraken_mtf_rsi_sell_min = float(os.getenv("OKX_MTF_RSI_SELL_MIN", "65") or "65")
        except Exception:
            self._kraken_mtf_rsi_sell_min = 65.0

        self._kraken_mtf_require_trend = str(os.getenv("OKX_MTF_REQUIRE_TREND", "true")).strip().lower() in {"1", "true", "yes", "on"}
        self._kraken_mtf_require_confirm = str(os.getenv("OKX_MTF_REQUIRE_CONFIRM", "true")).strip().lower() in {"1", "true", "yes", "on"}
        try:
            self._kraken_mtf_fast_ema = int(os.getenv("OKX_MTF_FAST_EMA", "20") or "20")
        except Exception:
            self._kraken_mtf_fast_ema = 20
        try:
            self._kraken_mtf_slow_ema = int(os.getenv("OKX_MTF_SLOW_EMA", "50") or "50")
        except Exception:
            self._kraken_mtf_slow_ema = 50
        try:
            self._kraken_mtf_min_candles = int(os.getenv("OKX_MTF_MIN_CANDLES", "60") or "60")
        except Exception:
            self._kraken_mtf_min_candles = 60

        try:
            self._kraken_mtf_min_candles_1h = int(os.getenv("OKX_MTF_MIN_CANDLES_1H", "60") or "60")
        except Exception:
            self._kraken_mtf_min_candles_1h = 60

        self._kraken_strict_execution = str(os.getenv("OKX_STRICT_EXECUTION", "true")).strip().lower() in {"1", "true", "yes", "on"}
        self._kraken_paper_check_balance = str(os.getenv("OKX_PAPER_CHECK_BALANCE", "false")).strip().lower() in {"1", "true", "yes", "on"}

        # Performance: risk-reward ratio filter
        try:
            self._min_rr_ratio = float(os.getenv("OKX_MIN_RR_RATIO", "1.5") or "1.5")
        except Exception:
            self._min_rr_ratio = 1.5

        # Performance: cooldown after SL hit (prevents revenge trading same symbol)
        try:
            self._sl_cooldown_sec = float(os.getenv("OKX_SL_COOLDOWN_SEC", "300") or "300")
        except Exception:
            self._sl_cooldown_sec = 300.0
        self._sl_cooldown_map: Dict[str, float] = {}  # symbol -> timestamp of last SL

        try:
            self._max_trade_sec = int(float(os.getenv("OKX_MAX_TRADE_SEC", "14400") or "14400"))
        except Exception:
            self._max_trade_sec = 14400

        # Liquidity cache: skip dead symbols instantly (no OHLCV fetch) for N minutes.
        # OKX EEA EUR pairs: most illiquid symbols stay dead for hours. 15min avoids
        # wasting 3 OHLCV calls/symbol/cycle on symbols that won't produce signals.
        self._illiquid_cache: Dict[str, float] = {}  # symbol -> timestamp when marked illiquid
        try:
            self._illiquid_cache_ttl = float(os.getenv("OKX_ILLIQUID_CACHE_SEC", "900") or "900")
        except Exception:
            self._illiquid_cache_ttl = 900.0  # 15 minutes default

        # Spot buy-only mode: on spot exchange you can only SELL if you hold the token.
        # With only USDC balance and no token holdings, only BUY signals are executable.
        self._spot_buy_only = str(os.getenv("OKX_SPOT_BUY_ONLY", "true")).strip().lower() in {"1", "true", "yes", "on"}

        # Performance: volume confirmation
        self._volume_confirm_enabled = str(os.getenv("OKX_VOLUME_CONFIRM", "true")).strip().lower() in {"1", "true", "yes", "on"}
        try:
            self._volume_min_ratio = float(os.getenv("OKX_VOLUME_MIN_RATIO", "0.10") or "0.10")
        except Exception:
            self._volume_min_ratio = 0.10

        if self._kraken_live_prices_enabled or self._kraken_trading_enabled:
            logger.info(
                "OKX integration enabled (live_prices=%s, trading=%s)",
                self._kraken_live_prices_enabled,
                self._kraken_trading_enabled,
            )

        self._min_signal_confidence = float(os.getenv("MIN_SIGNAL_CONFIDENCE", "86") or "86")
        self._min_tp1_pips = float(os.getenv("MIN_TP1_PIPS", "40") or "40")
        self._min_tp1_pct = float(os.getenv("MIN_TP1_PCT", "0.7") or "0.7")
        self._max_signals_per_cycle = int(os.getenv("MAX_SIGNALS_PER_CYCLE", str(self.max_concurrent_trades)) or str(self.max_concurrent_trades))
        self._daily_stop_usd = float(os.getenv("DAILY_STOP_USD", "3") or "3")
        self._kraken_only_symbols = str(os.getenv("OKX_ONLY_SYMBOLS", "true")).strip().lower() in {"1", "true", "yes", "on"}

        try:
            self._okx_quote_ccy = str(os.getenv("OKX_QUOTE_CCY", "EUR") or "EUR").strip().upper()
        except Exception:
            self._okx_quote_ccy = "EUR"
        if not self._okx_quote_ccy:
            self._okx_quote_ccy = "EUR"
        
        # Real crypto prices
        self.base_prices = {
            'BTCUSDT': 48250.0, 'ETHUSDT': 2580.0, 'XRPUSDT': 0.52,
            'SOLUSDT': 105.40, 'AVAXUSDT': 36.80, 'ADAUSDT': 0.58,
            'DOGEUSDT': 0.085, 'LTCUSDT': 72.50, 'BNBUSDT': 588.0,
            'MATICUSDT': 0.92, 'DOTUSDT': 7.85, 'LINKUSDT': 18.40,
            'ATOMUSDT': 9.75, 'UNIUSDT': 6.82
        }
        
        # Volatility settings
        self.volatility_map = {
            'BTCUSDT': 0.025, 'ETHUSDT': 0.030, 'XRPUSDT': 0.040,
            'SOLUSDT': 0.045, 'AVAXUSDT': 0.038, 'ADAUSDT': 0.035,
            'DOGEUSDT': 0.050, 'LTCUSDT': 0.028, 'BNBUSDT': 0.022,
            'MATICUSDT': 0.042, 'DOTUSDT': 0.036, 'LINKUSDT': 0.032,
            'ATOMUSDT': 0.034, 'UNIUSDT': 0.038
        }

        # If quote currency is not USDT, ensure base_prices/volatility_map include that suffix too.
        try:
            quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
        except Exception:
            quote = "EUR"
        if quote and quote != "USDT":
            try:
                for k, v in list(self.base_prices.items()):
                    if isinstance(k, str) and k.endswith("USDT"):
                        self.base_prices.setdefault(k[:-4] + quote, float(v))
                for k, v in list(self.volatility_map.items()):
                    if isinstance(k, str) and k.endswith("USDT"):
                        self.volatility_map.setdefault(k[:-4] + quote, float(v))
            except Exception:
                pass

        # If Kraken-only mode is enabled, do not include other markets in the runtime universe.
        # Otherwise: if Kraken live prices are enabled, default to crypto-only unless user explicitly enables other markets.
        default_other_markets = "false" if self._kraken_live_prices_enabled else "true"
        force_crypto_only = bool(self._kraken_only_symbols and self._kraken_enabled())

        metals_enabled = (not force_crypto_only) and (str(os.getenv("ENABLE_METALS_TRADING", default_other_markets)).strip().lower() in {"1", "true", "yes", "on"})
        if metals_enabled:
            self.base_prices.update({
                'XAUUSD': 5182.0,
                'XAGUSD': 26.30,
            })
            self.volatility_map.update({
                'XAUUSD': 0.010,
                'XAGUSD': 0.020,
            })

        forex_enabled = (not force_crypto_only) and (str(os.getenv("ENABLE_FOREX_TRADING", default_other_markets)).strip().lower() in {"1", "true", "yes", "on"})
        if forex_enabled:
            self.base_prices.update({
                'EURUSD': 1.0860,
                'GBPUSD': 1.2680,
                'USDJPY': 150.20,
            })
            self.volatility_map.update({
                'EURUSD': 0.0010,
                'GBPUSD': 0.0012,
                'USDJPY': 0.0010,
            })

        indices_enabled = (not force_crypto_only) and (str(os.getenv("ENABLE_INDICES_TRADING", default_other_markets)).strip().lower() in {"1", "true", "yes", "on"})
        if indices_enabled:
            self.base_prices.update({
                'NAS100': 18150.0,
                'SPX500': 5090.0,
            })
            self.volatility_map.update({
                'NAS100': 0.0020,
                'SPX500': 0.0018,
            })

        oil_enabled = (not force_crypto_only) and (str(os.getenv("ENABLE_OIL_TRADING", default_other_markets)).strip().lower() in {"1", "true", "yes", "on"})
        if oil_enabled:
            self.base_prices.update({
                'USOIL': 77.00,
                'UKOIL': 79.00,
            })
            self.volatility_map.update({
                'USOIL': 0.015,
                'UKOIL': 0.015,
            })

    def _apply_kraken_only_universe(self) -> None:
        """When Kraken-only mode is enabled, ensure base_prices/volatility_map cover the Kraken USDT universe.

        This makes dashboard symbols, market thread, and analysis loop run only on Kraken pairs.
        """
        try:
            if not (self._kraken_only_symbols and self._kraken_enabled()):
                return
            uni = list(getattr(self, "_kraken_universe_symbols", []) or [])
            if not uni:
                return

            # Keep deterministic order
            quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
            uni = sorted(list(dict.fromkeys([str(s) for s in uni if str(s).endswith(quote) and self._kraken_supported_pair(str(s)) is not None])))
            if not uni:
                return

            try:
                max_n = int(os.getenv("OKX_UNIVERSE_MAX", "0") or "0")
            except Exception:
                max_n = 0
            if max_n and max_n > 0:
                uni = uni[: max(1, int(max_n))]

            new_base: Dict[str, float] = {}
            new_vol: Dict[str, float] = {}
            for s in uni:
                try:
                    new_base[s] = float(self.base_prices.get(s, 1.0) or 1.0)
                except Exception:
                    new_base[s] = 1.0
                try:
                    new_vol[s] = float(self.volatility_map.get(s, 0.030) or 0.030)
                except Exception:
                    new_vol[s] = 0.030

            with self._data_lock:
                try:
                    self._kraken_universe_symbols = list(uni)
                except Exception:
                    pass
                self.base_prices = new_base
                self.volatility_map = new_vol
                for s, px in self.base_prices.items():
                    self._last_prices.setdefault(s, float(px))
                    self._candles.setdefault(s, deque(maxlen=600))
        except Exception:
            return

    def _kraken_fetch_ohlcv_for_dashboard(self, symbol: str, tf_sec: int) -> Optional[List[Dict[str, float]]]:
        """Fetch OHLCV from Kraken for dashboard charting and convert to candle dicts.

        Uses a supported Kraken timeframe then resamples to requested tf_sec.
        """
        try:
            if not symbol:
                return None
            if not (self._kraken_enabled() and (self._kraken_supported_pair(symbol) is not None)):
                return None
            tf_sec_i = int(max(1, int(tf_sec)))

            # Choose a Kraken-supported base timeframe to minimize resample work.
            if tf_sec_i <= 60:
                base_tf = "1m"
                base_tf_sec = 60
            elif tf_sec_i <= 300:
                base_tf = "5m"
                base_tf_sec = 300
            elif tf_sec_i <= 900:
                base_tf = "15m"
                base_tf_sec = 900
            elif tf_sec_i <= 1800:
                base_tf = "30m"
                base_tf_sec = 1800
            else:
                base_tf = "1h"
                base_tf_sec = 3600

            # Aim for ~600 candles at the requested tf (chart view size), converted to base timeframe.
            need = int((600 * tf_sec_i) / max(1, base_tf_sec)) + 50
            limit = int(max(50, min(720, need)))
            # Dashboard fetch uses its own executor so the chart doesn't freeze when signal generation is busy.
            o = None
            try:
                pair = self._kraken_supported_pair(symbol)
                if not pair:
                    return None

                limit_i = int(max(10, min(1000, int(limit))))
                key = (pair, str(base_tf), int(limit_i))
                now = time.time()
                cached = self._kraken_ohlcv_cache.get(key)
                if cached is not None:
                    ts, data = cached
                    if (now - float(ts)) <= float(self._kraken_ohlcv_cache_ttl_sec):
                        o = data
                if o is None:
                    fut = self._dash_executor.submit(self._kraken.fetch_ohlcv, pair, str(base_tf), None, limit_i)
                    try:
                        raw = fut.result(timeout=max(1.0, float(getattr(self, "_kraken_call_timeout_sec", 6.0) or 6.0)))
                    except Exception:
                        try:
                            fut.cancel()
                        except Exception:
                            pass
                        return None
                    if not isinstance(raw, list) or not raw:
                        return None
                    o = [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] if len(x) > 5 else 0.0)] for x in raw]
                    self._kraken_ohlcv_cache[key] = (now, o)
            except Exception:
                o = None
            if not o:
                return None

            candles = []
            for row in o:
                try:
                    t_ms = float(row[0])
                    t = int(t_ms / 1000)
                    candles.append({
                        "t": int(t),
                        "o": float(row[1]),
                        "h": float(row[2]),
                        "l": float(row[3]),
                        "c": float(row[4]),
                    })
                except Exception:
                    continue

            if not candles:
                return None
            candles = candles[-1200:]
            if tf_sec_i != int(base_tf_sec):
                candles = self._resample_candles(candles, tf_sec_i)
            return candles
        except Exception:
            return None

    def _get_signal_universe(self) -> List[str]:
        symbols = list(self.base_prices.keys())
        if self._kraken_live_prices_enabled and self._kraken_only_symbols:
            # Prefer dynamic Kraken USDT universe (spot) if available
            if self._kraken_universe_symbols:
                return list(self._kraken_universe_symbols)
            # Fallback: only symbols that map to Kraken pairs (avoid mixing live crypto with simulated FX/metals)
            symbols = [s for s in symbols if self._kraken_supported_pair(s) is not None]
        return symbols

    def _symbol_market(self, symbol: str) -> str:
        try:
            if not symbol:
                return "other"
            if self._is_crypto_symbol(symbol):
                return "crypto"
            if symbol in {"XAUUSD", "XAGUSD"}:
                return "metals"
            if symbol in {"EURUSD", "GBPUSD", "USDJPY"}:
                return "forex"
            if symbol in {"USOIL", "UKOIL"}:
                return "oil"
            if symbol in {"NAS100", "SPX500"}:
                return "indices"
        except Exception:
            pass
        return "other"

    def _symbol_source(self, symbol: str) -> str:
        try:
            if self._kraken_live_prices_enabled and (self._kraken_supported_pair(symbol) is not None):
                return "okx"
        except Exception:
            pass
        return "sim"

    def _calc_performance_stats(self) -> Dict[str, float]:
        try:
            with self._data_lock:
                trades = list(self._trade_history)

            pnls: List[float] = []
            for t in trades:
                try:
                    pnls.append(float(t.get("pnl", 0.0)))
                except Exception:
                    pass

            total_trades = len(pnls)
            if total_trades == 0:
                d = self._PERF_STATS_EMPTY.copy()
                d["equity_curve"] = []
                return d

            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_profit = float(sum(wins))
            gross_loss = float(abs(sum(losses)))
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

            expectancy = float(sum(pnls)) / float(total_trades)
            avg_win = float(sum(wins)) / float(len(wins)) if wins else 0.0
            avg_loss = float(sum(losses)) / float(len(losses)) if losses else 0.0
            best_trade = float(max(pnls)) if pnls else 0.0
            worst_trade = float(min(pnls)) if pnls else 0.0

            # Avg trade duration
            durations: List[float] = []
            for t in trades:
                try:
                    d = float(t.get("duration_sec", 0) or 0)
                    if d > 0:
                        durations.append(d)
                except Exception:
                    pass
            avg_duration = float(sum(durations) / len(durations)) if durations else 0.0

            # Sharpe ratio (annualized, assuming ~365 trades/year as proxy)
            sharpe = 0.0
            if len(pnls) >= 2:
                import numpy as np
                arr = np.array(pnls, dtype=float)
                std = float(np.std(arr, ddof=1))
                if std > 0:
                    sharpe = float(np.mean(arr) / std) * (365 ** 0.5)

            # Equity curve + drawdown
            equity = 0.0
            peak = 0.0
            max_dd = 0.0
            eq_curve: List[List[float]] = []
            for i, p in enumerate(pnls):
                equity += float(p)
                peak = max(peak, equity)
                dd = peak - equity
                max_dd = max(max_dd, dd)
                try:
                    ts = int(trades[i].get("ts", 0) or 0)
                except Exception:
                    ts = 0
                if ts > 0:
                    eq_curve.append([ts, round(equity, 4)])

            _empty = self._PERF_STATS_EMPTY.copy()
            _empty.update({
                "profit_factor": float(profit_factor),
                "expectancy": float(expectancy),
                "max_drawdown": float(max_dd),
                "avg_win": float(avg_win),
                "avg_loss": float(avg_loss),
                "best_trade": float(best_trade),
                "worst_trade": float(worst_trade),
                "win_count": len(wins),
                "loss_count": len(losses),
                "avg_trade_duration_sec": round(avg_duration, 0),
                "sharpe_ratio": round(sharpe, 2),
                "equity_curve": eq_curve,
            })
            return _empty
        except Exception:
            d = self._PERF_STATS_EMPTY.copy()
            d["equity_curve"] = []
            return d

    def _load_next_signal_id(self) -> int:
        try:
            if os.path.exists(self._signal_id_state_path):
                with open(self._signal_id_state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                val = int(data.get("next_signal_id", 1))
                return max(1, val)
        except Exception as e:
            logger.error(f"Failed to load signal id state: {e}")
        return 1

    def _save_next_signal_id(self) -> None:
        try:
            with open(self._signal_id_state_path, "w", encoding="utf-8") as f:
                json.dump({"next_signal_id": int(self._next_signal_id)}, f)
        except Exception as e:
            logger.error(f"Failed to save signal id state: {e}")

    def _reset_state_if_requested(self) -> None:
        """Optional reset to start clean (histories + signal IDs).

        Trigger by setting RESET_ALL_STATE=true in environment.
        """
        try:
            reset_all = str(os.getenv("RESET_ALL_STATE", "false")).strip().lower() in {"1", "true", "yes", "on"}
            reset_signal = str(os.getenv("RESET_SIGNAL_STATE", "false")).strip().lower() in {"1", "true", "yes", "on"}
            if not (reset_all or reset_signal):
                return

            with self._data_lock:
                self.active_signals = {}
                self._signal_history.clear()
                self._trade_history.clear()
                self._verification_audit.clear()

            self.total_trades = 0
            self.winning_trades = 0
            self.daily_pnl = 0.0
            self.total_pnl = 0.0

            if os.path.exists(self._signal_id_state_path):
                try:
                    os.remove(self._signal_id_state_path)
                except Exception as e:
                    logger.error(f"Failed to delete signal id state file: {e}")

            self._next_signal_id = 1
            self._save_next_signal_id()
            logger.warning("STATE RESET: histories cleared and signal_id counter reset to 1")
        except Exception as e:
            logger.error(f"State reset failed: {e}")

    def _pip_size_for_symbol(self, symbol: str, entry_price: float) -> float:
        profile = self._market_profile(symbol)
        if profile is not None:
            # derive pip size from decimals (best-effort)
            dec = int(profile.get("dec", 4))
            if symbol in {"EURUSD", "GBPUSD"}:
                return 0.0001
            if symbol == "USDJPY":
                return 0.01
            if symbol == "XAUUSD":
                return 1.0
            if symbol == "XAGUSD":
                return 0.01
            return 10 ** (-max(0, dec - 1))

        # crypto / default — scale pip size to price magnitude
        # (PEPE at $0.000003 needs pip=0.0000001, BTC at $65k needs pip=1.0)
        if entry_price >= 10000:
            return 1.0
        if entry_price >= 100:
            return 0.1
        if entry_price >= 1:
            return 0.01
        if entry_price >= 0.01:
            return 0.0001
        if entry_price >= 0.0001:
            return 0.000001
        if entry_price > 0:
            # Ultra-micro: pip = entry_price / 10000 (gives ~10k pips per 1x move)
            return max(1e-12, entry_price / 10000.0)
        return 0.0001

    def _pick_free_port(self, ports: List[int]) -> int:
        for p in ports:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
        return ports[0]

    def _start_market_data(self) -> None:
        if self._market_thread and self._market_thread.is_alive():
            return

        base_tf = int(os.getenv("BASE_CANDLE_TF_SEC", str(self._candle_tf_sec)) or str(self._candle_tf_sec))
        if base_tf < 1:
            base_tf = 1
        self._candle_tf_sec = base_tf

        self._warmup_candles()

        def worker():
            while self.running:
                try:
                    now = int(time.time())
                    bucket = now - (now % self._candle_tf_sec)
                    for sym in list(self.base_prices.keys()):
                        price = self.get_current_price(sym)
                        if price is None:
                            continue
                        with self._data_lock:
                            self._last_prices[sym] = float(price)
                            candles = self._candles.get(sym)
                            if candles is None:
                                candles = deque(maxlen=600)
                                self._candles[sym] = candles

                            if not candles or candles[-1]["t"] != bucket:
                                candles.append({
                                    "t": bucket,
                                    "o": float(price),
                                    "h": float(price),
                                    "l": float(price),
                                    "c": float(price),
                                })
                            else:
                                c = candles[-1]
                                c["h"] = max(c["h"], float(price))
                                c["l"] = min(c["l"], float(price))
                                c["c"] = float(price)
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Market data thread error: {e}")
                    time.sleep(1)

        self._market_thread = threading.Thread(target=worker, daemon=True)
        self._market_thread.start()

    def _warmup_candles(self) -> None:
        try:
            n = int(os.getenv("CANDLE_WARMUP_COUNT", "300") or "300")
        except Exception:
            n = 300
        if n <= 0:
            return

        try:
            now = int(time.time())
            tf = int(self._candle_tf_sec) if int(self._candle_tf_sec) > 0 else 1
            last_bucket = now - (now % tf)
        except Exception:
            return

        try:
            with self._data_lock:
                for sym in list(self.base_prices.keys()):
                    px0 = float(self._last_prices.get(sym, self.base_prices.get(sym, 0.0) or 0.0))
                    if px0 <= 0:
                        continue

                    candles = self._candles.get(sym)
                    if candles is None:
                        candles = deque(maxlen=600)
                        self._candles[sym] = candles

                    # Only warm up if empty
                    if len(candles) > 0:
                        continue

                    vol = float(self.volatility_map.get(sym, 0.01) or 0.01)
                    px = px0
                    for i in range(max(1, min(n, 600))):
                        t = int(last_bucket - (max(1, min(n, 600)) - i) * tf)
                        # simple random-walk candle
                        drift = random.uniform(-1.0, 1.0) * vol * 0.25
                        o = px
                        c = max(1e-12, px * (1.0 + drift))
                        hi = max(o, c) * (1.0 + abs(random.uniform(0.0, vol * 0.15)))
                        lo = min(o, c) * (1.0 - abs(random.uniform(0.0, vol * 0.15)))
                        candles.append({"t": int(t), "o": float(o), "h": float(hi), "l": float(lo), "c": float(c)})
                        px = c
                    self._last_prices[sym] = float(px0)
        except Exception:
            return

    def _kraken_fetch_ticker_safe(self, pair: str) -> Optional[Dict[str, object]]:
        try:
            if not pair:
                return None
            if self._kraken is None:
                return None
            try:
                timeout_s = float(getattr(self, "_kraken_call_timeout_sec", 6.0) or 6.0)
            except Exception:
                timeout_s = 6.0

            _res_box = [None]
            def _do_fetch_ticker():
                try:
                    _res_box[0] = self._kraken.fetch_ticker(pair)
                except Exception:
                    pass
            _t = threading.Thread(target=_do_fetch_ticker, daemon=True)
            _t.start()
            _t.join(timeout=max(1.0, min(timeout_s, 8.0)))
            if _t.is_alive():
                self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                return None
            res = _res_box[0]
            return res if isinstance(res, dict) else None
        except Exception:
            return None

    def _kraken_fetch_order_safe(self, order_id: str, pair: str) -> Optional[Dict[str, object]]:
        try:
            if not order_id or not pair:
                return None
            if self._kraken is None:
                return None
            try:
                timeout_s = float(getattr(self, "_kraken_call_timeout_sec", 6.0) or 6.0)
            except Exception:
                timeout_s = 6.0

            _res_box = [None]
            def _do_fetch_order():
                try:
                    _res_box[0] = self._kraken.fetch_order(order_id, pair)
                except Exception:
                    pass
            _t = threading.Thread(target=_do_fetch_order, daemon=True)
            _t.start()
            _t.join(timeout=max(1.0, min(timeout_s, 8.0)))
            if _t.is_alive():
                self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                return None
            res = _res_box[0]
            return res if isinstance(res, dict) else None
        except Exception:
            return None

    def _parse_tf_seconds(self, tf: str) -> int:
        try:
            s = (tf or "").strip().lower()
            if not s:
                return int(self._candle_tf_sec)
            if s.endswith("m") and s[:-1].isdigit():
                return int(s[:-1]) * 60
            if s.endswith("h") and s[:-1].isdigit():
                return int(s[:-1]) * 3600
            if s.isdigit():
                return int(s)
        except Exception:
            pass
        return int(self._candle_tf_sec)

    def _resample_candles(self, candles: List[Dict[str, float]], tf_sec: int) -> List[Dict[str, float]]:
        try:
            if not candles:
                return []
            base = int(self._candle_tf_sec)
            if tf_sec <= base:
                return candles
            if tf_sec % base != 0:
                # best-effort: still bucket by tf_sec even if not a multiple
                pass

            out: List[Dict[str, float]] = []
            current = None
            cur_bucket = None
            for c in candles:
                t = int(c.get("t", 0))
                bucket = t - (t % int(tf_sec))
                if cur_bucket != bucket:
                    if current is not None:
                        out.append(current)
                    cur_bucket = bucket
                    current = {
                        "t": int(bucket),
                        "o": float(c.get("o", 0.0)),
                        "h": float(c.get("h", 0.0)),
                        "l": float(c.get("l", 0.0)),
                        "c": float(c.get("c", 0.0)),
                    }
                else:
                    if current is None:
                        continue
                    current["h"] = max(float(current.get("h", 0.0)), float(c.get("h", 0.0)))
                    current["l"] = min(float(current.get("l", 0.0)), float(c.get("l", 0.0)))
                    current["c"] = float(c.get("c", 0.0))
            if current is not None:
                out.append(current)
            return out

        except Exception:
            return candles

    def _kraken_build_order_plan(self, symbol: str, side: str, usd_size: float) -> Dict[str, object]:
        """Build an order plan (pair/limit price/amount) without placing an order."""
        out: Dict[str, object] = {"ok": False}
        if self._kraken is None:
            out["error"] = "kraken_not_initialized"
            return out

        pair = self._kraken_supported_pair(symbol)
        if not pair:
            out["error"] = "unsupported_pair"
            return out

        try:
            if getattr(self._kraken, "markets", None) and pair not in getattr(self._kraken, "markets", {}):
                out["error"] = "unsupported_pair_market"
                return out
        except Exception:
            pass

        t = self._kraken_fetch_ticker_safe(pair)
        if not isinstance(t, dict):
            out["error"] = "fetch_ticker_failed"
            return out
        bid = float(t.get("bid") or 0.0)
        ask = float(t.get("ask") or 0.0)
        last = float(t.get("last") or 0.0)

        px_ref = ask if side.upper() == "BUY" else bid
        if px_ref <= 0:
            px_ref = last
        if px_ref <= 0:
            out["error"] = "no_price"
            return out

        # Spread guard (paper plan mirrors live behavior)
        try:
            max_spread_pct = float(getattr(self, "_kraken_max_spread_pct", 1.0) or 1.0)
            if bid > 0 and ask > 0:
                spread_pct = ((ask - bid) / bid) * 100.0
                out["spread_pct"] = round(spread_pct, 4)
                if spread_pct > max_spread_pct:
                    out["error"] = f"spread_too_wide:{spread_pct:.3f}%>{max_spread_pct:.1f}%"
                    return out
        except Exception:
            pass

        offset = abs(float(self._kraken_limit_offset_pct)) / 100.0
        if side.upper() == "BUY":
            limit_px = px_ref * (1.0 + offset)
        else:
            limit_px = px_ref * (1.0 - offset)

        try:
            limit_px = float(self._kraken_round_price(pair, float(limit_px)))
        except Exception:
            limit_px = float(limit_px)

        if float(limit_px) <= 0:
            out["error"] = "bad_limit_price"
            return out

        amount = float(usd_size) / float(limit_px)
        amount = self._kraken_round_amount(pair, amount)
        if float(amount) <= 0:
            out["error"] = "amount_rounded_to_zero"
            return out
        min_amt = self._kraken_min_amount(pair)
        if min_amt > 0 and amount < min_amt:
            out["error"] = f"amount_below_min:{amount}<{min_amt}"
            return out

        # Enforce minimum notional/cost if exchange provides it
        try:
            m = getattr(self._kraken, "markets", {}).get(pair) or {}
            cost_min = (((m.get("limits") or {}).get("cost") or {}).get("min"))
            if cost_min is not None:
                try:
                    cost_min_f = float(cost_min)
                except Exception:
                    cost_min_f = 0.0
                if cost_min_f > 0:
                    notional = float(amount) * float(limit_px)
                    if notional + 1e-12 < cost_min_f:
                        out["error"] = f"cost_below_min:{notional}<{cost_min_f}"
                        return out
        except Exception:
            pass

        # In paper mode we usually don't want to block planning because of balances.
        if self._kraken_paper_check_balance:
            ok_bal, bal_msg = self._kraken_check_spot_balance(pair, side, float(amount), float(limit_px))
            if not ok_bal:
                out.update({"ok": False, "pair": pair, "type": "limit", "side": side.lower(), "amount": float(amount), "price": float(limit_px), "status": "plan_failed", "error": f"balance:{bal_msg}"})
                return out

        out.update({"ok": True, "pair": pair, "type": "limit", "side": side.lower(), "amount": float(amount), "price": float(limit_px), "status": "planned"})
        return out

    def _start_dashboard(self) -> None:
        if self._dashboard_thread and self._dashboard_thread.is_alive():
            return

        # Use a stable port so the browser/API doesn't hit a different port and fail.
        # If you need a different port, set DASHBOARD_PORT in .env.
        try:
            desired_port = int(os.getenv("DASHBOARD_PORT", "5001") or "5001")
        except Exception:
            desired_port = 5001

        # If the desired port is busy, pick a free one and log it.
        chosen = desired_port
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", desired_port))
        except OSError:
            chosen = self._pick_free_port([desired_port, 5001, 5002, 5003, 5000])
        self.dashboard_port = int(chosen)

        def run_server():
            try:
                from flask import Flask, jsonify, request, render_template_string, make_response
                try:
                    from flask_cors import CORS
                except Exception:
                    CORS = None

                import traceback

                try:
                    # Silence request logs (GET /api/...) that spam the console
                    import logging as _logging
                    _logging.getLogger('werkzeug').setLevel(_logging.ERROR)
                except Exception:
                    pass

                app = Flask(__name__)
                try:
                    app.logger.disabled = True
                    app.logger.propagate = False
                except Exception:
                    pass
                if CORS:
                    CORS(app)

                @app.after_request
                def _no_translate(response):
                    response.headers["Content-Language"] = "en"
                    response.headers["X-Robots-Tag"] = "notranslate"
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    # Force Chrome to treat JSON as data, not translatable text
                    if response.content_type and "json" in response.content_type:
                        response.headers["Content-Type"] = "application/json; charset=utf-8"
                    return response

                @app.errorhandler(Exception)
                def handle_exception(e):
                    try:
                        from werkzeug.exceptions import NotFound
                        if isinstance(e, NotFound):
                            return e
                    except Exception:
                        pass
                    try:
                        logger.error(f"Dashboard unhandled exception: {e}\n{traceback.format_exc()}")
                    except Exception:
                        pass
                    try:
                        return jsonify({"error": "internal_error", "message": str(e)}), 500
                    except Exception:
                        return ("Internal Server Error", 500)

                html = r'''
<!doctype html>
<html lang="en" translate="no" class="notranslate">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta name="google" content="notranslate" />
  <meta http-equiv="Content-Language" content="en" />
  <title>ROBOXRP PRO v13.2 - Live Dashboard</title>
  <script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    *{box-sizing:border-box}
    body{margin:0;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0a0e1a;color:#e2e8f0;min-height:100vh}
    .wrap{max-width:1280px;margin:0 auto;padding:12px 16px}

    /* Header */
    .header{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1e293b;margin-bottom:12px}
    .header-left{display:flex;align-items:center;gap:12px}
    .logo{font-size:20px;font-weight:800;background:linear-gradient(135deg,#818cf8,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .version{font-size:11px;color:#64748b;margin-top:2px}
    .conn-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600}
    .conn-badge .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
    .conn-ok{background:#064e3b;color:#34d399;border:1px solid #065f46}
    .conn-ok .dot{background:#34d399;box-shadow:0 0 6px #34d399;animation:pulse 2s infinite}
    .conn-warn{background:#78350f;color:#fbbf24;border:1px solid #92400e}
    .conn-warn .dot{background:#fbbf24}
    .conn-off{background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
    .conn-off .dot{background:#f87171}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

    /* Grid */
    .grid{display:grid;gap:10px}
    .grid-5{grid-template-columns:repeat(5,1fr)}
    .grid-3{grid-template-columns:repeat(3,1fr)}
    .grid-2{grid-template-columns:1fr 1fr}
    .grid-chart{grid-template-columns:2.2fr 1fr}

    /* Cards */
    .card{background:#111827;border:1px solid #1e293b;border-radius:10px;padding:12px;position:relative;overflow:hidden}
    .card-glow{box-shadow:0 0 20px rgba(99,102,241,.08)}
    .card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
    .card-title{font-weight:700;font-size:13px;color:#cbd5e1}
    .label{color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
    .stat{font-size:24px;font-weight:800}
    .stat-sm{font-size:18px;font-weight:700}
    .sub{font-size:11px;color:#64748b;margin-top:2px}

    /* Colors */
    .green{color:#4ade80}.red{color:#f87171}.amber{color:#fbbf24}.blue{color:#60a5fa}.purple{color:#a78bfa}.cyan{color:#22d3ee}
    .bg-green{background:#052e16;border-color:#065f46}
    .bg-red{background:#2a0505;border-color:#7f1d1d}

    /* Stat cards top stripe */
    .stripe-green{border-top:3px solid #4ade80}
    .stripe-red{border-top:3px solid #f87171}
    .stripe-blue{border-top:3px solid #60a5fa}
    .stripe-purple{border-top:3px solid #a78bfa}
    .stripe-amber{border-top:3px solid #fbbf24}

    /* Controls */
    select,button{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:5px 8px;font-size:11px;outline:none;transition:border-color .15s}
    select:focus,button:focus{border-color:#6366f1}
    button{background:#4f46e5;border-color:#4f46e5;cursor:pointer;font-weight:600}
    button:hover{background:#4338ca}
    .btn-sm{padding:3px 8px;font-size:10px;border-radius:4px}
    .controls{display:flex;gap:6px;align-items:center;flex-wrap:wrap}

    /* Tables */
    table{width:100%;border-collapse:collapse}
    th,td{padding:5px 6px;font-size:11px;white-space:nowrap}
    th{color:#64748b;text-align:left;font-weight:600;text-transform:uppercase;letter-spacing:.3px;border-bottom:2px solid #1e293b;font-size:10px}
    td{border-bottom:1px solid #1e293b44;color:#cbd5e1}
    .right{text-align:right}
    tbody tr:hover{background:#1e293b44}
    .scroll{overflow:auto}

    /* Progress bar */
    .pbar{height:4px;border-radius:2px;background:#1e293b;overflow:hidden;min-width:50px}
    .pbar-fill{height:100%;border-radius:2px;transition:width .3s}

    /* Confidence pill */
    .conf-pill{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:700}
    .conf-high{background:#052e16;color:#4ade80}
    .conf-med{background:#422006;color:#fbbf24}
    .conf-low{background:#450a0a;color:#f87171}

    /* Tag */
    .tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600}
    .tag-live{background:#78350f;color:#fbbf24}
    .tag-paper{background:#172554;color:#60a5fa}
    .tag-win{background:#052e16;color:#4ade80}
    .tag-loss{background:#450a0a;color:#f87171}

    /* Health grid */
    .health-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px;font-size:11px;color:#94a3b8}
    .health-item{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1e293b44}
    .health-label{color:#64748b}
    .health-val{color:#cbd5e1;font-weight:600;text-align:right}

    /* Equity chart */
    #equityChart{width:100%;height:120px;background:#0f172a;border-radius:6px}

    /* Chart */
    #chart{width:100%}

    /* Scrollbar (dark theme) */
    ::-webkit-scrollbar{width:6px;height:6px}
    ::-webkit-scrollbar-track{background:#0a0e1a}
    ::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
    ::-webkit-scrollbar-thumb:hover{background:#475569}

    /* Empty state */
    .empty-row td{text-align:center;padding:20px;color:#475569;font-style:italic;border-bottom:none}

    /* Footer */
    .footer{text-align:center;padding:16px 0 8px;color:#334155;font-size:10px;border-top:1px solid #1e293b;margin-top:12px}
    .footer a{color:#6366f1;text-decoration:none}

    /* Responsive */
    @media (max-width:1100px){.grid-5{grid-template-columns:repeat(3,1fr)}.grid-chart{grid-template-columns:1fr}}
    @media (max-width:768px){.grid-5{grid-template-columns:repeat(2,1fr)}.grid-3{grid-template-columns:1fr}.grid-2{grid-template-columns:1fr}.header{flex-direction:column;gap:8px}}
  </style>
</head>
<body>
  <div class="wrap">
    <!-- HEADER -->
    <div class="header">
      <div class="header-left">
        <div>
          <div class="logo">ROBOXRP PRO</div>
          <div class="version">v13.2 Live Trading Dashboard</div>
        </div>
        <span id="connBadge" class="conn-badge conn-off"><span class="dot"></span><span id="connText">Connecting...</span></span>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;color:#64748b">Last update: <span id="lastUpdate" class="blue">...</span></div>
        <div style="font-size:11px;color:#64748b">Cycle: <span id="hCycle">...</span></div>
      </div>
    </div>

    <!-- TOP STATS -->
    <div class="grid grid-5" style="margin-bottom:10px">
      <div class="card stripe-blue"><div class="label">Active Trades</div><div id="activeTrades" class="stat">0</div><div id="activeTradesMax" class="sub">/ 0 max</div></div>
      <div class="card stripe-green"><div class="label">Win Rate</div><div id="winRate" class="stat green">0%</div><div id="totalTrades" class="sub">0 trades</div></div>
      <div class="card stripe-purple"><div class="label">Realized PnL</div><div id="realizedPnl" class="stat">$0.00</div><div id="dailyPnl" class="sub">Today: $0.00</div></div>
      <div class="card stripe-amber"><div class="label">Unrealized PnL</div><div id="unrealizedPnl" class="stat">$0.00</div><div id="heatInfo" class="sub">Heat: $0.00</div></div>
      <div class="card stripe-blue"><div class="label">Total PnL</div><div id="totalPnl" class="stat">$0.00</div><div id="pfInfo" class="sub">PF: 0.00 | Exp: $0.00</div></div>
    </div>

    <!-- SESSION BAR -->
    <div style="display:flex;gap:16px;align-items:center;margin-bottom:10px;padding:6px 12px;background:#111827;border:1px solid #1e293b;border-radius:8px;font-size:11px;color:#94a3b8;flex-wrap:wrap">
      <span>Uptime: <b id="sessionUptime" class="blue">0m</b></span>
      <span>Balance: <b id="sessionBalance" class="green">...</b></span>
      <span>W/L: <b id="sessionWL">0/0</b></span>
      <span>Sharpe: <b id="sessionSharpe">0.00</b></span>
      <span>Avg Duration: <b id="sessionAvgDur">0m</b></span>
      <span>Mode: <b id="sessionMode" class="amber">...</b></span>
    </div>

    <!-- STATISTICS + EQUITY CURVE -->
    <div class="grid grid-3" style="margin-bottom:10px">
      <div class="card">
        <div class="card-title" style="margin-bottom:8px">Performance</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
          <div class="health-item"><span class="health-label">Avg Win</span><span id="avgWin" class="health-val green">$0.00</span></div>
          <div class="health-item"><span class="health-label">Avg Loss</span><span id="avgLoss" class="health-val red">$0.00</span></div>
          <div class="health-item"><span class="health-label">Best Trade</span><span id="bestTrade" class="health-val green">$0.00</span></div>
          <div class="health-item"><span class="health-label">Worst Trade</span><span id="worstTrade" class="health-val red">$0.00</span></div>
          <div class="health-item"><span class="health-label">Profit Factor</span><span id="profitFactor" class="health-val">0.00</span></div>
          <div class="health-item"><span class="health-label">Expectancy</span><span id="expectancy" class="health-val">$0.00</span></div>
          <div class="health-item"><span class="health-label">Max Drawdown</span><span id="maxDD" class="health-val red">$0.00</span></div>
          <div class="health-item"><span class="health-label">Loss Streak</span><span id="lossStreak" class="health-val">0/5</span></div>
        </div>
      </div>
      <div class="card" style="grid-column:span 2">
        <div class="card-title" style="margin-bottom:6px">Equity Curve</div>
        <div id="equityChart"></div>
      </div>
    </div>

    <!-- CONNECTIVITY / HEALTH -->
    <div class="card" style="margin-bottom:10px">
      <div class="card-header">
        <div class="card-title">System Health</div>
        <div style="font-size:10px;color:#64748b">Auto-refresh 3s</div>
      </div>
      <div class="health-grid">
        <div class="health-item"><span class="health-label">OKX</span><span id="hKraken" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Universe</span><span id="hUniverse" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Balance</span><span id="hUsdt" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">OHLCV</span><span id="hOhlcv" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Trading</span><span id="hTrading" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Telegram</span><span id="hTelegram" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Threads</span><span id="hThreads" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Signal Scan</span><span id="hScan" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Last Skip</span><span id="hSkip" class="health-val amber">...</span></div>
        <div class="health-item"><span class="health-label">Heat / Fees</span><span id="hHeat" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Streak (L/W)</span><span id="hLosses" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Stake</span><span id="hStake" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Cycle</span><span id="hCycle" class="health-val">...</span></div>
        <div class="health-item"><span class="health-label">Last Error</span><span id="hLastError" class="health-val">...</span></div>
      </div>
    </div>

    <!-- CHART + ACTIVE TRADES -->
    <div class="grid grid-chart" style="margin-bottom:10px">
      <div class="card card-glow">
        <div class="card-header">
          <div class="card-title">Live Candles</div>
          <div class="controls">
            <select id="marketSelect">
              <option value="all">All</option>
              <option value="crypto">Crypto</option>
              <option value="forex">Forex</option>
              <option value="metals">Metals</option>
              <option value="oil">Oil</option>
              <option value="indices">Indices</option>
            </select>
            <select id="symbolSelect"></select>
            <select id="tfSelect">
              <option value="1m">1m</option>
              <option value="5m">5m</option>
              <option value="15m">15m</option>
              <option value="30m">30m</option>
              <option value="1h">1h</option>
              <option value="3h">3h</option>
            </select>
            <label style="display:flex;align-items:center;gap:4px;font-size:10px;color:#64748b;cursor:pointer">
              <input id="liveOnly" type="checkbox" /> Live
            </label>
            <button id="refreshBtn" class="btn-sm">Refresh</button>
          </div>
        </div>
        <div style="font-size:10px;color:#64748b;margin-bottom:4px"><span id="candleInfo">Loading...</span> <span id="symbolSource"></span></div>
        <div id="chart" style="height:420px;"></div>
      </div>
      <div class="card">
        <div class="card-title" style="margin-bottom:8px">Active Trades</div>
        <div class="scroll" style="max-height:420px">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Symbol</th>
                <th>Side</th>
                <th class="right">Entry</th>
                <th class="right">Now</th>
                <th class="right">PnL</th>
                <th class="right">$Size</th>
                <th>Progress</th>
                <th>TPs</th>
                <th class="right">Time</th>
              </tr>
            </thead>
            <tbody id="activeTradesTable"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- SIGNALS + HISTORY -->
    <div class="grid grid-2" style="margin-bottom:10px">
      <div class="card">
        <div class="card-title" style="margin-bottom:8px">Executed Signals</div>
        <div class="scroll" style="max-height:300px">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>ID</th>
                <th>Symbol</th>
                <th>Side</th>
                <th class="right">Entry</th>
                <th class="right">TP1-2</th>
                <th class="right">SL</th>
                <th>R:R</th>
                <th>Conf</th>
                <th>Mode</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="signalsTable"></tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <div class="card-title" style="margin-bottom:8px">Trade History</div>
        <div class="scroll" style="max-height:300px">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>ID</th>
                <th>Symbol</th>
                <th>Result</th>
                <th class="right">PnL</th>
                <th class="right">Pips</th>
                <th>Reason</th>
                <th>Duration</th>
                <th>TPs</th>
              </tr>
            </thead>
            <tbody id="historyTable"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- FOOTER -->
    <div class="footer">ROBOXRP PRO v13.2 &mdash; OKX Spot Trading System &mdash; Dashboard auto-refreshes every 3s</div>

  </div>

  <script>
    const chartContainer = document.getElementById('chart');
    const infoEl = document.getElementById('candleInfo');

    if (!window.LightweightCharts) {
      infoEl.textContent = 'Chart library failed to load.';
    }

    let chart = null, candleSeries = null, lineSeries = null, eqChart = null, eqSeries = null;

    try {
      chart = LightweightCharts.createChart(chartContainer, {
        width: chartContainer.clientWidth || 640, height: 420,
        layout: { background: { color: '#0a0e1a' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: '#1e293b55' }, horzLines: { color: '#1e293b55' } },
        timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 8, barSpacing: 10, fixLeftEdge: true },
        rightPriceScale: { borderColor: '#1e293b' },
        crosshair: { mode: 0 },
      });
      candleSeries = chart.addCandlestickSeries({
        upColor: '#22c55e', downColor: '#ef4444', borderDownColor: '#ef4444', borderUpColor: '#22c55e',
        wickDownColor: '#ef4444', wickUpColor: '#22c55e'
      });
      lineSeries = chart.addLineSeries({ color: '#6366f1', lineWidth: 1, crosshairMarkerVisible: false });
      window.addEventListener('resize', () => { try { chart.applyOptions({ width: chartContainer.clientWidth }); } catch(e){} });
      setTimeout(() => { try { chart.applyOptions({ width: chartContainer.clientWidth }); chart.timeScale().fitContent(); } catch(e){} }, 100);
    } catch (e) { infoEl.textContent = 'Chart init error: ' + e; }

    // Equity curve chart
    try {
      const eqC = document.getElementById('equityChart');
      eqChart = LightweightCharts.createChart(eqC, {
        width: eqC.clientWidth || 400, height: 120,
        layout: { background: { color: '#0f172a' }, textColor: '#64748b' },
        grid: { vertLines: { visible: false }, horzLines: { color: '#1e293b44' } },
        timeScale: { visible: false }, rightPriceScale: { borderColor: '#1e293b' },
        crosshair: { mode: 0 },
      });
      eqSeries = eqChart.addAreaSeries({ topColor: 'rgba(99,102,241,0.4)', bottomColor: 'rgba(99,102,241,0.02)', lineColor: '#6366f1', lineWidth: 2 });
      window.addEventListener('resize', () => { try { eqChart.applyOptions({ width: eqC.clientWidth }); } catch(e){} });
      setTimeout(() => { try { eqChart.applyOptions({ width: eqC.clientWidth }); } catch(e){} }, 100);
    } catch (e) {}

    // Utilities
    async function api(path, ms) {
      const c = new AbortController();
      const t = setTimeout(() => c.abort(), ms || 4000);
      const r = await fetch(path, { signal: c.signal }); clearTimeout(t);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }
    function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString(); }
    function nf(v, d, fb) { const n = Number(v); return Number.isFinite(n) ? n.toFixed(d) : Number(fb||0).toFixed(d); }
    function pnlColor(v) { const n = Number(v); return n > 0 ? 'green' : (n < 0 ? 'red' : ''); }
    function pnlStr(v) { const n = Number(v); return (n >= 0 ? '+' : '') + nf(n, 2, 0); }
    function fmtSec(s) { s=Math.round(s); const h=Math.floor(s/3600),m=Math.floor((s%3600)/60); if(h>0) return h+'h'+m+'m'; if(m>0) return m+'m'; return s+'s'; }
    function fmtUptime(s) { s=Math.round(s); const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60); if(d>0) return d+'d '+h+'h'; if(h>0) return h+'h '+m+'m'; return m+'m'; }

    let symbolItems = [];
    let lastHealthData = null;
    let lastSummaryData = null;
    let _ccySym = '$';  // dynamic: set from health quote_ccy

    // Connection badge
    function updateConnBadge(ok, stalled) {
      const badge = document.getElementById('connBadge');
      const text = document.getElementById('connText');
      if (stalled) { badge.className = 'conn-badge conn-warn'; text.textContent = 'Stalled'; }
      else if (ok) { badge.className = 'conn-badge conn-ok'; text.textContent = 'Connected'; }
      else { badge.className = 'conn-badge conn-off'; text.textContent = 'Offline'; }
    }

    function renderSymbols() {
      const sel = document.getElementById('symbolSelect');
      const market = document.getElementById('marketSelect').value;
      const live = document.getElementById('liveOnly').checked;
      sel.innerHTML = '';
      (symbolItems || []).filter(x => {
        if (market && market !== 'all' && x.market !== market) return false;
        if (live && x.source !== 'okx') return false; return true;
      }).forEach(x => { const o = document.createElement('option'); o.value = x.symbol; o.textContent = x.symbol; sel.appendChild(o); });
      if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
    }

    async function loadSymbols() {
      const d = await api('/api/symbols', 4000);
      symbolItems = (d.items || []).length ? d.items : (d.symbols || []).map(s => ({ symbol: s, market: 'other', source: 'sim' }));
      renderSymbols();
    }

    function renderSummary(s) {
      lastSummaryData = s;
      document.getElementById('lastUpdate').textContent = (s && s.ts) ? new Date(s.ts * 1000).toLocaleTimeString() : '...';
      const at = (s && s.active_trades !== undefined) ? s.active_trades : 0;
      document.getElementById('activeTrades').textContent = at;
      document.getElementById('activeTradesMax').textContent = '/ ' + (s && s.max_concurrent_trades ? s.max_concurrent_trades : '?') + ' max';
      document.getElementById('winRate').textContent = nf(s ? s.win_rate : 0, 1) + '%';
      document.getElementById('winRate').className = 'stat ' + ((s && s.win_rate >= 50) ? 'green' : (s && s.win_rate > 0 ? 'amber' : ''));
      document.getElementById('totalTrades').textContent = (s && s.total_trades ? s.total_trades : 0) + ' trades';

      const rPnl = Number(s ? (s.realized_pnl ?? s.total_pnl ?? 0) : 0);
      const uPnl = Number(s ? (s.unrealized_pnl ?? 0) : 0);
      const tPnl = Number(s ? (s.total_pnl_combined ?? (rPnl + uPnl)) : 0);
      const dPnl = Number(s ? (s.daily_pnl ?? 0) : 0);
      document.getElementById('realizedPnl').textContent = _ccySym + pnlStr(rPnl);
      document.getElementById('realizedPnl').className = 'stat ' + pnlColor(rPnl);
      document.getElementById('unrealizedPnl').textContent = _ccySym + pnlStr(uPnl);
      document.getElementById('unrealizedPnl').className = 'stat ' + pnlColor(uPnl);
      document.getElementById('totalPnl').textContent = _ccySym + pnlStr(tPnl);
      document.getElementById('totalPnl').className = 'stat ' + pnlColor(tPnl);
      document.getElementById('dailyPnl').textContent = 'Today: ' + _ccySym + pnlStr(dPnl);

      // Performance stats
      const pf = Number(s ? (s.profit_factor ?? 0) : 0);
      const exp = Number(s ? (s.expectancy ?? 0) : 0);
      const avgW = Number(s ? (s.avg_win ?? 0) : 0);
      const avgL = Number(s ? (s.avg_loss ?? 0) : 0);
      const bestT = Number(s ? (s.best_trade ?? 0) : 0);
      const worstT = Number(s ? (s.worst_trade ?? 0) : 0);
      const maxDd = Number(s ? (s.max_drawdown ?? 0) : 0);
      document.getElementById('pfInfo').textContent = 'PF: ' + nf(pf, 2) + ' | Exp: ' + _ccySym + pnlStr(exp);
      document.getElementById('avgWin').textContent = _ccySym + pnlStr(avgW);
      document.getElementById('avgLoss').textContent = _ccySym + pnlStr(avgL);
      document.getElementById('bestTrade').textContent = _ccySym + pnlStr(bestT);
      document.getElementById('worstTrade').textContent = _ccySym + pnlStr(worstT);
      document.getElementById('profitFactor').textContent = nf(pf, 2);
      document.getElementById('expectancy').textContent = _ccySym + pnlStr(exp);
      document.getElementById('maxDD').textContent = _ccySym + nf(maxDd, 2);

      // Session bar
      try {
        document.getElementById('sessionUptime').textContent = fmtUptime(s ? (s.session_uptime_sec || 0) : 0);
        document.getElementById('sessionWL').textContent = (s ? (s.win_count||0) : 0) + 'W / ' + (s ? (s.loss_count||0) : 0) + 'L';
        const sh = Number(s ? (s.sharpe_ratio || 0) : 0);
        const shEl = document.getElementById('sessionSharpe');
        shEl.textContent = nf(sh, 2); shEl.className = sh > 1 ? 'green' : (sh > 0 ? 'amber' : 'red');
        document.getElementById('sessionAvgDur').textContent = fmtSec(s ? (s.avg_trade_duration_sec || 0) : 0);
      } catch(e){}

      // Equity curve
      try {
        const eq = s && s.equity_curve ? s.equity_curve : [];
        if (eq.length > 0 && eqSeries) {
          eqSeries.setData(eq.map(p => ({ time: p[0], value: p[1] })));
          eqChart.timeScale().fitContent();
        }
      } catch (e) {}
    }

    function renderHealth(h) {
      lastHealthData = h;
      try {
        const ok = !!(h && h.okx_initialized);
        const stalled = !!(h && h.cycle_stalled);
        updateConnBadge(ok, stalled);

        document.getElementById('hKraken').textContent = ok ? 'OK' : ((h && h.okx_enabled) ? 'INIT...' : 'OFF');
        document.getElementById('hKraken').style.color = ok ? '#4ade80' : '#f87171';
        document.getElementById('hUniverse').textContent = (h && h.okx_universe_size !== undefined) ? String(h.okx_universe_size) : '0';
        const r = (h && h.okx_readiness) || {};
        _ccySym = (r.quote_ccy === 'EUR') ? '\u20ac' : '$';
        const _qf = r.quote_free !== undefined && r.quote_free !== null ? r.quote_free : r.usdt_free;
        document.getElementById('hUsdt').textContent = (_qf !== undefined && _qf !== null) ? _ccySym + nf(_qf, 2) : '...';
        document.getElementById('hOhlcv').textContent = r.ohlcv_ok === true ? 'OK' : (r.ohlcv_ok === false ? 'FAIL' : '...');

        try {
          const live = !!(h && h.okx_trading); const mode = (h && h.okx_trading_mode) || '';
          const b = (h && h.okx_trading_blocked_reason) || (r && r.trading_blocked_reason) || '';
          let t = live ? ('LIVE ' + mode) : 'OFF'; if (b) t += ' | ' + b;
          document.getElementById('hTrading').textContent = t;
          document.getElementById('hTrading').style.color = live ? '#4ade80' : '#f87171';
        } catch(e){}

        document.getElementById('hTelegram').textContent = (h && h.telegram_enabled) ? 'ON (' + (h.telegram_failures||0) + ' fails)' : 'OFF';
        document.getElementById('hThreads').textContent = 'MKT=' + !!(h && h.market_thread_alive) + ' DASH=' + !!(h && h.dashboard_thread_alive);

        try {
          const idx = h && h.signal_scan_index || 0, tot = h && h.signal_scan_total || 0;
          const sym = (h && h.signal_scan_symbol) || '', raw = h && h.signal_scan_last_raw || 0;
          const filt = h && h.signal_scan_last_filtered || 0, elapsed = h && h.signal_scan_elapsed_sec || 0;
          const ilq = h && h.illiquid_cache_size || 0;
          let txt = idx + '/' + tot + ' ' + sym + ' | ' + raw + ' raw ' + filt + ' filt | ' + elapsed + 's';
          if (ilq > 0) txt += ' | ilq=' + ilq;
          document.getElementById('hScan').textContent = txt;
        } catch(e){ document.getElementById('hScan').textContent = '...'; }

        try {
          let skipTxt = (h && h.signal_scan_last_skip) || 'none';
          const sd = h && h.signal_scan_skip_dist;
          if (sd && typeof sd === 'object') {
            const top3 = Object.entries(sd).sort((a,b) => b[1]-a[1]).slice(0,3).map(e => e[0]+'='+e[1]).join(' ');
            if (top3) skipTxt += ' | ' + top3;
          }
          document.getElementById('hSkip').textContent = skipTxt;
        } catch(e){}

        // Session bar: balance + mode (from health data)
        try {
          const r = (h && h.okx_readiness) || {};
          const _qf2 = r.quote_free !== undefined && r.quote_free !== null ? r.quote_free : r.usdt_free;
          const bal = (_qf2 !== undefined && _qf2 !== null) ? _ccySym + nf(_qf2, 2) : '...';
          document.getElementById('sessionBalance').textContent = bal;
          const mode = (h && h.okx_trading_mode) || 'paper';
          const mEl = document.getElementById('sessionMode');
          mEl.textContent = mode.toUpperCase();
          mEl.className = mode === 'live' ? 'amber' : 'blue';
        } catch(e){}

        try {
          const heat = h && h.portfolio_heat_usd !== undefined ? _ccySym + nf(h.portfolio_heat_usd, 2) : '...';
          const fee = h && h.taker_fee_pct !== undefined ? nf(h.taker_fee_pct, 2) + '%' : '...';
          document.getElementById('hHeat').textContent = heat + ' / ' + fee;
          const cl = h && h.consecutive_losses !== undefined ? h.consecutive_losses + '/' + (h.max_consecutive_losses || 5) : '...';
          const cw = h && h.consecutive_wins !== undefined ? h.consecutive_wins : 0;
          const streakTxt = cw > 0 ? cl + ' | W' + cw : cl;
          document.getElementById('hLosses').textContent = streakTxt;
          document.getElementById('lossStreak').textContent = streakTxt;
          document.getElementById('heatInfo').textContent = 'Heat: ' + heat;
          // Dynamic stake display
          try {
            const stakeEl = document.getElementById('hStake');
            if (stakeEl && h) {
              const ep = h.effective_position_usd !== undefined ? _ccySym + nf(h.effective_position_usd, 2) : '...';
              const ds = h.dynamic_stake_enabled ? ' (dyn)' : '';
              stakeEl.textContent = ep + ds;
            }
          } catch(e){}
        } catch(e){}

        try {
          const cn = h && h.cycle_n !== undefined ? '#' + h.cycle_n : '';
          const ca = h && h.cycle_avg_sec ? ' ~' + nf(h.cycle_avg_sec,0) + 's' : '';
          const age = h && h.cycle_age_sec !== undefined ? ' | ' + Math.round(h.cycle_age_sec) + 's ago' : '';
          const el = document.getElementById('hCycle');
          el.textContent = cn + ca + age + (stalled ? ' STALLED' : '');
          el.style.color = stalled ? '#ef4444' : '';
        } catch(e){}

        document.getElementById('hLastError').textContent = (h && h.last_cycle_error) || 'none';
        if (h && h.last_cycle_error) document.getElementById('hLastError').style.color = '#f87171';
        else document.getElementById('hLastError').style.color = '';
      } catch(e){}
    }

    async function loadHealth() { renderHealth(await api('/api/health', 4000)); }
    async function loadSummary() { renderSummary(await api('/api/summary', 4000)); }

    let candlesAbort = null;
    async function loadCandles(symbol) {
      const tf = document.getElementById('tfSelect').value;
      try {
        if (!candleSeries) { infoEl.textContent = 'Chart not initialized.'; return; }
        try { if (candlesAbort) candlesAbort.abort(); } catch(e){}
        candlesAbort = new AbortController();
        const t = setTimeout(() => { try { candlesAbort.abort(); } catch(e){} }, 9000);
        const r = await fetch('/api/candles?symbol=' + encodeURIComponent(symbol) + '&tf=' + encodeURIComponent(tf), { signal: candlesAbort.signal });
        clearTimeout(t);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        const candles = data.candles || [];
        const series = candles.map(c => ({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c }));
        candleSeries.setData(series);
        if (lineSeries) lineSeries.setData(candles.map(c => ({ time: c.t, value: c.c })));
        infoEl.textContent = symbol + ' ' + tf + ' | ' + series.length + ' candles';
        try {
          const it = symbolItems.find(x => x.symbol === symbol);
          document.getElementById('symbolSource').textContent = it ? it.market + ' | ' + it.source : '';
        } catch(e){}
        chart.timeScale().fitContent();
        if (series.length > 5) { try { chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, series.length - 75), to: series.length + 5 }); } catch(e){} }
      } catch (e) {
        if (e && (e.name === 'AbortError' || String(e).includes('AbortError'))) return;
        infoEl.textContent = 'Error: ' + e;
      }
    }

    async function loadActiveTrades() {
      const data = await api('/api/active_trades', 4000);
      const tb = document.getElementById('activeTradesTable');
      tb.innerHTML = '';
      const trades = data.trades || [];
      if (trades.length === 0) { tb.innerHTML = '<tr class="empty-row"><td colspan="10">No active trades</td></tr>'; return; }
      trades.forEach(t => {
        const tr = document.createElement('tr');
        const cls = t.side === 'BUY' ? 'green' : 'red';
        const pnlCls = pnlColor(t.pnl);
        const tps = (t.tp_reached || []).join(',') || '-';
        let pct = 0, pColor = '#6366f1';
        try {
          const entry = Number(t.entry), price = Number(t.price), tp1 = Number(t.next_tp || t.entry), sl = Number(t.sl || t.entry);
          const range = Math.abs(tp1 - entry) || 1;
          if (t.side === 'BUY') pct = Math.max(-100, Math.min(100, ((price - entry) / range) * 100));
          else pct = Math.max(-100, Math.min(100, ((entry - price) / range) * 100));
          pColor = pct >= 0 ? '#4ade80' : '#f87171';
        } catch(e){}
        const pctW = Math.min(100, Math.abs(pct));
        tr.innerHTML = '<td>#' + t.id + '</td><td>' + t.symbol + '</td><td class="' + cls + '">' + t.side + '</td>' +
          '<td class="right">' + nf(t.entry, 6) + '</td><td class="right">' + nf(t.price, 6) + '</td>' +
          '<td class="right ' + pnlCls + '">$' + pnlStr(t.pnl) + '</td>' +
          '<td class="right">$' + nf(t.usd_size || 0, 2) + '</td>' +
          '<td><div class="pbar"><div class="pbar-fill" style="width:' + pctW + '%;background:' + pColor + '"></div></div></td>' +
          '<td>' + tps + '</td><td class="right">' + fmtSec(t.time_in_trade_sec || 0) + '</td>';
        tb.appendChild(tr);
      });
    }

    async function loadSignals() {
      const data = await api('/api/signals', 4000);
      const tb = document.getElementById('signalsTable');
      tb.innerHTML = '';
      const sigs = data.signals || [];
      if (sigs.length === 0) { tb.innerHTML = '<tr class="empty-row"><td colspan="11">No signals yet</td></tr>'; return; }
      sigs.forEach(x => {
        const tr = document.createElement('tr');
        const cls = x.side === 'BUY' ? 'green' : 'red';
        const tp1 = Number(x.tp1 ?? (x.targets ? x.targets[0] : 0) ?? 0);
        const tp2 = Number(x.tp2 ?? (x.targets ? x.targets[1] : 0) ?? 0);
        const sl = Number(x.sl ?? x.stop_loss ?? 0);
        const entry = Number(x.entry || 0);
        // R:R = (TP2 - entry) / (entry - SL) for BUY
        let rr = 0;
        try {
          const tpRef = tp2 > 0 ? tp2 : tp1;
          const reward = Math.abs(tpRef - entry);
          const risk = Math.abs(entry - sl);
          if (risk > 0) rr = reward / risk;
        } catch(e){}
        const rrCls = rr >= 2 ? 'green' : (rr >= 1.2 ? 'amber' : 'red');
        const conf = Number(x.conf || 0);
        const confCls = conf >= 95 ? 'conf-high' : (conf >= 90 ? 'conf-med' : 'conf-low');
        const oMode = x.order_mode || '';
        const tagCls = oMode === 'live' ? 'tag-live' : 'tag-paper';
        const oErr = x.order_error || '';
        const statusTxt = oErr ? '<span style="color:#ef4444;font-size:10px">' + oErr.substring(0,30) + '</span>' : (x.order_status || '-');
        tr.innerHTML = '<td>' + fmtTime(x.ts) + '</td><td>#' + x.id + '</td><td>' + x.symbol + '</td>' +
          '<td class="' + cls + '">' + x.side + '</td><td class="right">' + nf(x.entry, 6) + '</td>' +
          '<td class="right">' + nf(tp1, 4) + '/' + nf(tp2, 4) + '</td>' +
          '<td class="right">' + nf(sl, 6) + '</td>' +
          '<td class="' + rrCls + '">' + nf(rr, 2) + ':1</td>' +
          '<td><span class="conf-pill ' + confCls + '">' + nf(conf, 0) + '%</span></td>' +
          '<td><span class="tag ' + tagCls + '">' + oMode + '</span></td>' +
          '<td>' + statusTxt + '</td>';
        tb.appendChild(tr);
      });
    }

    async function loadHistory() {
      const data = await api('/api/trade_history', 4000);
      const tb = document.getElementById('historyTable');
      tb.innerHTML = '';
      const trades = data.trades || [];
      if (trades.length === 0) { tb.innerHTML = '<tr class="empty-row"><td colspan="9">No closed trades yet</td></tr>'; return; }
      trades.forEach(x => {
        const tr = document.createElement('tr');
        const cls = x.result === 'WIN' ? 'tag-win' : 'tag-loss';
        const pnlCls = pnlColor(x.pnl);
        const tps = (x.tp_reached || []).join(',') || '-';
        const pipsCls = (x.pips || 0) >= 0 ? 'green' : 'red';
        const dur = x.duration_sec ? fmtSec(x.duration_sec) : '-';
        tr.innerHTML = '<td>' + fmtTime(x.ts) + '</td><td>#' + x.id + '</td><td>' + x.symbol + '</td>' +
          '<td><span class="tag ' + cls + '">' + x.result + '</span></td>' +
          '<td class="right ' + pnlCls + '">$' + pnlStr(x.pnl) + '</td>' +
          '<td class="right ' + pipsCls + '">' + nf(x.pips||0, 0) + '</td>' +
          '<td>' + (x.reason || '') + '</td><td>' + dur + '</td><td>' + tps + '</td>';
        tb.appendChild(tr);
      });
    }

    async function init() {
      await loadSymbols();
      const marketSel = document.getElementById('marketSelect');
      const sel = document.getElementById('symbolSelect');
      const tfSel = document.getElementById('tfSelect');
      const liveOnly = document.getElementById('liveOnly');
      let inFlight = false, lastCKey = '', lastCMs = 0;

      const refresh = async (force) => {
        if (inFlight) return; inFlight = true;
        try {
          const sym = sel.value, tf = tfSel.value, key = sym + '|' + tf, now = Date.now();
          if (force || key !== lastCKey || (now - lastCMs) >= 6000) { lastCKey = key; lastCMs = now; try { loadCandles(sym); } catch(e){} }
          try { await loadSummary(); } catch(e){ updateConnBadge(false, false); }
          try { await loadHealth(); } catch(e){}
          try { await loadActiveTrades(); } catch(e){}
          try { await loadSignals(); } catch(e){}
          try { await loadHistory(); } catch(e){}
        } finally { inFlight = false; }
      };

      document.getElementById('refreshBtn').onclick = () => refresh(true);
      sel.onchange = () => refresh(true);
      tfSel.onchange = () => refresh(true);
      marketSel.onchange = async () => { renderSymbols(); await refresh(); };
      liveOnly.onchange = async () => { renderSymbols(); await refresh(); };

      await refresh(true);
      setInterval(() => refresh(false), 3000);
    }
    init();
  </script>
</body>
</html>
'''

                @app.get('/')
                def index():
                    resp = make_response(render_template_string(html))
                    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    resp.headers["Pragma"] = "no-cache"
                    resp.headers["Expires"] = "0"
                    return resp

                @app.get('/favicon.ico')
                def favicon():
                    return ("", 204)

                @app.get('/robots.txt')
                def robots_txt():
                    return ("", 204)

                @app.get('/manifest.json')
                def manifest_json():
                    return ("", 204)

                @app.get('/site.webmanifest')
                def site_webmanifest():
                    return ("", 204)

                @app.get('/apple-touch-icon.png')
                def apple_touch_icon_png():
                    return ("", 204)

                @app.get('/apple-touch-icon-precomposed.png')
                def apple_touch_icon_precomposed_png():
                    return ("", 204)

                @app.get('/browserconfig.xml')
                def browserconfig_xml():
                    return ("", 204)

                @app.get('/api/symbols')
                def symbols():
                    market = (request.args.get('market', '') or '').strip().lower()
                    cache_key = market or ""

                    # TTL cache first to avoid repeated sorting/filtering on large universes
                    try:
                        cached = self._dashboard_symbols_cache
                        if isinstance(cached, dict) and cached.get("_key") == cache_key:
                            ts = float(cached.get("_ts", 0.0) or 0.0)
                            if (time.time() - ts) <= float(self._dashboard_symbols_cache_ttl_sec):
                                out = dict(cached)
                                out.pop("_key", None)
                                out.pop("_ts", None)
                                out.setdefault("status", "OK")
                                out["ts"] = int(time.time())
                                return jsonify(out)
                    except Exception:
                        pass

                    acquired = False
                    try:
                        acquired = bool(self._data_lock.acquire(timeout=0.10))
                    except Exception:
                        acquired = False

                    if not acquired:
                        try:
                            out = dict(self._dashboard_symbols_cache or {"symbols": [], "items": []})
                        except Exception:
                            out = {"symbols": [], "items": []}
                        # If cache is empty (cold start), do a best-effort read without locking so UI can populate.
                        try:
                            if not out.get("items"):
                                symbols_list = list(getattr(self, "base_prices", {}).keys())
                                items2 = []
                                for s in sorted(symbols_list):
                                    m = self._symbol_market(s)
                                    if market and market != 'all' and m != market:
                                        continue
                                    items2.append({
                                        "symbol": s,
                                        "market": m,
                                        "source": self._symbol_source(s),
                                    })
                                out = {"symbols": [x["symbol"] for x in items2], "items": items2}
                        except Exception:
                            pass
                        out.pop("_key", None)
                        out.pop("_ts", None)
                        out["status"] = "BUSY"
                        out["ts"] = int(time.time())
                        return jsonify(out)

                    try:
                        symbols_list = list(self.base_prices.keys())
                    finally:
                        try:
                            self._data_lock.release()
                        except Exception:
                            pass

                    items = []
                    for s in sorted(symbols_list):
                        m = self._symbol_market(s)
                        if market and market != 'all' and m != market:
                            continue
                        items.append({
                            "symbol": s,
                            "market": m,
                            "source": self._symbol_source(s),
                        })

                    out = {"symbols": [x["symbol"] for x in items], "items": items, "status": "OK", "ts": int(time.time())}
                    try:
                        self._dashboard_symbols_cache = dict(out)
                        self._dashboard_symbols_cache["_key"] = cache_key
                        self._dashboard_symbols_cache["_ts"] = float(time.time())
                    except Exception:
                        pass
                    return jsonify(out)

                @app.get('/api/summary')
                def summary():
                    try:
                        acquired = False
                        try:
                            acquired = bool(self._data_lock.acquire(timeout=0.25))
                        except Exception:
                            acquired = False

                        if not acquired:
                            try:
                                cached = dict(self._dashboard_summary_cache or {})
                            except Exception:
                                cached = {"status": "BUSY", "ts": int(time.time())}
                            cached.setdefault("status", "BUSY")
                            cached["ts"] = int(time.time())
                            return jsonify(cached)

                        try:
                            stats = self._calc_performance_stats()
                            unrealized = 0.0
                            for _, t in self.active_signals.items():
                                try:
                                    unrealized += float(t.get("pnl", 0.0) or 0.0)
                                except Exception:
                                    pass

                            realized = float(self.total_pnl)
                            total_combined = float(realized + unrealized)
                            session_up = round(time.time() - float(getattr(self, "_session_start_ts", time.time())), 0)
                            payload = {
                                "status": "ACTIVE" if self.running else "STOPPED",
                                "ts": int(time.time()),
                                "active_trades": len(self.active_signals),
                                "max_concurrent_trades": int(self.max_concurrent_trades),
                                "win_rate": float(self.get_win_rate()),
                                "daily_pnl": float(self.daily_pnl),
                                "total_pnl": float(self.total_pnl),
                                "realized_pnl": float(realized),
                                "unrealized_pnl": float(unrealized),
                                "total_pnl_combined": float(total_combined),
                                "total_trades": int(self.total_trades),
                                "win_count": int(stats.get("win_count", 0)),
                                "loss_count": int(stats.get("loss_count", 0)),
                                "profit_factor": float(stats.get("profit_factor", 0.0)),
                                "expectancy": float(stats.get("expectancy", 0.0)),
                                "max_drawdown": float(stats.get("max_drawdown", 0.0)),
                                "avg_win": float(stats.get("avg_win", 0.0)),
                                "avg_loss": float(stats.get("avg_loss", 0.0)),
                                "best_trade": float(stats.get("best_trade", 0.0)),
                                "worst_trade": float(stats.get("worst_trade", 0.0)),
                                "sharpe_ratio": float(stats.get("sharpe_ratio", 0.0)),
                                "avg_trade_duration_sec": float(stats.get("avg_trade_duration_sec", 0.0)),
                                "session_uptime_sec": float(session_up),
                                "equity_curve": stats.get("equity_curve", []),
                                "health": {
                                    "okx": bool(self._kraken_enabled() and self._kraken is not None),
                                    "okx_universe": int(len(self._kraken_universe_symbols or [])),
                                    "telegram": bool(self._telegram_enabled()),
                                    "dash_port": int(getattr(self, "dashboard_port", 0) or 0),
                                    "okx_readiness": dict(getattr(self, "_kraken_last_readiness", {}) or {}),
                                },
                            }
                            try:
                                self._dashboard_summary_cache = dict(payload)
                            except Exception:
                                pass
                            return jsonify(payload)
                        finally:
                            try:
                                self._data_lock.release()
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"/api/summary error: {e}\n{traceback.format_exc()}")
                        return jsonify({"error": "summary_failed", "message": str(e)}), 500

                @app.get('/api/health')
                def health():
                    try:
                        return jsonify(self._health_snapshot())
                    except Exception as e:
                        return jsonify({"ok": False, "error": "health_failed", "message": str(e)}), 500

                # Backwards-compatible endpoint (some scripts expect /api/dashboard)
                @app.get('/api/dashboard')
                def api_dashboard_compat():
                    return summary()

                @app.get('/api/candles')
                def candles():
                    sym = request.args.get('symbol', '')
                    tf = request.args.get('tf', '')
                    tf_sec = self._parse_tf_seconds(tf)
                    cache_key = (str(sym), int(tf_sec))

                    # For OKX symbols, serve OHLCV directly without competing for the main data lock.
                    try:
                        if self._kraken_supported_pair(sym) is not None:
                            data = self._kraken_fetch_ohlcv_for_dashboard(sym, int(tf_sec))
                            if data:
                                view = data[-600:]
                                last_t = int(view[-1].get("t", 0)) if view else 0
                                out = {"symbol": sym, "tf": int(tf_sec), "count": int(len(view)), "last_t": last_t, "candles": view, "status": "OK"}
                                try:
                                    self._dashboard_candles_cache[cache_key] = dict(out)
                                except Exception:
                                    pass
                                return jsonify(out)
                    except Exception:
                        pass
                    acquired = False
                    try:
                        acquired = bool(self._data_lock.acquire(timeout=0.10))
                    except Exception:
                        acquired = False

                    if not acquired:
                        cached = self._dashboard_candles_cache.get(cache_key)
                        if isinstance(cached, dict) and cached.get("candles") is not None:
                            try:
                                out = dict(cached)
                                out["ts"] = int(time.time())
                                out["status"] = "BUSY"
                                return jsonify(out)
                            except Exception:
                                pass
                        return jsonify({"symbol": sym, "tf": int(tf_sec), "count": 0, "last_t": 0, "candles": [], "status": "BUSY"})

                    try:
                        raw = list(self._candles.get(sym, []))
                        last_px = float(self._last_prices.get(sym, self.base_prices.get(sym, 0.0) or 0.0))
                    finally:
                        try:
                            self._data_lock.release()
                        except Exception:
                            pass

                    data = None
                    try:
                        if self._kraken_supported_pair(sym) is not None:
                            data = self._kraken_fetch_ohlcv_for_dashboard(sym, int(tf_sec))
                    except Exception:
                        data = None

                    if not data:
                        data = raw
                        # If we have no candles yet for this symbol, synthesize one so the chart renders.
                        if not data and last_px > 0:
                            now = int(time.time())
                            bucket = now - (now % int(tf_sec or 1))
                            data = [{"t": int(bucket), "o": last_px, "h": last_px, "l": last_px, "c": last_px}]
                        data = data[-1200:]
                        data = self._resample_candles(data, tf_sec)
                    view = data[-600:]
                    last_t = int(view[-1].get("t", 0)) if view else 0
                    out = {"symbol": sym, "tf": int(tf_sec), "count": int(len(view)), "last_t": last_t, "candles": view, "status": "OK"}
                    try:
                        self._dashboard_candles_cache[cache_key] = dict(out)
                    except Exception:
                        pass
                    return jsonify(out)

                @app.get('/api/active_trades')
                def active_trades():
                    acquired = False
                    try:
                        acquired = bool(self._data_lock.acquire(timeout=0.10))
                    except Exception:
                        acquired = False

                    if not acquired:
                        try:
                            cached = dict(self._dashboard_active_trades_cache or {"trades": []})
                        except Exception:
                            cached = {"trades": []}
                        cached["status"] = "BUSY"
                        cached["ts"] = int(time.time())
                        return jsonify(cached)

                    try:
                        active_items = list(self.active_signals.items())
                        last_prices = dict(self._last_prices)
                    finally:
                        try:
                            self._data_lock.release()
                        except Exception:
                            pass

                    trades = []
                    for sym, t in active_items:
                        sig = t.get('signal')
                        entry_time = t.get('entry_time')
                        time_in_trade = 0
                        if entry_time:
                            try:
                                time_in_trade = int((datetime.now() - entry_time).total_seconds())
                            except Exception:
                                time_in_trade = 0

                        price_now = float(last_prices.get(sym, getattr(sig, 'entry_price', 0.0) or 0.0))
                        targets = [float(x) for x in getattr(sig, 'targets', [])[:4]]
                        tp_reached = list(t.get('tp_reached', []))
                        next_tp = None
                        try:
                            idx = len(tp_reached)
                            if 0 <= idx < len(targets):
                                next_tp = targets[idx]
                        except Exception:
                            next_tp = None

                        trades.append({
                            "id": int(getattr(sig, 'signal_id', 0)),
                            "symbol": sym,
                            "side": getattr(sig, 'action', ''),
                            "entry": float(getattr(sig, 'entry_price', 0.0)),
                            "price": price_now,
                            "sl": float(getattr(sig, 'stop_loss', 0.0)),
                            "tp": targets,
                            "next_tp": next_tp,
                            "pnl": float(t.get('pnl', 0.0)),
                            "mfe": float(t.get('mfe', 0.0)),
                            "mae": float(t.get('mae', 0.0)),
                            "amount": float(t.get('amount', 0.0) or 0.0),
                            "usd_size": float(t.get('usd_size', 0.0) or 0.0),
                            "tp_reached": tp_reached,
                            "time_in_trade_sec": time_in_trade,
                            "ts": int(entry_time.timestamp()) if entry_time else int(time.time()),
                        })

                    out = {"trades": trades, "status": "OK", "ts": int(time.time())}
                    try:
                        self._dashboard_active_trades_cache = dict(out)
                    except Exception:
                        pass
                    return jsonify(out)

                @app.get('/api/signals')
                def signals():
                    acquired = False
                    try:
                        acquired = bool(self._data_lock.acquire(timeout=0.10))
                    except Exception:
                        acquired = False

                    if not acquired:
                        try:
                            cached = dict(self._dashboard_signals_cache or {"signals": []})
                        except Exception:
                            cached = {"signals": []}
                        cached["status"] = "BUSY"
                        cached["ts"] = int(time.time())
                        return jsonify(cached)

                    try:
                        sigs = list(self._signal_history)
                    finally:
                        try:
                            self._data_lock.release()
                        except Exception:
                            pass

                    out = {"signals": sigs[-200:][::-1], "status": "OK", "ts": int(time.time())}
                    try:
                        self._dashboard_signals_cache = dict(out)
                    except Exception:
                        pass
                    return jsonify(out)

                @app.get('/api/trade_history')
                def trade_history():
                    acquired = False
                    try:
                        acquired = bool(self._data_lock.acquire(timeout=0.10))
                    except Exception:
                        acquired = False

                    if not acquired:
                        try:
                            cached = dict(self._dashboard_trade_history_cache or {"trades": []})
                        except Exception:
                            cached = {"trades": []}
                        cached["status"] = "BUSY"
                        cached["ts"] = int(time.time())
                        return jsonify(cached)

                    try:
                        trades = list(self._trade_history)
                    finally:
                        try:
                            self._data_lock.release()
                        except Exception:
                            pass

                    out = {"trades": trades[-200:][::-1], "status": "OK", "ts": int(time.time())}
                    try:
                        self._dashboard_trade_history_cache = dict(out)
                    except Exception:
                        pass
                    return jsonify(out)

                @app.get('/api/verification_audit')
                def verification_audit():
                    acquired = False
                    try:
                        acquired = bool(self._data_lock.acquire(timeout=0.10))
                    except Exception:
                        acquired = False

                    if not acquired:
                        try:
                            cached = dict(self._dashboard_audit_cache or {"audit": []})
                        except Exception:
                            cached = {"audit": []}
                        cached["status"] = "BUSY"
                        cached["ts"] = int(time.time())
                        return jsonify(cached)

                    try:
                        items = list(self._verification_audit)
                    finally:
                        try:
                            self._data_lock.release()
                        except Exception:
                            pass

                    out = {"audit": items[-500:][::-1], "status": "OK", "ts": int(time.time())}
                    try:
                        self._dashboard_audit_cache = dict(out)
                    except Exception:
                        pass
                    return jsonify(out)

                logger.info(f"Dashboard running at: http://localhost:{self.dashboard_port}")
                try:
                    # Threaded WSGI server (stdlib) - more stable than Flask dev server in a background thread.
                    from wsgiref.simple_server import make_server as _make_server
                    from socketserver import ThreadingMixIn
                    from wsgiref.simple_server import WSGIServer
                    from wsgiref.simple_server import WSGIRequestHandler

                    class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
                        daemon_threads = True

                    class _QuietHandler(WSGIRequestHandler):
                        def log_message(self, format, *args):
                            return

                    httpd = _make_server('127.0.0.1', int(self.dashboard_port), app, server_class=_ThreadingWSGIServer, handler_class=_QuietHandler)
                    self._dashboard_server = httpd
                    httpd.serve_forever()
                except Exception as e:
                    logger.error(f"Dashboard WSGI server failed, falling back to Flask dev server: {e}\n{traceback.format_exc()}")
                    app.run(host='127.0.0.1', port=self.dashboard_port, debug=False, use_reloader=False, threaded=True)
            except Exception as e:
                logger.error(f"Dashboard server error: {e}\n{traceback.format_exc()}")

        self._dashboard_thread = threading.Thread(target=run_server, daemon=True)
        self._dashboard_thread.start()

    def _load_telegram_config(self) -> None:
        try:
            tok = os.getenv("TELEGRAM_BOT_TOKEN")
            chat = os.getenv("TELEGRAM_CHAT_ID")
            self._telegram_bot_token = tok.strip() if isinstance(tok, str) else tok
            self._telegram_chat_id = chat.strip() if isinstance(chat, str) else chat
        except Exception as e:
            logger.error(f"Telegram config load failed: {e}")

    def _telegram_enabled(self) -> bool:
        if self._telegram_disabled_for_session:
            return False
        enabled_flag = str(os.getenv("TELEGRAM_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled_flag:
            return False
        return bool(self._telegram_bot_token and self._telegram_chat_id)

    def _is_metal_symbol(self, symbol: str) -> bool:
        return symbol in {"XAUUSD", "XAGUSD"}

    def _is_crypto_symbol(self, symbol: str) -> bool:
        try:
            if not symbol:
                return False
            q = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
        except Exception:
            q = "EUR"
        try:
            if q and symbol.endswith(str(q)):
                return True
        except Exception:
            pass
        # Back-compat common quotes
        return bool(symbol.endswith("USDT") or symbol.endswith("USDC"))

    def _market_profile(self, symbol: str) -> Optional[Dict[str, float]]:
        if symbol == "XAUUSD":
            return {"entry_step": 5.0, "range": 5.0, "sl": 16.0, "tp1": 4.0, "tp2": 7.0, "tp3": 10.0, "tp4": 16.0, "dec": 0}
        if symbol == "XAGUSD":
            return {"entry_step": 0.05, "range": 0.05, "sl": 0.16, "tp1": 0.04, "tp2": 0.07, "tp3": 0.10, "tp4": 0.16, "dec": 2}
        if symbol in {"EURUSD", "GBPUSD"}:
            return {"entry_step": 0.0005, "range": 0.0005, "sl": 0.0016, "tp1": 0.0004, "tp2": 0.0007, "tp3": 0.0010, "tp4": 0.0016, "dec": 5}
        if symbol == "USDJPY":
            return {"entry_step": 0.05, "range": 0.05, "sl": 0.16, "tp1": 0.04, "tp2": 0.07, "tp3": 0.10, "tp4": 0.16, "dec": 2}
        if symbol in {"NAS100"}:
            return {"entry_step": 5.0, "range": 5.0, "sl": 16.0, "tp1": 4.0, "tp2": 7.0, "tp3": 10.0, "tp4": 16.0, "dec": 0}
        if symbol in {"SPX500"}:
            return {"entry_step": 1.0, "range": 1.0, "sl": 3.2, "tp1": 0.8, "tp2": 1.4, "tp3": 2.0, "tp4": 3.2, "dec": 0}
        if symbol in {"USOIL", "UKOIL"}:
            return {"entry_step": 0.05, "range": 0.05, "sl": 0.16, "tp1": 0.04, "tp2": 0.07, "tp3": 0.10, "tp4": 0.16, "dec": 2}
        return None

    def _kraken_effective_position_usd(self) -> float:
        try:
            if not (self._kraken_trading_enabled and str(self._kraken_trading_mode).lower() == "live"):
                return float(self._kraken_position_usd)

            warmup_sec = max(0.0, float(self._kraken_live_warmup_minutes) * 60.0)
            elapsed = max(0.0, time.time() - float(self._kraken_live_start_ts or time.time()))
            if warmup_sec > 0 and elapsed < warmup_sec:
                return float(self._kraken_live_warmup_usd)

            if not bool(self._kraken_live_promoted):
                self._kraken_live_promoted = True
                try:
                    print(f"✅ LIVE WARMUP COMPLETE: switching to compound position sizing")
                except Exception:
                    pass

            # ── COMPOUND GROWTH: position size = % of real balance ──
            # As balance grows from €50 → €500 → €5000 → €50000,
            # position sizes grow proportionally, reinvesting profits automatically.
            base_usd = float(self._kraken_live_target_usd)
            try:
                compound_pct = float(getattr(self, "_compound_position_pct", 0) or 0)
                if compound_pct > 0 and self._kraken is not None:
                    quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
                    real_balance = self._kraken_free_balance(quote)
                    if real_balance > 0:
                        # Position = compound_pct% of balance, but respect min/max
                        compound_usd = real_balance * (compound_pct / 100.0)
                        min_pos = float(getattr(self, "_compound_min_usd", 3.0) or 3.0)
                        max_pos = float(getattr(self, "_compound_max_usd", 0) or 0)
                        compound_usd = max(min_pos, compound_usd)
                        if max_pos > 0:
                            compound_usd = min(max_pos, compound_usd)
                        # Must leave room for max_concurrent_trades positions + fees
                        max_trades = max(1, int(getattr(self, "max_concurrent_trades", 3) or 3))
                        fee_buf = 1.0 + (abs(float(getattr(self, "_kraken_fee_buffer_pct", 0.30) or 0.30)) / 100.0)
                        max_safe = (real_balance / (max_trades * fee_buf))
                        compound_usd = min(compound_usd, max_safe)
                        base_usd = max(min_pos, compound_usd)
            except Exception:
                pass

            # Dynamic stake scaling (Freqtrade custom_stake_amount pattern)
            # After consecutive losses: reduce position to limit drawdown
            # After consecutive wins: gradually increase (compound gains)
            try:
                if bool(getattr(self, "_dynamic_stake_enabled", True)):
                    losses = int(getattr(self, "_consecutive_losses", 0) or 0)
                    wins = int(getattr(self, "_consecutive_wins", 0) or 0)
                    if losses >= 3:
                        scale = max(0.50, 1.0 - (losses * 0.10))
                        base_usd = max(float(self._kraken_live_warmup_usd), base_usd * scale)
                    elif wins >= 3:
                        scale = min(1.30, 1.0 + (wins * 0.05))
                        base_usd = base_usd * scale
            except Exception:
                pass

            # ── EQUITY CURVE PROTECTION (account management) ──
            # If daily PnL is negative, reduce position size proportionally.
            # If daily PnL is positive, allow full size (protect gains by not over-risking).
            # This ensures the EUR balance is always growing, not shrinking.
            try:
                daily_pnl = float(getattr(self, "daily_pnl", 0.0) or 0.0)
                daily_stop = float(getattr(self, "_daily_stop_usd", 0.0) or 0.0)
                if daily_pnl < 0 and daily_stop > 0:
                    # Scale down: at -50% of daily stop → use 50% position
                    loss_ratio = min(1.0, abs(daily_pnl) / daily_stop)
                    equity_scale = max(0.30, 1.0 - (loss_ratio * 0.70))
                    base_usd = base_usd * equity_scale
                elif daily_pnl > 0:
                    # Winning day: lock in profit by NOT increasing beyond normal size
                    # Just use the standard compound amount
                    pass
            except Exception:
                pass

            return base_usd
        except Exception:
            return float(getattr(self, "_kraken_position_usd", 10.0) or 10.0)

    def _send_telegram_message_sync(self, text: str) -> bool:
        try:
            import requests

            api_base = str(os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org") or "https://api.telegram.org").strip()
            url = f"{api_base}/bot{self._telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self._telegram_chat_id,
                "text": text,
            }
            headers = {
                "Connection": "close",
                "Content-Type": "application/json",
            }
            last_error = None
            max_retries = int(os.getenv("TELEGRAM_MAX_RETRIES", "3") or "3")
            disable_after = int(os.getenv("TELEGRAM_DISABLE_AFTER_FAILURES", "6") or "6")
            base_sleep = float(os.getenv("TELEGRAM_RETRY_BASE_SLEEP_SEC", "2.0") or "2.0")
            connect_timeout = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT_SEC", "6") or "6")
            read_timeout = float(os.getenv("TELEGRAM_READ_TIMEOUT_SEC", "12") or "12")

            for attempt in range(1, max_retries + 1):
                try:
                    # Fresh session per attempt — reusing a session after ConnectionResetError(10054)
                    # causes the same stale TCP socket to be reused, failing repeatedly.
                    session = requests.Session()
                    session.trust_env = False
                    resp = session.post(url, json=payload, headers=headers, timeout=(connect_timeout, read_timeout))
                    session.close()
                    if resp.status_code == 200:
                        self._telegram_failure_count = 0
                        self._telegram_last_error = ""
                        return True
                    last_error = f"HTTP {resp.status_code} {resp.text[:300]}"
                    self._telegram_last_error = str(last_error)
                    logger.warning(f"Telegram send failed (attempt {attempt}/{max_retries}): {last_error}")
                except Exception as e:
                    last_error = str(e)
                    self._telegram_last_error = str(last_error)
                    logger.warning(f"Telegram send exception (attempt {attempt}/{max_retries}): {e}")
                    try:
                        session.close()
                    except Exception:
                        pass

                try:
                    # Exponential backoff: 2s, 4s, 8s ...
                    time.sleep(base_sleep * (2 ** (attempt - 1)))
                except Exception:
                    pass

            if last_error:
                self._telegram_failure_count += 1
                self._telegram_last_error = str(last_error)
                logger.error(f"Telegram send failed after retries: {last_error}")

            if self._telegram_failure_count >= disable_after:
                self._telegram_disabled_for_session = True
                logger.error("Telegram disabled for this session after repeated failures")
            return False
        except Exception as e:
            self._telegram_last_error = str(e)
            logger.error(f"Telegram send exception: {e}")
            return False

    async def _maybe_send_startup_test(self) -> None:
        try:
            if getattr(self, "_telegram_startup_test_sent", False):
                return
            if not self._telegram_enabled():
                self._telegram_startup_test_sent = True
                return

            self._telegram_startup_test_sent = True
            threading.Thread(target=self._send_telegram_message_sync, args=("✅ ROBOXRP PRO v13.2 started. Telegram connected.",), daemon=True).start()
            print("📱 Telegram startup test: sent (non-blocking)")
        except Exception as e:
            logger.error(f"Telegram startup test exception: {e}")

    def _kraken_supported_pair(self, symbol: str) -> Optional[str]:
        """Map internal symbols to ccxt OKX symbols."""
        if not symbol:
            return None

        # Already in ccxt format (e.g. "BTC/USDC") — return as-is
        if "/" in symbol:
            return symbol

        try:
            quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
        except Exception:
            quote = "EUR"
        if not quote:
            quote = "EUR"

        if quote and symbol.endswith(quote):
            base = symbol[: -len(quote)]
            if not base:
                return None
            return f"{base}/{quote}"
        if symbol.endswith("USD"):
            base = symbol[:-3]
            return f"{base}/USD"
        return None

    def _kraken_refresh_universe(self) -> None:
        try:
            if self._kraken is None:
                return
            markets = getattr(self._kraken, "markets", {}) or {}
            try:
                quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
            except Exception:
                quote = "EUR"
            if not quote:
                quote = "EUR"
            out: List[str] = []
            for pair, m in markets.items():
                try:
                    if not isinstance(m, dict):
                        continue
                    if not bool(m.get("active", True)):
                        continue
                    if not bool(m.get("spot", True)):
                        continue
                    if not (isinstance(pair, str) and pair.endswith(f"/{quote}")):
                        continue
                    base = str(m.get("base") or pair.split("/", 1)[0]).upper()
                    # Basic safety filters
                    bad = ("3L" in base) or ("3S" in base) or (len(base) >= 2 and base.endswith(("UP", "DOWN", "BULL", "BEAR")))
                    if bad:
                        continue
                    sym = f"{base}{quote}"
                    out.append(sym)
                except Exception:
                    continue

            out = sorted(list(dict.fromkeys(out)))
            # Keep only symbols we can map back to a Kraken pair
            out = [s for s in out if self._kraken_supported_pair(s) is not None]
            self._kraken_universe_symbols = out
            self._kraken_universe_last_refresh_ts = float(time.time())

            try:
                self._apply_kraken_only_universe()
            except Exception:
                pass
        except Exception:
            return

    def _kraken_readiness_check(self, force: bool = False) -> Dict[str, object]:
        try:
            now = time.time()
            if (not force) and self._kraken_last_readiness and float(self._kraken_readiness_refresh_sec) > 0:
                if (now - float(self._kraken_last_readiness_ts or 0.0)) < float(self._kraken_readiness_refresh_sec):
                    out = dict(self._kraken_last_readiness)
                    try:
                        out["universe_size"] = int(len(self._kraken_universe_symbols or []))
                    except Exception:
                        pass
                    return out

            self._ensure_kraken()
            try:
                quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
            except Exception:
                quote = "EUR"
            if not quote:
                quote = "EUR"
            ready: Dict[str, object] = {
                "ts": int(now),
                "okx_initialized": bool(self._kraken is not None),
                "okx_enabled": bool(self._kraken_enabled()),
                "trading_enabled": bool(self._kraken_trading_enabled),
                "trading_mode": str(self._kraken_trading_mode),
                "quote_ccy": str(quote),
            }

            if self._kraken is None:
                self._kraken_last_readiness = dict(ready)
                self._kraken_last_readiness_ts = float(now)
                return dict(ready)

            try:
                ready["universe_size"] = int(len(self._kraken_universe_symbols or []))
            except Exception:
                pass

            # If markets are loaded but universe hasn't been built yet, build it now.
            try:
                if int(ready.get("universe_size") or 0) <= 0:
                    markets = getattr(self._kraken, "markets", None)
                    if not isinstance(markets, dict) or not markets:
                        try:
                            self._kraken.load_markets()
                        except Exception as e:
                            logger.warning("OKX load_markets failed during readiness: %s", e)
                    self._kraken_refresh_universe()
                    ready["universe_size"] = int(len(self._kraken_universe_symbols or []))
            except Exception:
                pass

            # Balance check (only meaningful if trading is enabled)
            try:
                if bool(self._kraken_trading_enabled):
                    q_free = float(self._kraken_free_balance(str(quote)))
                    ready["quote_free"] = float(q_free)
                else:
                    ready["quote_free"] = None
            except Exception:
                ready["quote_free"] = None

            try:
                # Preserve existing blocked reason like auth_failed.
                blocked = str(getattr(self, "_kraken_trading_blocked_reason", "") or "")
                if not blocked:
                    if bool(self._kraken_trading_enabled):
                        uf = ready.get("quote_free")
                        if uf is None:
                            blocked = "balance_unknown"
                        else:
                            try:
                                if float(uf) <= 0.0:
                                    blocked = "no_quote"
                            except Exception:
                                blocked = "balance_unknown"
                ready["trading_blocked_reason"] = str(blocked)
                self._kraken_trading_blocked_reason = str(blocked)
            except Exception:
                pass

            # OHLCV self-check (public endpoint)
            try:
                q = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
                default_sym = f"BTC{q}" if q else "BTCEUR"
                sym = str(os.getenv("OKX_TEST_SYMBOL", default_sym) or default_sym)
                o = self._kraken_fetch_ohlcv_cached(sym, "1m", 20)
                ready["ohlcv_ok"] = bool(o and isinstance(o, list) and len(o) >= 5)
            except Exception:
                ready["ohlcv_ok"] = False

            self._kraken_last_readiness = dict(ready)
            self._kraken_last_readiness_ts = float(now)
            return dict(ready)
        except Exception as e:
            out = {"ts": int(time.time()), "error": str(e)}
            try:
                self._kraken_last_readiness = dict(out)
                self._kraken_last_readiness_ts = float(time.time())
            except Exception:
                pass
            return out

    def _health_snapshot(self) -> Dict[str, object]:
        try:
            kraken_ok = bool(self._kraken_enabled() and self._kraken is not None)
        except Exception:
            kraken_ok = False

        # Ensure readiness is populated for dashboard/health even between cycles.
        try:
            if bool(self._kraken_enabled()):
                lr = getattr(self, "_kraken_last_readiness", None)
                if not isinstance(lr, dict) or not lr:
                    try:
                        self._kraken_readiness_check(force=False)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            universe_n = int(len(self._kraken_universe_symbols or []))
        except Exception:
            universe_n = 0

        try:
            ohlcv_cache_n = int(len(self._kraken_ohlcv_cache or {}))
        except Exception:
            ohlcv_cache_n = 0

        try:
            market_thread_alive = bool(self._market_thread and self._market_thread.is_alive())
        except Exception:
            market_thread_alive = False

        try:
            dash_thread_alive = bool(self._dashboard_thread and self._dashboard_thread.is_alive())
        except Exception:
            dash_thread_alive = False

        try:
            telegram_ok = bool(self._telegram_enabled())
        except Exception:
            telegram_ok = False

        return {
            "ts": int(time.time()),
            "running": bool(self.running),
            "dashboard_port": int(getattr(self, "dashboard_port", 0) or 0),
            "market_thread_alive": bool(market_thread_alive),
            "dashboard_thread_alive": bool(dash_thread_alive),
            "telegram_enabled": bool(telegram_ok),
            "telegram_failures": int(getattr(self, "_telegram_failure_count", 0) or 0),
            "telegram_last_error": str(getattr(self, "_telegram_last_error", "") or ""),
            "okx_enabled": bool(self._kraken_enabled()),
            "okx_initialized": bool(kraken_ok),
            "okx_trading": bool(getattr(self, "_kraken_trading_enabled", False)),
            "okx_trading_mode": str(getattr(self, "_kraken_trading_mode", "") or ""),
            "okx_trading_blocked_reason": str(getattr(self, "_kraken_trading_blocked_reason", "") or ""),
            "okx_universe_size": int(universe_n),
            "okx_universe_last_refresh_ts": int(getattr(self, "_kraken_universe_last_refresh_ts", 0.0) or 0.0),
            "okx_ohlcv_cache_size": int(ohlcv_cache_n),
            "okx_readiness": dict(getattr(self, "_kraken_last_readiness", {}) or {}),
            "last_cycle_ts": int(getattr(self, "_last_cycle_ts", 0.0) or 0.0),
            "last_cycle_error_ts": int(getattr(self, "_last_cycle_error_ts", 0.0) or 0.0),
            "last_cycle_error": str(getattr(self, "_last_cycle_error", "") or ""),
            "last_cycle_stage": str(getattr(self, "_last_cycle_stage", "") or ""),
            "last_cycle_stage_ts": int(getattr(self, "_last_cycle_stage_ts", 0.0) or 0.0),
            "signal_scan_symbol": str(getattr(self, "_signal_scan_symbol", "") or ""),
            "signal_scan_index": int(getattr(self, "_signal_scan_index", 0) or 0),
            "signal_scan_total": int(getattr(self, "_signal_scan_total", 0) or 0),
            "signal_scan_last_skip": str(getattr(self, "_signal_scan_last_skip", "") or ""),
            "signal_scan_last_ohlcv_ms": dict(getattr(self, "_signal_scan_last_ohlcv_ms", {}) or {}),
            "signal_scan_last_raw": int(getattr(self, "_signal_scan_last_raw", 0) or 0),
            "signal_scan_last_filtered": int(getattr(self, "_signal_scan_last_filtered", 0) or 0),
            "signal_scan_last_gates": dict(getattr(self, "_signal_scan_last_gates", {}) or {}),
            "signal_scan_skip_dist": dict(getattr(self, "_signal_scan_skip_dist", {}) or {}),
            "illiquid_cache_size": int(len(getattr(self, "_illiquid_cache", {}) or {})),
            "signal_scan_started_ts": int(getattr(self, "_signal_scan_started_ts", 0.0) or 0.0),
            "signal_scan_elapsed_sec": round(max(0.0, time.time() - float(getattr(self, "_signal_scan_started_ts", 0.0) or 0.0)), 1) if float(getattr(self, "_signal_scan_started_ts", 0.0) or 0.0) > 0 else 0,
            "active_trades": int(len(getattr(self, "active_signals", {}) or {})),
            "total_pnl": float(getattr(self, "total_pnl", 0.0) or 0.0),
            "daily_pnl": float(getattr(self, "daily_pnl", 0.0) or 0.0),
            "portfolio_heat_usd": round(self._current_portfolio_heat(), 2),
            "consecutive_losses": int(getattr(self, "_consecutive_losses", 0) or 0),
            "consecutive_wins": int(getattr(self, "_consecutive_wins", 0) or 0),
            "max_consecutive_losses": int(getattr(self, "_max_consecutive_losses", 5) or 5),
            "dynamic_stake_enabled": bool(getattr(self, "_dynamic_stake_enabled", False)),
            "effective_position_usd": round(float(self._kraken_effective_position_usd()), 2),
            "compound_enabled": bool(float(getattr(self, "_compound_position_pct", 0) or 0) > 0),
            "compound_pct": float(getattr(self, "_compound_position_pct", 0) or 0),
            "taker_fee_pct": float(getattr(self, "_taker_fee_pct", 0.10) or 0.10),
            "cycle_age_sec": round(time.time() - float(getattr(self, "_last_cycle_ts", 0.0) or 0.0), 0) if float(getattr(self, "_last_cycle_ts", 0.0) or 0.0) > 0 else 0,
            "cycle_stalled": bool(float(getattr(self, "_last_cycle_ts", 0.0) or 0.0) > 0 and (time.time() - float(self._last_cycle_ts)) > 300),
            "cycle_n": int(getattr(self, "_cycle_count", 0) or 0),
            "cycle_avg_sec": round(sum(getattr(self, "_cycle_times", []) or []) / max(1, len(getattr(self, "_cycle_times", []) or [])), 1) if getattr(self, "_cycle_times", None) else 0,
            "session_peak_balance": round(float(getattr(self, "_session_peak_balance", 0.0) or 0.0), 2),
            "session_start_balance": round(float(getattr(self, "_session_start_balance", 0.0) or 0.0), 2),
            "session_drawdown_pct": round(((float(self._session_peak_balance) - float(self.daily_pnl + self._session_peak_balance)) / max(0.01, float(self._session_peak_balance))) * 100.0, 1) if float(getattr(self, "_session_peak_balance", 0) or 0) > 0 and float(getattr(self, "daily_pnl", 0) or 0) < 0 else 0.0,
            "max_drawdown_pct": float(getattr(self, "_max_drawdown_pct", 20.0) or 20.0),
            "drawdown_paused": bool(getattr(self, "_drawdown_paused", False)),
            "patient_orders_enabled": bool(getattr(self, "_patient_orders_enabled", False)),
            "pending_orders_count": len(getattr(self, "_pending_orders", {}) or {}),
            "pending_orders": [{
                "symbol": str(k),
                "side": str(v.get("side", "")),
                "price": round(float(v.get("price", 0) or 0), 8),
                "age_sec": int(time.time() - float(v.get("placed_ts", 0) or 0)),
                "amends": int(v.get("amend_count", 0) or 0),
            } for k, v in (getattr(self, "_pending_orders", {}) or {}).items()],
        }

    def _kraken_fetch_ohlcv_cached(self, symbol: str, tf: str, limit: int) -> Optional[List[List[float]]]:
        try:
            self._ensure_kraken()
            if self._kraken is None:
                return None
            pair = self._kraken_supported_pair(symbol)
            if not pair:
                return None
            limit_i = int(max(10, min(1000, int(limit))))
            key = (pair, str(tf), limit_i)
            now = time.time()
            cached = self._kraken_ohlcv_cache.get(key)
            if cached is not None:
                ts, data = cached
                if (now - float(ts)) <= float(self._kraken_ohlcv_cache_ttl_sec):
                    return data

            try:
                timeout_s = float(getattr(self, "_kraken_call_timeout_sec", 12.0) or 12.0)
            except Exception:
                timeout_s = 12.0

            # Use dedicated thread to avoid blocking on stalled _ccxt_executor
            _ohlcv_box = [None]
            def _do_fetch_ohlcv():
                try:
                    _ohlcv_box[0] = self._kraken.fetch_ohlcv(pair, str(tf), None, limit_i)
                except Exception:
                    pass
            _t = threading.Thread(target=_do_fetch_ohlcv, daemon=True)
            _t.start()
            _t.join(timeout=max(1.0, min(timeout_s, 10.0)))
            ohlcv = _ohlcv_box[0]
            if _t.is_alive():
                self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                return None
            if ohlcv is None:
                return None
            if not isinstance(ohlcv, list) or not ohlcv:
                return None
            data = [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] if len(x) > 5 else 0.0)] for x in ohlcv]
            self._kraken_ohlcv_cache[key] = (now, data)
            # Evict stale entries to prevent unbounded memory growth
            try:
                max_cache = 500
                if len(self._kraken_ohlcv_cache) > max_cache:
                    stale_keys = [
                        k for k, (ts, _) in list(self._kraken_ohlcv_cache.items())
                        if (now - float(ts)) > float(self._kraken_ohlcv_cache_ttl_sec) * 3
                    ]
                    for sk in stale_keys:
                        self._kraken_ohlcv_cache.pop(sk, None)
            except Exception:
                pass
            return data
        except Exception:
            return None

    def _ema_series(self, values: List[float], period: int) -> List[float]:
        p = int(max(1, period))
        out: List[float] = []
        k = 2.0 / (p + 1.0)
        ema = None
        for v in values:
            if ema is None:
                ema = float(v)
            else:
                ema = float(v) * k + float(ema) * (1.0 - k)
            out.append(float(ema))
        return out

    def _rsi_last(self, closes: List[float], period: int) -> Optional[float]:
        try:
            p = int(max(2, period))
            if len(closes) < (p + 2):
                return None
            # Wilder's smoothed RSI (industry standard)
            # Seed with simple average over first 'p' deltas
            avg_gain = 0.0
            avg_loss = 0.0
            for i in range(1, p + 1):
                ch = float(closes[i]) - float(closes[i - 1])
                if ch >= 0:
                    avg_gain += ch
                else:
                    avg_loss += abs(ch)
            avg_gain /= float(p)
            avg_loss /= float(p)
            # Smooth over remaining bars
            for i in range(p + 1, len(closes)):
                ch = float(closes[i]) - float(closes[i - 1])
                if ch >= 0:
                    avg_gain = (avg_gain * (p - 1) + ch) / float(p)
                    avg_loss = (avg_loss * (p - 1)) / float(p)
                else:
                    avg_gain = (avg_gain * (p - 1)) / float(p)
                    avg_loss = (avg_loss * (p - 1) + abs(ch)) / float(p)
            if avg_gain <= 0 and avg_loss <= 0:
                return 50.0
            if avg_loss <= 0:
                return 100.0
            rs = avg_gain / max(1e-12, avg_loss)
            rsi = 100.0 - (100.0 / (1.0 + rs))
            return float(rsi)
        except Exception:
            return None

    # ── Smart Market Intelligence helpers (top-bot patterns) ──────────────

    def _detect_bullish_pattern(self, candles_5m: list) -> Tuple[bool, str]:
        """Detect bullish candlestick patterns on 5m data.
        Returns (pattern_found, pattern_name).
        Checks last 3 candles for: hammer, bullish engulfing, morning star, double bottom."""
        try:
            if not candles_5m or len(candles_5m) < 5:
                return False, ""
            # Last 3 candles: [O, H, L, C, V] format (indices 1-4 of OHLCV)
            c0 = candles_5m[-1]  # current
            c1 = candles_5m[-2]  # previous
            c2 = candles_5m[-3]  # 2 bars ago
            o0, h0, l0, cl0 = float(c0[1]), float(c0[2]), float(c0[3]), float(c0[4])
            o1, h1, l1, cl1 = float(c1[1]), float(c1[2]), float(c1[3]), float(c1[4])
            o2, h2, l2, cl2 = float(c2[1]), float(c2[2]), float(c2[3]), float(c2[4])
            body0 = cl0 - o0
            body1 = cl1 - o1
            range0 = h0 - l0 if h0 > l0 else 1e-12
            range1 = h1 - l1 if h1 > l1 else 1e-12

            # Hammer: small body at top, long lower wick (2x+ body), bullish after downtrend
            lower_wick0 = min(o0, cl0) - l0
            upper_wick0 = h0 - max(o0, cl0)
            if (lower_wick0 > abs(body0) * 2.0 and upper_wick0 < abs(body0) * 0.5
                    and cl1 < o1 and cl2 < o2):  # prior 2 candles bearish
                return True, "HAMMER"

            # Bullish engulfing: prev bearish, current bullish body fully covers prev
            if (body1 < 0 and body0 > 0
                    and cl0 > o1 and o0 <= cl1
                    and abs(body0) > abs(body1) * 0.8):
                return True, "ENGULF"

            # Morning star: bearish → small body (doji) → bullish
            body2 = cl2 - o2
            if (body2 < 0 and abs(body1) < range1 * 0.3  # doji/small body
                    and body0 > 0 and cl0 > (o2 + cl2) / 2):
                return True, "MSTAR"

            # Higher low with bullish close: l0 > l1, current bullish
            if l0 > l1 and body0 > 0 and cl0 > h1 * 0.998:
                return True, "HL_BREAK"

            return False, ""
        except Exception:
            return False, ""

    def _detect_bearish_pattern(self, candles_5m: list) -> Tuple[bool, str]:
        """Detect bearish candlestick patterns (mirror of bullish)."""
        try:
            if not candles_5m or len(candles_5m) < 5:
                return False, ""
            c0 = candles_5m[-1]
            c1 = candles_5m[-2]
            c2 = candles_5m[-3]
            o0, h0, l0, cl0 = float(c0[1]), float(c0[2]), float(c0[3]), float(c0[4])
            o1, h1, l1, cl1 = float(c1[1]), float(c1[2]), float(c1[3]), float(c1[4])
            o2, h2, l2, cl2 = float(c2[1]), float(c2[2]), float(c2[3]), float(c2[4])
            body0 = cl0 - o0
            body1 = cl1 - o1
            range0 = h0 - l0 if h0 > l0 else 1e-12

            # Shooting star
            upper_wick0 = h0 - max(o0, cl0)
            lower_wick0 = min(o0, cl0) - l0
            if (upper_wick0 > abs(body0) * 2.0 and lower_wick0 < abs(body0) * 0.5
                    and cl1 > o1 and cl2 > o2):
                return True, "SHOOT_STAR"

            # Bearish engulfing
            if (body1 > 0 and body0 < 0
                    and cl0 < o1 and o0 >= cl1
                    and abs(body0) > abs(body1) * 0.8):
                return True, "BEAR_ENGULF"

            return False, ""
        except Exception:
            return False, ""

    def _volume_spike_detected(self, candles_5m: list) -> Tuple[bool, float]:
        """Check if recent volume is significantly above average (buying pressure).
        Returns (spike_detected, spike_ratio).
        Top bots use 1.5x avg vol in last 3 candles vs 20-candle avg."""
        try:
            if not candles_5m or len(candles_5m) < 10:
                return False, 0.0
            vols = [float(x[5]) for x in candles_5m[-20:] if len(x) > 5 and float(x[5]) > 0]
            if len(vols) < 8:
                return False, 0.0
            avg_vol = sum(vols) / len(vols)
            if avg_vol <= 0:
                return False, 0.0
            # Average of last 3 candles (not just last one — reduces noise)
            recent_avg = sum(vols[-3:]) / 3.0
            ratio = recent_avg / avg_vol
            return ratio >= 1.5, round(ratio, 2)
        except Exception:
            return False, 0.0

    def _price_making_higher_lows(self, closes: list, n: int = 10) -> bool:
        """Check if price is making higher lows (bullish structure).
        Compares lows of last N/2 candles vs previous N/2 candles."""
        try:
            if len(closes) < n:
                return False
            half = n // 2
            recent = closes[-half:]
            older = closes[-n:-half]
            recent_low = min(recent)
            older_low = min(older)
            return recent_low > older_low
        except Exception:
            return False

    def _price_making_lower_highs(self, closes: list, n: int = 10) -> bool:
        """Check if price is making lower highs (bearish structure)."""
        try:
            if len(closes) < n:
                return False
            half = n // 2
            recent = closes[-half:]
            older = closes[-n:-half]
            return max(recent) < max(older)
        except Exception:
            return False

    def _coin_is_active(self, candles_5m: list, candles_1h: list) -> Tuple[bool, str]:
        """Smart coin activity filter: reject stagnant coins that waste time.
        Checks: (1) 1h price range >= 0.15% in last 12h — if yes, coin is active.
        (2) Only if 1h data is insufficient, check 5m diversity.
        (3) Volume drought check on 5m.
        EUR/OKX EEA has low 5m liquidity but real 1h data — trust 1h."""
        try:
            # Check 1: 1h price range over last 12 candles (12 hours)
            # If 1h shows real movement, coin is ACTIVE — skip 5m check entirely.
            # Bot already falls back to 1h for entry data when 5m is flat.
            _1h_ok = False
            if candles_1h and len(candles_1h) >= 6:
                _h12 = candles_1h[-12:] if len(candles_1h) >= 12 else candles_1h
                highs = [float(c[2]) for c in _h12]
                lows = [float(c[3]) for c in _h12]
                price_range_12h = (max(highs) - min(lows)) / max(1e-12, min(lows)) * 100.0
                if price_range_12h >= 0.15:
                    _1h_ok = True  # 1h has real movement — coin is active
                else:
                    return False, f"STALE_1H({price_range_12h:.2f}%)"

            # If 1h confirmed active, skip 5m diversity check (EUR pairs have low 5m liquidity)
            if _1h_ok:
                # Only reject on extreme volume drought (100% zero over 20 candles = truly dead)
                # EUR/OKX EEA: 5m volume is sporadic, 1-2 candles with vol out of 10 is normal.
                # Wider window (20 candles = 100min) gives more stable assessment.
                if candles_5m and len(candles_5m) >= 10:
                    _vd_window = candles_5m[-20:] if len(candles_5m) >= 20 else candles_5m[-10:]
                    _vd_vols = [float(c[5]) if len(c) > 5 else 0.0 for c in _vd_window]
                    _vd_zero = sum(1 for v in _vd_vols if v <= 0)
                    _vd_total = len(_vd_vols)
                    if _vd_zero >= _vd_total:  # 100% zero = truly dead
                        return False, f"VOL_DROUGHT({_vd_zero}/{_vd_total})"
                return True, ""

            # Fallback: no 1h data — check 5m diversity (relaxed: 15% unique, was 30%)
            if candles_5m and len(candles_5m) >= 10:
                recent_closes = [float(c[4]) for c in candles_5m[-20:]]
                unique_pct = len(set(recent_closes)) / len(recent_closes) * 100.0
                if unique_pct < 15:
                    return False, f"STALE_5M(uniq={unique_pct:.0f}%)"

            # Check volume drought (fallback path — no 1h)
            if candles_5m and len(candles_5m) >= 10:
                _vd_window = candles_5m[-20:] if len(candles_5m) >= 20 else candles_5m[-10:]
                _vd_vols = [float(c[5]) if len(c) > 5 else 0.0 for c in _vd_window]
                _vd_zero = sum(1 for v in _vd_vols if v <= 0)
                _vd_total = len(_vd_vols)
                if _vd_zero >= _vd_total:
                    return False, f"VOL_DROUGHT({_vd_zero}/{_vd_total})"

            return True, ""
        except Exception:
            return True, ""

    # ── Modern Bot Indicators (Freqtrade/3Commas/Jesse level) ────────────

    def _macd_histogram(self, closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, bool]:
        """Compute MACD histogram (fast EMA - slow EMA - signal EMA of MACD).
        Returns (macd_value, histogram_value, bullish_cross).
        Bullish cross = histogram crosses from negative to positive (buy signal).
        Used by Freqtrade, 3Commas as standard confirmation indicator."""
        try:
            if len(closes) < slow + signal + 2:
                return 0.0, 0.0, False
            ema_fast = self._ema_series(closes, fast)
            ema_slow = self._ema_series(closes, slow)
            # MACD line = fast EMA - slow EMA
            macd_line = [float(ema_fast[i]) - float(ema_slow[i]) for i in range(len(ema_fast))]
            # Signal line = EMA of MACD line
            if len(macd_line) < signal + 1:
                return 0.0, 0.0, False
            signal_line = self._ema_series(macd_line, signal)
            # Histogram = MACD - Signal
            hist_now = float(macd_line[-1]) - float(signal_line[-1])
            hist_prev = float(macd_line[-2]) - float(signal_line[-2])
            macd_val = float(macd_line[-1])
            # Bullish cross: histogram was negative, now positive (momentum shift)
            bullish_cross = hist_prev < 0 and hist_now > 0
            return macd_val, hist_now, bullish_cross
        except Exception:
            return 0.0, 0.0, False

    def _bollinger_squeeze(self, closes: list, period: int = 20, squeeze_threshold: float = 0.5) -> Tuple[bool, float, str]:
        """Detect Bollinger Band squeeze (low volatility → imminent breakout).
        Returns (squeeze_detected, bandwidth_pct, direction_hint).
        Squeeze = bandwidth < threshold% of price. Direction from price vs midline.
        Top bots (Jesse, Hummingbot) use BB squeeze as timing signal — enter during
        squeeze, profit from the subsequent breakout."""
        try:
            if len(closes) < period + 2:
                return False, 0.0, ""
            window = [float(c) for c in closes[-period:]]
            sma = sum(window) / len(window)
            if sma <= 0:
                return False, 0.0, ""
            # Standard deviation
            variance = sum((x - sma) ** 2 for x in window) / max(1, len(window) - 1)
            std_dev = variance ** 0.5
            # Bollinger Bands
            upper = sma + 2.0 * std_dev
            lower = sma - 2.0 * std_dev
            bandwidth_pct = ((upper - lower) / sma) * 100.0
            # Squeeze when bandwidth is very narrow (< threshold%)
            squeeze = bandwidth_pct < squeeze_threshold
            # Direction hint: price above SMA = bullish breakout likely
            last_price = float(closes[-1])
            direction = "UP" if last_price > sma else "DOWN"
            return squeeze, round(bandwidth_pct, 3), direction
        except Exception:
            return False, 0.0, ""

    def _rsi_divergence(self, closes: list, rsi_period: int = 14, lookback: int = 20) -> Tuple[bool, bool]:
        """Detect RSI divergence (strongest reversal signal in technical analysis).
        Returns (bullish_divergence, bearish_divergence).
        Bullish: price makes lower low but RSI makes higher low → reversal up.
        Bearish: price makes higher high but RSI makes lower high → reversal down.
        This is the #1 indicator used by professional traders and top bots."""
        try:
            if len(closes) < rsi_period + lookback + 3:
                return False, False
            # Calculate RSI for the lookback window
            rsi_values = []
            for i in range(lookback + 1):
                end_idx = len(closes) - lookback + i
                if end_idx < rsi_period + 2:
                    rsi_values.append(50.0)
                    continue
                rsi_val = self._rsi_last(closes[:end_idx], rsi_period)
                rsi_values.append(float(rsi_val) if rsi_val is not None else 50.0)
            if len(rsi_values) < 5:
                return False, False
            price_window = [float(c) for c in closes[-lookback - 1:]]
            # Find local lows and highs in price (simple: compare to neighbors)
            price_lows = []
            price_highs = []
            rsi_at_lows = []
            rsi_at_highs = []
            for i in range(1, len(price_window) - 1):
                if price_window[i] <= price_window[i-1] and price_window[i] <= price_window[i+1]:
                    price_lows.append(price_window[i])
                    ri = min(i, len(rsi_values) - 1)
                    rsi_at_lows.append(rsi_values[ri])
                if price_window[i] >= price_window[i-1] and price_window[i] >= price_window[i+1]:
                    price_highs.append(price_window[i])
                    ri = min(i, len(rsi_values) - 1)
                    rsi_at_highs.append(rsi_values[ri])
            # Bullish divergence: last price low < previous price low, but RSI low > previous RSI low
            bull_div = False
            if len(price_lows) >= 2 and len(rsi_at_lows) >= 2:
                if price_lows[-1] < price_lows[-2] and rsi_at_lows[-1] > rsi_at_lows[-2]:
                    bull_div = True
            # Bearish divergence: last price high > previous price high, but RSI high < previous RSI high
            bear_div = False
            if len(price_highs) >= 2 and len(rsi_at_highs) >= 2:
                if price_highs[-1] > price_highs[-2] and rsi_at_highs[-1] < rsi_at_highs[-2]:
                    bear_div = True
            return bull_div, bear_div
        except Exception:
            return False, False

    def _kraken_enabled(self) -> bool:
        return bool(self._kraken_live_prices_enabled or self._kraken_trading_enabled)

    def _ensure_kraken(self) -> None:
        """Ensure Kraken client is initialized if integration is enabled.
        Freqtrade pattern: retry init if previous attempt failed (network down at startup)."""
        try:
            if not self._kraken_enabled():
                return
            if self._kraken is not None:
                return
            # If init was attempted but failed (self._kraken still None), allow retry
            # after cooldown to handle network recovery scenarios
            if self._kraken_init_attempted:
                _last_try = float(getattr(self, "_kraken_init_last_ts", 0.0) or 0.0)
                if (time.time() - _last_try) < 60.0:  # retry every 60s max
                    return
                self._kraken_init_attempted = False  # allow retry
                logger.info("Retrying OKX client init (previous attempt failed)")
            self._kraken_init_last_ts = time.time()
            self._init_kraken()
        except Exception:
            return

    def _maybe_reset_ccxt_executor(self) -> None:
        """Recover from stalled ccxt calls (Windows/network stalls can leave worker threads stuck).

        If too many timeouts accumulate, recreate the ThreadPoolExecutor to free stuck threads.
        """
        try:
            failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0)
            workers = int(getattr(self, "_ccxt_pool_workers", 6) or 6)
            # Avoid aggressive resets: treat them as a last resort.
            if failures < max(12, workers * 6):
                return
        except Exception:
            return

        try:
            lock = getattr(self, "_ccxt_exec_lock", None)
            if lock is None:
                return
        except Exception:
            return

        try:
            with lock:
                failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0)
                workers = int(getattr(self, "_ccxt_pool_workers", 6) or 6)
                if failures < max(12, workers * 6):
                    return
                old = getattr(self, "_ccxt_executor", None)
                try:
                    if old is not None:
                        try:
                            old.shutdown(wait=False, cancel_futures=True)
                        except TypeError:
                            old.shutdown(wait=False)
                except Exception:
                    pass

                try:
                    self._ccxt_executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers)))
                    self._ccxt_timeout_failures = 0
                    logger.warning("Reset ccxt executor after repeated timeouts (recovered from stalled OKX calls).")
                except Exception:
                    pass
        except Exception:
            return

    def _init_kraken(self) -> None:
        """Initialize OKX client once (if enabled)."""
        if self._kraken_init_attempted:
            return
        self._kraken_init_attempted = True

        if not self._kraken_enabled():
            return
        if ccxt is None:
            logger.warning("OKX enabled but ccxt is not installed. Install it to use OKX integration.")
            return

        def _clean_env(v: Optional[str]) -> str:
            try:
                s = str(v or "")
                s = s.strip()
                # Remove common invisible/zero-width characters that can break auth when copy/pasted.
                try:
                    s = (
                        s.replace("\u200b", "")
                        .replace("\u200c", "")
                        .replace("\u200d", "")
                        .replace("\ufeff", "")
                    )
                except Exception:
                    pass
                # Defensive: some editors paste with surrounding quotes
                if (len(s) >= 2) and ((s[0] == s[-1]) and (s[0] in {"\"", "'"})):
                    s = s[1:-1].strip()
                return s
            except Exception:
                return ""

        api_key = _clean_env(os.getenv("OKX_API_KEY"))
        api_secret = _clean_env(os.getenv("OKX_API_SECRET"))
        api_passphrase = _clean_env(os.getenv("OKX_API_PASSPHRASE"))

        # OKX is region-partitioned. If your account is in EEA/US, OKX requires using the
        # region-specific domain; otherwise private calls can fail with 50119.
        # Examples:
        # - EEA: eea.okx.com
        # - US:  us.okx.com
        try:
            okx_api_domain = str(os.getenv("OKX_API_DOMAIN", "") or "").strip()
        except Exception:
            okx_api_domain = ""
        if okx_api_domain:
            okx_api_domain = okx_api_domain.replace("https://", "").replace("http://", "").strip().strip("/")
        okx_base_url = ("https://" + okx_api_domain) if okx_api_domain else ""

        # OKX keys/secrets should not contain whitespace. Remove any that might have slipped in.
        try:
            api_key = re.sub(r"\s+", "", str(api_key or ""))
        except Exception:
            pass
        try:
            api_secret = re.sub(r"\s+", "", str(api_secret or ""))
        except Exception:
            pass

        try:
            if api_key:
                logger.info(
                    "OKX credential fingerprint (apiKey_len=%s, apiKey_last4=%s, apiSecret_len=%s, passphrase_len=%s)",
                    len(api_key),
                    api_key[-4:],
                    len(api_secret or ""),
                    len(api_passphrase or ""),
                )
            else:
                logger.info("OKX credential fingerprint (apiKey_len=0)")
        except Exception:
            pass

        # OKX public endpoints (ticker/ohlcv) do not require credentials.
        if self._kraken_trading_enabled and (not api_key or not api_secret or not api_passphrase):
            logger.warning("OKX trading enabled but OKX_API_KEY/OKX_API_SECRET/OKX_API_PASSPHRASE are missing from environment.")
            return

        try:
            base_cfg = {
                "enableRateLimit": True,
                "timeout": int(os.getenv("OKX_HTTP_TIMEOUT_MS", "10000") or "10000"),
            }
            # Some OKX regional deployments require overriding the ccxt hostname (not only urls).
            # Reported fix for 50119 in third-party bots is to set hostname to app.okx.com for some regions.
            try:
                if okx_api_domain:
                    base_cfg["hostname"] = str(okx_api_domain)
            except Exception:
                pass
            trade_cfg = dict(base_cfg)
            if api_key and api_secret and api_passphrase:
                trade_cfg["apiKey"] = str(api_key)
                trade_cfg["secret"] = str(api_secret)
                trade_cfg["password"] = str(api_passphrase)

            # Start with trade config (if present). If auth fails, we will fall back to public-only.
            self._kraken = ccxt.okx(trade_cfg)

            # Apply region-specific API domain if configured.
            try:
                if okx_base_url:
                    urls = getattr(self._kraken, "urls", None)
                    if isinstance(urls, dict):
                        api_urls = urls.get("api")
                        if isinstance(api_urls, dict):
                            api_urls = dict(api_urls)
                            api_urls["public"] = str(okx_base_url)
                            api_urls["private"] = str(okx_base_url)
                            urls["api"] = api_urls
                        else:
                            urls["api"] = {"public": str(okx_base_url), "private": str(okx_base_url)}
                        self._kraken.urls = urls
                    logger.info("OKX API domain override active: %s", okx_api_domain)
            except Exception:
                pass

            try:
                self._kraken.load_markets()
            except Exception as e:
                logger.warning("OKX load_markets failed: %s", e)

            # Clear caches after market load
            try:
                self._kraken_ticker_cache.clear()
                self._kraken_balance_cache = None
                self._kraken_balance_cache_ts = 0.0
            except Exception:
                pass

            # Build dynamic universe once markets are known
            try:
                self._kraken_refresh_universe()
            except Exception:
                pass

            try:
                self._apply_kraken_only_universe()
            except Exception:
                pass
            logger.info(
                "OKX client initialized (live_prices=%s, trading=%s)",
                self._kraken_live_prices_enabled,
                self._kraken_trading_enabled,
            )

            # If trading is enabled, validate API auth once to prevent repeated private-call failures.
            try:
                if bool(self._kraken_trading_enabled):
                    ok_auth = True
                    err_s = ""
                    try:
                        timeout_s = float(getattr(self, "_kraken_call_timeout_sec", 6.0) or 6.0)
                    except Exception:
                        timeout_s = 6.0
                    try:
                        _auth_box = [None]
                        def _do_auth_check():
                            try:
                                _auth_box[0] = self._kraken.fetch_balance()
                            except Exception as _e:
                                _auth_box[0] = _e
                        _auth_t = threading.Thread(target=_do_auth_check, daemon=True)
                        _auth_t.start()
                        _auth_t.join(timeout=max(1.0, min(float(timeout_s), 8.0)))
                        if _auth_t.is_alive():
                            raise Exception("auth_balance_timeout")
                        if isinstance(_auth_box[0], Exception):
                            raise _auth_box[0]
                    except Exception as e:
                        err_s = str(e)
                        ok_auth = False
                    if not ok_auth:
                        err_l = str(err_s or "").lower()
                        if (
                            ("50119" in err_l)
                            or ("50111" in err_l)
                            or ("api key doesn't exist" in err_l)
                            or ("invalid ok-access-key" in err_l)
                            or ("invalid ok-access" in err_l)
                            or ("api key" in err_l and "exist" in err_l)
                        ):
                            self._kraken_trading_blocked_reason = "auth_failed"
                            self._kraken_trading_enabled = False
                            logger.error("OKX auth failed (API key invalid). Live trading disabled for this session.")
                            # Re-init as public-only so public endpoints (ticker/ohlcv) do not fail due to bad auth headers.
                            try:
                                self._kraken = ccxt.okx(base_cfg)
                                try:
                                    self._kraken.load_markets()
                                except Exception as e:
                                    logger.warning("OKX load_markets failed (public-only): %s", e)
                                try:
                                    self._kraken_refresh_universe()
                                except Exception:
                                    pass
                            except Exception:
                                pass
            except Exception:
                pass

            if self._kraken_live_prices_enabled:
                try:
                    try:
                        q = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
                    except Exception:
                        q = "EUR"
                    default_sym = f"BTC{q}" if q else "BTCEUR"
                    test_symbol = os.getenv("OKX_TEST_SYMBOL", default_sym)
                    test_pair = self._kraken_supported_pair(test_symbol)
                    if test_pair:
                        ticker = self._kraken.fetch_ticker(test_pair)
                        last = ticker.get("last")
                        if last is not None:
                            logger.info("OKX ticker OK (%s last=%s)", test_pair, last)
                        else:
                            logger.warning("OKX ticker returned no 'last' for %s", test_pair)
                    else:
                        logger.warning("OKX test symbol not supported for mapping: %s", test_symbol)
                except Exception as e:
                    logger.warning(f"OKX ticker self-check failed: {e}")

            # ── STARTUP RECOVERY: sell orphaned tokens from crashed/stopped sessions ──
            # Run in background thread to avoid blocking init and first trade cycle.
            if self._kraken_trading_enabled and self._kraken is not None:
                try:
                    _rec_t = threading.Thread(target=self._startup_sell_orphaned_tokens, daemon=True, name="startup_recovery")
                    _rec_t.start()
                except Exception as e_rec:
                    logger.warning("Startup recovery thread failed: %s", e_rec)

        except Exception as e:
            logger.warning(f"Failed to init OKX client: {e}")
            self._kraken = None

    def _startup_sell_orphaned_tokens(self) -> None:
        """Sell any non-quote tokens left from previous sessions and cancel orphaned trigger orders."""
        try:
            quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
            skip_ccys = {quote, "USDC", "USDT", "USD"}
            markets = getattr(self._kraken, "markets", {}) or {}

            # 1. Cancel all orphaned trigger orders on /QUOTE pairs
            cancelled = 0
            for pair in list(markets.keys()):
                if f"/{quote}" not in pair:
                    continue
                try:
                    orders = self._kraken.fetch_open_orders(pair, None, None, {"stop": True})
                    for o in (orders or []):
                        oid = o.get("id")
                        if oid:
                            try:
                                self._kraken.cancel_order(oid, pair, {"stop": True})
                                cancelled += 1
                                time.sleep(0.5)  # avoid rate limit between cancels
                            except Exception:
                                pass
                except Exception:
                    pass
            if cancelled > 0:
                print(f"🧹 Startup: cancelled {cancelled} orphaned trigger order(s)", flush=True)
                time.sleep(3)  # cooldown after cancels before selling

            # 2. Sell any non-quote token balances
            bal = self._kraken.fetch_balance({"type": "trading"})
            free = bal.get("free", {}) or {}
            sold = 0
            total_recv = 0.0
            for ccy, raw_amt in free.items():
                if ccy in skip_ccys or not raw_amt or float(raw_amt) <= 0:
                    continue
                pair = f"{ccy}/{quote}"
                if pair not in markets:
                    continue
                amt = float(raw_amt)
                try:
                    amt = float(self._kraken.amount_to_precision(pair, amt))
                except Exception:
                    pass
                if amt <= 0:
                    continue
                mkt = markets.get(pair, {})
                min_amt = float((mkt.get("limits", {}).get("amount", {}).get("min")) or 0)
                if min_amt > 0 and amt < min_amt:
                    continue
                # Retry with increasing delay to handle OKX rate limit (50011)
                _sell_ok = False
                for _retry in range(3):
                    try:
                        if _retry > 0:
                            _wait = [3, 5, 8][min(_retry, 2)]
                            logger.info("Startup recovery: retry %d for %s (wait %ds)", _retry + 1, ccy, _wait)
                            time.sleep(_wait)
                        ticker = self._kraken.fetch_ticker(pair)
                        px = float(ticker.get("last", 0) or 0)
                        if px <= 0:
                            break
                        notional = amt * px
                        min_cost = float((mkt.get("limits", {}).get("cost", {}).get("min")) or 0)
                        if min_cost > 0 and notional < min_cost:
                            break
                        order = self._kraken.create_order(pair, "market", "sell", amt)
                        oid = order.get("id", "?")
                        time.sleep(1)
                        try:
                            st = self._kraken.fetch_order(oid, pair)
                            cost = float(st.get("cost", 0) or 0)
                            total_recv += cost
                        except Exception:
                            pass
                        sold += 1
                        _sell_ok = True
                        print(f"🧹 Startup: sold {amt:.6f} {ccy} → {quote} (order={oid})", flush=True)
                        break
                    except Exception as e:
                        logger.warning("Startup recovery: failed to sell %s (attempt %d): %s", ccy, _retry + 1, e)
                time.sleep(2)  # breathing room between tokens

            if sold > 0:
                print(f"🧹 Startup recovery: sold {sold} orphaned token(s), ~€{total_recv:.2f} recovered", flush=True)
                # Invalidate balance cache AND session balance so it re-captures
                # with correct post-recovery balance (not pre-recovery which is too low)
                self._kraken_balance_cache = None
                self._kraken_balance_cache_ts = 0.0
                self._session_start_balance = 0.0
                self._session_peak_balance = 0.0
            elif cancelled == 0:
                logger.info("Startup recovery: no orphaned tokens or triggers found")
        except Exception as e:
            logger.warning("Startup recovery error: %s", e)

    def _kraken_fetch_last_price(self, symbol: str) -> Optional[float]:
        self._ensure_kraken()
        if not self._kraken:
            return None

        pair = self._kraken_supported_pair(symbol)
        if not pair:
            return None

        try:
            if self._kraken is not None and getattr(self._kraken, "markets", None):
                if pair not in getattr(self._kraken, "markets", {}):
                    return None
        except Exception:
            pass

        try:
            now = time.time()
            cached = self._kraken_ticker_cache.get(pair)
            if cached is not None:
                ts, last_val = cached
                if (now - float(ts)) <= float(self._kraken_ticker_cache_ttl_sec):
                    return float(last_val)

            try:
                timeout_s = float(getattr(self, "_kraken_call_timeout_sec", 12.0) or 12.0)
            except Exception:
                timeout_s = 12.0

            # Use dedicated thread instead of _ccxt_executor to avoid indefinite blocking
            # when all executor threads are stuck on stale OKX API calls
            _ticker_box = [None]
            def _do_fetch_ticker():
                try:
                    _ticker_box[0] = self._kraken.fetch_ticker(pair)
                except Exception:
                    pass
            _t = threading.Thread(target=_do_fetch_ticker, daemon=True)
            _t.start()
            _t.join(timeout=max(1.0, min(timeout_s, 8.0)))
            ticker = _ticker_box[0]
            if _t.is_alive() or ticker is None:
                if _t.is_alive():
                    self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                return None
            last = ticker.get("last")
            if last is None:
                return None
            last_f = float(last)
            self._kraken_ticker_cache[pair] = (now, last_f)
            return last_f
        except Exception as e:
            logger.debug(f"Kraken fetch_ticker failed for {pair}: {e}")
            return None

    def _kraken_round_amount(self, pair: str, amount: float) -> float:
        try:
            if self._kraken is None:
                return float(amount)
            # Prefer ccxt precision helpers (handles exchange-specific rules)
            try:
                s = self._kraken.amount_to_precision(pair, float(amount))
                s_f = float(s)
                # Guard: some market configs could round very small amounts to 0.
                # If that happens, fall back to raw amount and let min-size checks decide.
                if s_f <= 0 and float(amount) > 0:
                    raise ValueError("amount_to_precision_rounded_to_zero")
                return s_f
            except Exception:
                pass

            m = getattr(self._kraken, "markets", {}).get(pair) or {}
            precision = (m.get("precision") or {}).get("amount")
            if precision is None:
                return float(amount)
            # Some markets report precision=0 which would floor everything to 0 if we quantize.
            if int(precision) <= 0:
                return float(amount)
            q = Decimal("1").scaleb(-int(precision))
            return float(Decimal(str(amount)).quantize(q, rounding=ROUND_DOWN))
        except Exception:
            return float(amount)

    def _kraken_round_price(self, pair: str, price: float) -> float:
        try:
            if self._kraken is None:
                return float(price)
            try:
                s = self._kraken.price_to_precision(pair, float(price))
                return float(s)
            except Exception:
                return float(price)
        except Exception:
            return float(price)

    def _kraken_min_amount(self, pair: str) -> float:
        try:
            if self._kraken is None:
                return 0.0
            m = getattr(self._kraken, "markets", {}).get(pair) or {}
            limits = (m.get("limits") or {}).get("amount") or {}
            mn = limits.get("min")
            return float(mn) if mn is not None else 0.0
        except Exception:
            return 0.0

    def _kraken_free_balance(self, currency: str) -> float:
        try:
            if self._kraken is None:
                return 0.0

            now = time.time()
            bal = None
            if self._kraken_balance_cache is not None and (now - float(self._kraken_balance_cache_ts)) <= float(self._kraken_balance_cache_ttl_sec):
                bal = self._kraken_balance_cache
            else:
                try:
                    timeout_s = float(getattr(self, "_kraken_call_timeout_sec", 6.0) or 6.0)
                except Exception:
                    timeout_s = 6.0

                try:
                    cur_u = str(currency or "").strip().upper()
                    def _fetch_okx_balance() -> object:
                        try:
                            ex_id = str(getattr(self._kraken, "id", "") or "").lower()
                        except Exception:
                            ex_id = ""
                        if ex_id == "okx":
                            for bal_type in ("trading", "spot", "funding"):
                                try:
                                    b = self._kraken.fetch_balance({"type": bal_type})
                                    if isinstance(b, dict):
                                        fm = b.get("free")
                                        if isinstance(fm, dict) and cur_u:
                                            v = fm.get(cur_u)
                                            if v is not None and float(v) > 0:
                                                return b
                                except Exception:
                                    continue
                            # Fallback: return last attempt even if currency not found
                            try:
                                return self._kraken.fetch_balance({"type": "trading"})
                            except Exception:
                                pass
                        return self._kraken.fetch_balance()

                    _bal_box = [None]
                    def _do_fetch_bal():
                        try:
                            _bal_box[0] = _fetch_okx_balance()
                        except Exception as _e:
                            _bal_box[0] = _e
                    _bal_t = threading.Thread(target=_do_fetch_bal, daemon=True)
                    _bal_t.start()
                    _bal_t.join(timeout=max(1.0, min(timeout_s, 10.0)))
                    if _bal_t.is_alive():
                        try:
                            self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                        except Exception:
                            pass
                        self._maybe_reset_ccxt_executor()
                        return 0.0
                    if isinstance(_bal_box[0], Exception):
                        raise _bal_box[0]
                    bal = _bal_box[0]
                except Exception:
                    return 0.0
                self._kraken_balance_cache = bal if isinstance(bal, dict) else None
                self._kraken_balance_cache_ts = now

            free = None
            if isinstance(bal, dict):
                free_map = bal.get("free")
                if isinstance(free_map, dict):
                    free = free_map.get(currency)
                    if free is None:
                        try:
                            free = free_map.get(str(currency or "").strip().upper())
                        except Exception:
                            free = None
                if free is None:
                    # Some ccxt variants store currency keys at top-level
                    cur = bal.get(currency)
                    if isinstance(cur, dict):
                        free = cur.get("free")
                    if free is None:
                        try:
                            cur2 = bal.get(str(currency or "").strip().upper())
                            if isinstance(cur2, dict):
                                free = cur2.get("free")
                        except Exception:
                            pass
            return float(free) if free is not None else 0.0
        except Exception:
            return 0.0

    def _kraken_check_spot_balance(self, pair: str, side: str, amount: float, price: float) -> Tuple[bool, str]:
        try:
            if not pair or "/" not in pair:
                return False, "bad_pair"
            base, quote = pair.split("/", 1)
            side_u = str(side).upper()

            if side_u == "SELL":
                free_base = self._kraken_free_balance(base)
                if free_base + 1e-12 < float(amount):
                    return False, f"insufficient_{base}:{free_base}<{amount}"
                return True, "ok"

            if side_u == "BUY":
                free_quote = self._kraken_free_balance(quote)
                fee_buf = 1.0 + (abs(float(self._kraken_fee_buffer_pct)) / 100.0)
                need = float(amount) * float(price) * fee_buf
                if free_quote + 1e-9 < need:
                    return False, f"insufficient_{quote}:{free_quote}<{need}"
                return True, "ok"

            return False, "unknown_side"
        except Exception:
            return False, "balance_check_failed"

    def _kraken_place_order_limit_timeout(self, symbol: str, side: str, usd_size: float) -> Dict[str, object]:
        out: Dict[str, object] = {"ok": False}
        if self._kraken is None:
            out["error"] = "okx_not_initialized"
            return out

        pair = self._kraken_supported_pair(symbol)
        if not pair:
            out["error"] = "unsupported_pair"
            return out

        try:
            if getattr(self._kraken, "markets", None) and pair not in getattr(self._kraken, "markets", {}):
                out["error"] = "unsupported_pair_market"
                return out
        except Exception:
            pass

        print(f"   📋 Order: {pair} {side} €{usd_size:.2f} — fetching ticker...", flush=True)
        t = self._kraken_fetch_ticker_safe(pair)
        if not isinstance(t, dict):
            out["error"] = "fetch_ticker_failed"
            print(f"   ❌ Ticker fetch failed for {pair}", flush=True)
            return out
        try:
            bid = float(t.get("bid") or 0.0)
            ask = float(t.get("ask") or 0.0)
            last = float(t.get("last") or 0.0)
        except Exception:
            bid, ask, last = 0.0, 0.0, 0.0

        px_ref = ask if side.upper() == "BUY" else bid
        if px_ref <= 0:
            px_ref = last
        if px_ref <= 0:
            out["error"] = "no_price"
            return out

        # Spread guard: reject if bid-ask spread is abnormally wide (likely illiquid)
        try:
            max_spread_pct = float(getattr(self, "_kraken_max_spread_pct", 1.0) or 1.0)
            if bid > 0 and ask > 0:
                spread_pct = ((ask - bid) / bid) * 100.0
                out["spread_pct"] = round(spread_pct, 4)
                if spread_pct > max_spread_pct:
                    out["error"] = f"spread_too_wide:{spread_pct:.3f}%>{max_spread_pct:.1f}%"
                    return out
        except Exception:
            pass

        offset = abs(float(self._kraken_limit_offset_pct)) / 100.0
        if side.upper() == "BUY":
            limit_px = px_ref * (1.0 + offset)
        else:
            limit_px = px_ref * (1.0 - offset)

        # Round limit price to exchange precision (OKX spot)
        try:
            limit_px = float(self._kraken_round_price(pair, float(limit_px)))
        except Exception:
            limit_px = float(limit_px)
        if float(limit_px) <= 0:
            out["error"] = "bad_limit_price"
            return out

        # Recompute amount using rounded price
        amount = float(usd_size) / float(limit_px)
        amount = self._kraken_round_amount(pair, amount)
        if float(amount) <= 0:
            out["error"] = "amount_rounded_to_zero"
            return out
        min_amt = self._kraken_min_amount(pair)
        if min_amt > 0 and amount < min_amt:
            out["error"] = f"amount_below_min:{amount}<{min_amt}"
            return out

        # Enforce minimum notional/cost if exchange provides it
        try:
            m = getattr(self._kraken, "markets", {}).get(pair) or {}
            cost_min = (((m.get("limits") or {}).get("cost") or {}).get("min"))
            if cost_min is not None:
                try:
                    cost_min_f = float(cost_min)
                except Exception:
                    cost_min_f = 0.0
                if cost_min_f > 0:
                    notional = float(amount) * float(limit_px)
                    if notional + 1e-12 < cost_min_f:
                        out["error"] = f"cost_below_min:{notional}<{cost_min_f}"
                        return out
        except Exception:
            pass

        ok_bal, bal_msg = self._kraken_check_spot_balance(pair, side, float(amount), float(limit_px))
        if not ok_bal:
            out["error"] = f"balance:{bal_msg}"
            print(f"   ❌ Balance check failed: {bal_msg}", flush=True)
            return out

        print(f"   📤 Placing {side} limit order: {amount} @ {limit_px:.8f} (spread={out.get('spread_pct', '?')}%)", flush=True)
        # Retry order placement on transient network errors (Freqtrade retries up to 3x)
        # CRITICAL: use executor with timeout to prevent indefinite blocking on OKX API
        try:
            _order_timeout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
        except Exception:
            _order_timeout = 15.0
        order = None
        _last_err = None
        for _attempt in range(3):
            try:
                print(f"   📡 Order attempt {_attempt+1}/3...", flush=True)
                _order_box = [None]
                _order_err = [None]
                def _do_create_order():
                    try:
                        _order_box[0] = self._kraken.create_order(pair, "limit", side.lower(), float(amount), float(limit_px))
                    except Exception as _e:
                        _order_err[0] = _e
                _ot = threading.Thread(target=_do_create_order, daemon=True)
                _ot.start()
                _ot.join(timeout=max(3.0, min(_order_timeout, 12.0)))
                if _ot.is_alive():
                    _last_err = "create_order_timeout"
                    print(f"   ⏰ Order attempt {_attempt+1}/3 timed out ({_order_timeout:.0f}s)", flush=True)
                    logger.warning("Order attempt %d/3 timed out (%.0fs)", _attempt + 1, _order_timeout)
                    self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                    time.sleep(1.0 * (_attempt + 1))
                    continue
                if _order_err[0] is not None:
                    _e = _order_err[0]
                    _ename = type(_e).__name__
                    if "NetworkError" in _ename or "RequestTimeout" in _ename:
                        _last_err = _e
                        print(f"   ⚠️ Order attempt {_attempt+1}/3 network error: {_e}", flush=True)
                        logger.warning("Order attempt %d/3 failed (network): %s", _attempt + 1, _e)
                        time.sleep(1.0 * (_attempt + 1))
                        continue
                    out["error"] = f"create_order_failed:{_e}"
                    print(f"   ❌ Order failed: {_e}", flush=True)
                    return out
                order = _order_box[0]
                break
            except Exception as e:
                out["error"] = f"create_order_failed:{e}"
                print(f"   ❌ Order exception: {e}", flush=True)
                return out
        if order is None:
            out["error"] = f"create_order_failed_3x:{_last_err}"
            return out

        order_id = order.get("id") if isinstance(order, dict) else None
        print(f"   ✅ Order placed: id={order_id}", flush=True)
        try:
            q = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
        except Exception:
            q = "EUR"
        out.update({"order_id": order_id, "pair": pair, "type": "limit", "side": side.lower(), "amount": float(amount), "price": float(limit_px), "quote_ccy": str(q)})

        timeout = max(1, int(self._kraken_limit_timeout_sec))
        t0 = time.time()
        last_status = None
        filled = None
        print(f"   ⏳ Waiting up to {timeout}s for fill...", flush=True)
        while time.time() - t0 < timeout:
            try:
                st = self._kraken_fetch_order_safe(str(order_id or ""), pair) if order_id else None
                if isinstance(st, dict):
                    last_status = st.get("status")
                    filled = st.get("filled")
                    if str(last_status).lower() in {"closed", "filled"}:
                        out.update({"ok": True, "status": str(last_status), "filled": filled,
                                    "average": st.get("average"), "cost": st.get("cost")})
                        return out
            except Exception:
                pass
            time.sleep(1)

        print(f"   ⏰ Timeout ({timeout}s). Last status: {last_status}, filled: {filled}. Cancelling...", flush=True)
        try:
            if order_id:
                _cancel_ok = [False]
                def _do_cancel_limit():
                    try:
                        self._kraken.cancel_order(order_id, pair)
                        _cancel_ok[0] = True
                    except Exception:
                        pass
                _ct = threading.Thread(target=_do_cancel_limit, daemon=True)
                _ct.start()
                _ct.join(timeout=max(3.0, min(_order_timeout, 8.0)))
                if _cancel_ok[0]:
                    print(f"   🔄 Order cancelled.", flush=True)
                elif _ct.is_alive():
                    logger.warning("cancel_order timed out for %s", order_id)
        except Exception:
            pass

        # If limit order was partially filled before timeout, treat partial as success
        _partial_filled = False
        try:
            if filled is not None and float(filled) > 0:
                _partial_filled = True
                # Re-fetch final state after cancel to get accurate filled/average
                try:
                    st_final = self._kraken_fetch_order_safe(str(order_id or ""), pair) if order_id else None
                    if isinstance(st_final, dict):
                        filled = st_final.get("filled", filled)
                        out.update({"ok": True, "status": "partial_filled_cancelled",
                                    "filled": filled, "amount": float(filled),
                                    "average": st_final.get("average"), "cost": st_final.get("cost")})
                        logger.info("Partial fill accepted: %s %s filled=%s", pair, side, filled)
                        return out
                except Exception:
                    pass
        except Exception:
            pass

        out.update({"ok": False, "status": "timeout_cancelled", "filled": filled})

        if self._kraken_fallback_market:
            try:
                _mkt_box = [None]
                def _do_market_fallback():
                    try:
                        _mkt_box[0] = self._kraken.create_order(pair, "market", side.lower(), float(amount))
                    except Exception as _e:
                        _mkt_box[0] = _e
                _mkt_t = threading.Thread(target=_do_market_fallback, daemon=True)
                _mkt_t.start()
                _mkt_t.join(timeout=max(3.0, min(_order_timeout, 10.0)))
                if _mkt_t.is_alive() or isinstance(_mkt_box[0], Exception):
                    raise Exception(_mkt_box[0] if isinstance(_mkt_box[0], Exception) else "market_fallback_timeout")
                mkt = _mkt_box[0]
                mkt_id = mkt.get("id") if isinstance(mkt, dict) else None
                # Propagate market order result so execute_trade can track the position
                out.update({"ok": True, "fallback": "market", "status": "market_fallback_filled",
                            "fallback_order_id": mkt_id, "order_id": mkt_id})
                # Fetch filled/average from market order for accurate tracking
                try:
                    if mkt_id:
                        mkt_st = self._kraken_fetch_order_safe(str(mkt_id), pair)
                        if isinstance(mkt_st, dict):
                            out["filled"] = mkt_st.get("filled")
                            out["average"] = mkt_st.get("average")
                            out["cost"] = mkt_st.get("cost")
                            out["amount"] = float(mkt_st.get("filled") or amount)
                except Exception:
                    pass
            except Exception as e:
                out.update({"fallback": "market_failed", "fallback_error": str(e)})

        return out

    # ── Patient Orders System ──────────────────────────────────────────
    # Modern bot standard: place limit orders at good prices and wait hours/days for fill.
    # No more 20s timeout + market fallback = no more slippage.

    def _patient_calc_limit_price(self, pair: str, side: str) -> float:
        """Calculate smart limit price using bid/ask from ticker.
        BUY: place at mid-price or bid+small offset (below ask = save spread)
        SELL: place at mid-price or ask-small offset (above bid = save spread)
        """
        try:
            t = self._kraken_fetch_ticker_safe(pair)
            if not isinstance(t, dict):
                return 0.0
            bid = float(t.get("bid") or 0.0)
            ask = float(t.get("ask") or 0.0)
            if bid <= 0 or ask <= 0:
                return float(t.get("last") or 0.0)

            mode = str(getattr(self, "_patient_price_mode", "mid") or "mid").strip().lower()
            if mode == "bid":
                # At bid (most patient — may take longer but best price)
                return bid if side.upper() == "BUY" else ask
            elif mode == "ask":
                # At ask/bid (least patient — fills faster, similar to current behavior)
                return ask if side.upper() == "BUY" else bid
            else:
                # Mid-price (balanced — good fill rate + saves half the spread)
                mid = (bid + ask) / 2.0
                return mid
        except Exception:
            return 0.0

    def _patient_place_order(self, symbol: str, side: str, usd_size: float,
                              signal: 'QuantumTradingSignal') -> Dict[str, object]:
        """Place a patient limit order at a good price. Returns immediately — no blocking wait.
        The order stays open on exchange. Bot monitors it each cycle via _patient_check_pending.
        """
        out: Dict[str, object] = {"ok": False}
        if self._kraken is None:
            out["error"] = "okx_not_initialized"
            return out

        pair = self._kraken_supported_pair(symbol)
        if not pair:
            out["error"] = "unsupported_pair"
            return out

        # Fetch ticker for spread guard + price calc
        t = self._kraken_fetch_ticker_safe(pair)
        if not isinstance(t, dict):
            out["error"] = "fetch_ticker_failed"
            return out
        bid = float(t.get("bid") or 0.0)
        ask = float(t.get("ask") or 0.0)

        # Spread guard
        try:
            max_spread_pct = float(getattr(self, "_kraken_max_spread_pct", 1.0) or 1.0)
            if bid > 0 and ask > 0:
                spread_pct = ((ask - bid) / bid) * 100.0
                out["spread_pct"] = round(spread_pct, 4)
                if spread_pct > max_spread_pct:
                    out["error"] = f"spread_too_wide:{spread_pct:.3f}%>{max_spread_pct:.1f}%"
                    return out
        except Exception:
            pass

        # Smart limit price (mid-price saves half the spread)
        limit_px = self._patient_calc_limit_price(pair, side)
        if limit_px <= 0:
            out["error"] = "no_price"
            return out

        # Round price
        try:
            limit_px = float(self._kraken_round_price(pair, float(limit_px)))
        except Exception:
            pass
        if limit_px <= 0:
            out["error"] = "bad_limit_price"
            return out

        # Calculate amount
        amount = float(usd_size) / float(limit_px)
        amount = self._kraken_round_amount(pair, amount)
        if float(amount) <= 0:
            out["error"] = "amount_rounded_to_zero"
            return out
        min_amt = self._kraken_min_amount(pair)
        if min_amt > 0 and amount < min_amt:
            out["error"] = f"amount_below_min:{amount}<{min_amt}"
            return out

        # Min notional check
        try:
            m = getattr(self._kraken, "markets", {}).get(pair) or {}
            cost_min = (((m.get("limits") or {}).get("cost") or {}).get("min"))
            if cost_min is not None:
                cost_min_f = float(cost_min)
                if cost_min_f > 0 and (float(amount) * float(limit_px)) < cost_min_f:
                    out["error"] = f"cost_below_min"
                    return out
        except Exception:
            pass

        # Balance check
        ok_bal, bal_msg = self._kraken_check_spot_balance(pair, side, float(amount), float(limit_px))
        if not ok_bal:
            out["error"] = f"balance:{bal_msg}"
            return out

        # Place limit order (no IOC/FOK — standard GTC behavior on OKX)
        print(f"   📋 PATIENT ORDER: {side} {amount} {pair} @ {limit_px:.8f} (mid-price, waiting for fill...)", flush=True)
        try:
            _order_timeout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
        except Exception:
            _order_timeout = 15.0
        order = None
        _last_err = None
        for _attempt in range(3):
            try:
                _order_box = [None]
                _order_err = [None]
                def _do_patient_order():
                    try:
                        _order_box[0] = self._kraken.create_order(pair, "limit", side.lower(), float(amount), float(limit_px))
                    except Exception as _e:
                        _order_err[0] = _e
                _ot = threading.Thread(target=_do_patient_order, daemon=True)
                _ot.start()
                _ot.join(timeout=max(3.0, min(_order_timeout, 12.0)))
                if _ot.is_alive():
                    _last_err = "timeout"
                    time.sleep(1.0 * (_attempt + 1))
                    continue
                if _order_err[0] is not None:
                    _e = _order_err[0]
                    _ename = type(_e).__name__
                    if "NetworkError" in _ename or "RequestTimeout" in _ename:
                        _last_err = _e
                        time.sleep(1.0 * (_attempt + 1))
                        continue
                    out["error"] = f"create_order_failed:{_e}"
                    return out
                order = _order_box[0]
                break
            except Exception as e:
                out["error"] = f"create_order_failed:{e}"
                return out
        if order is None:
            out["error"] = f"create_order_failed_3x:{_last_err}"
            return out

        order_id = order.get("id") if isinstance(order, dict) else None
        print(f"   ✅ Patient order placed: id={order_id} — will monitor each cycle", flush=True)

        # Quick fill check (maybe it filled instantly at mid-price)
        _filled_instantly = False
        try:
            time.sleep(0.5)
            st = self._kraken_fetch_order_safe(str(order_id or ""), pair) if order_id else None
            if isinstance(st, dict) and str(st.get("status", "")).lower() in {"closed", "filled"}:
                _filled_instantly = True
                out.update({"ok": True, "status": "filled", "order_id": order_id,
                            "filled": st.get("filled"), "average": st.get("average"),
                            "cost": st.get("cost"), "amount": float(amount),
                            "price": float(limit_px), "pair": pair, "side": side.lower(),
                            "patient": True, "patient_wait_sec": 0})
                print(f"   ⚡ INSTANT FILL at mid-price! Saved spread vs market order", flush=True)
                return out
        except Exception:
            pass

        # Not filled yet — register as pending order for monitoring
        try:
            q = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
        except Exception:
            q = "EUR"
        self._pending_orders[symbol] = {
            "order_id": order_id,
            "pair": pair,
            "side": side.lower(),
            "amount": float(amount),
            "price": float(limit_px),
            "signal": signal,
            "placed_ts": time.time(),
            "amend_count": 0,
            "usd_size": float(usd_size),
            "quote_ccy": q,
        }
        out.update({"ok": True, "status": "pending_patient", "order_id": order_id,
                    "amount": float(amount), "price": float(limit_px),
                    "pair": pair, "side": side.lower(), "patient": True,
                    "patient_pending": True})
        return out

    def _patient_check_pending(self) -> None:
        """Monitor all pending patient orders each cycle.
        - If filled → activate trade (same as normal fill)
        - If expired (>max_age) → cancel
        - If price drifted too far → amend order at new mid-price
        """
        if not self._pending_orders:
            return

        completed = []
        for symbol, po in list(self._pending_orders.items()):
            try:
                order_id = str(po.get("order_id") or "")
                pair = str(po.get("pair") or "")
                if not order_id or not pair:
                    completed.append(symbol)
                    continue

                age_sec = time.time() - float(po.get("placed_ts", 0) or 0)
                signal = po.get("signal")

                # Check order status on exchange
                st = self._kraken_fetch_order_safe(order_id, pair)
                if not isinstance(st, dict):
                    # Can't check — skip this cycle
                    continue

                status = str(st.get("status", "")).lower()
                filled = float(st.get("filled") or 0)

                if status in {"closed", "filled"}:
                    # ORDER FILLED — activate trade
                    print(f"   ✅ PATIENT FILL: {symbol} {po['side'].upper()} filled after {int(age_sec)}s!", flush=True)
                    avg_px = float(st.get("average") or po.get("price", 0))
                    po["_filled_avg"] = avg_px
                    po["_filled_amount"] = filled if filled > 0 else float(po.get("amount", 0))
                    po["_filled"] = True
                    po["_wait_sec"] = int(age_sec)
                    completed.append(symbol)
                    # The trade activation happens in run_trading_cycle after this method returns
                    continue

                if status in {"canceled", "cancelled", "expired", "rejected"}:
                    print(f"   ❌ Patient order {symbol} was {status} on exchange", flush=True)
                    completed.append(symbol)
                    continue

                # Still open — check expiry
                if age_sec >= float(self._patient_order_max_age_sec):
                    print(f"   ⏰ Patient order {symbol} expired ({int(age_sec/3600)}h) — cancelling", flush=True)
                    try:
                        self._cancel_limit_order_safe(order_id, pair)
                    except Exception:
                        pass
                    completed.append(symbol)
                    continue

                # Check if price drifted too far from order — amend if needed
                try:
                    new_mid = self._patient_calc_limit_price(pair, str(po.get("side", "buy")).upper())
                    if new_mid > 0:
                        order_px = float(po.get("price", 0))
                        if order_px > 0:
                            drift_pct = abs(new_mid - order_px) / order_px * 100.0
                            amend_threshold = float(getattr(self, "_patient_order_amend_pct", 1.5) or 1.5)
                            if drift_pct > amend_threshold:
                                # Price moved too far — cancel old order, place new one
                                amend_count = int(po.get("amend_count", 0) or 0)
                                if amend_count >= 10:
                                    print(f"   ⛔ Patient order {symbol}: max amends reached (10) — cancelling", flush=True)
                                    try:
                                        self._cancel_limit_order_safe(order_id, pair)
                                    except Exception:
                                        pass
                                    completed.append(symbol)
                                    continue

                                print(f"   🔄 Patient order {symbol}: price drifted {drift_pct:.1f}% — amending to {new_mid:.8f}", flush=True)
                                try:
                                    self._cancel_limit_order_safe(order_id, pair)
                                except Exception:
                                    pass

                                # Place new order at new mid-price
                                new_px = float(self._kraken_round_price(pair, float(new_mid)))
                                new_amt = float(po.get("usd_size", 0)) / new_px if new_px > 0 else 0
                                new_amt = self._kraken_round_amount(pair, new_amt)
                                if new_amt > 0 and new_px > 0:
                                    _box = [None]
                                    _err = [None]
                                    def _do_amend():
                                        try:
                                            _box[0] = self._kraken.create_order(pair, "limit", str(po["side"]).lower(), float(new_amt), float(new_px))
                                        except Exception as _e:
                                            _err[0] = _e
                                    _t = threading.Thread(target=_do_amend, daemon=True)
                                    _t.start()
                                    _t.join(timeout=10.0)
                                    if _err[0] is None and isinstance(_box[0], dict):
                                        new_id = _box[0].get("id")
                                        po["order_id"] = new_id
                                        po["price"] = new_px
                                        po["amount"] = new_amt
                                        po["amend_count"] = amend_count + 1
                                        logger.info("Patient amend %s: new order %s @ %.8f (amend #%d)", symbol, new_id, new_px, amend_count + 1)
                                    else:
                                        logger.warning("Patient amend failed for %s: %s", symbol, _err[0])
                                        completed.append(symbol)
                                else:
                                    completed.append(symbol)
                                continue
                except Exception:
                    pass

                # Print status update
                _hrs = int(age_sec / 3600)
                _mins = int((age_sec % 3600) / 60)
                _amends = int(po.get("amend_count", 0) or 0)
                print(f"   ⏳ PENDING: {symbol} {po['side'].upper()} @ {float(po.get('price',0)):.8f} — {_hrs}h{_mins}m (amends: {_amends})", flush=True)

            except Exception as e:
                logger.debug("Patient check error for %s: %s", symbol, e)

        # Remove completed pending orders
        for sym in completed:
            try:
                del self._pending_orders[sym]
            except Exception:
                pass

    def _activate_patient_fill(self, signal: 'QuantumTradingSignal', order_meta: Dict[str, object]) -> bool:
        """Activate a trade after a patient order has been filled on exchange.
        Creates the same trade_data structure as execute_trade, places TP/SL triggers.
        """
        try:
            symbol = signal.symbol
            executed_price = float(order_meta.get("average") or order_meta.get("price") or signal.entry_price)
            real_amount = float(order_meta.get("filled") or order_meta.get("amount") or 0)
            if real_amount <= 0 and executed_price > 0:
                real_amount = float(order_meta.get("usd_size", 10)) / executed_price

            # Recalculate TP/SL relative to actual fill price (same as execute_trade)
            _orig_entry = float(signal.entry_price)
            if _orig_entry > 0 and executed_price > 0 and abs(executed_price - _orig_entry) / max(1e-12, _orig_entry) > 0.001:
                _price_shift = executed_price - _orig_entry
                signal.entry_price = executed_price
                signal.targets = [float(t) + _price_shift for t in signal.targets]
                signal.stop_loss = float(signal.stop_loss) + _price_shift

            # Derive ATR% for trailing stop
            _trade_atr_pct = 0.5
            try:
                if executed_price > 0 and signal.stop_loss > 0:
                    _sl_dist = abs(executed_price - float(signal.stop_loss))
                    _trade_atr_pct = (_sl_dist / executed_price) * 100.0 / 2.5
            except Exception:
                pass

            # Calculate position size percent for the record
            _usd_size = float(real_amount * executed_price) if (real_amount > 0 and executed_price > 0) else 10.0
            try:
                _bal = float(self._kraken_free_balance(str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR")))
                _pos_pct = (_usd_size / max(1.0, _bal + _usd_size)) * 100.0 if _bal > 0 else 75.0
            except Exception:
                _pos_pct = float(getattr(self, "_compound_position_pct", 75.0) or 75.0)
            now_dt = datetime.now()

            self.active_signals[symbol] = {
                'signal': signal,
                'entry_time': now_dt,
                'position_size': _pos_pct,
                'amount': real_amount,
                'usd_size': float(real_amount * executed_price) if (real_amount > 0 and executed_price > 0) else 10.0,
                'status': 'ACTIVE',
                'pnl': 0.0,
                'mfe': 0.0,
                'mae': 0.0,
                'tp_reached': [],
                'order': dict(order_meta),
                'tp_order_id': None,
                'sl_order_id': None,
                'atr_pct': _trade_atr_pct,
            }

            # NOTE: total_trades is incremented at trade CLOSE (exit handler), not here.
            # Counting at open would double-count (Freqtrade pattern: count at close).

            # Invalidate balance cache
            try:
                self._kraken_balance_cache = None
                self._kraken_balance_cache_ts = 0.0
                self._kraken_last_readiness_ts = 0.0
            except Exception:
                pass

            # Place TP/SL triggers on exchange
            try:
                is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
                if is_live and real_amount > 0:
                    _tp2_px = float(signal.targets[1]) if len(signal.targets) >= 2 else (float(signal.targets[0]) if signal.targets else 0.0)
                    _sl_px = float(signal.stop_loss)
                    if _tp2_px > 0 and _sl_px > 0:
                        trig = self._place_tp_sl_triggers(symbol, signal.action, real_amount, _tp2_px, _sl_px)
                        self.active_signals[symbol]['tp_order_id'] = trig.get("tp_order_id")
                        self.active_signals[symbol]['sl_order_id'] = trig.get("sl_order_id")
            except Exception:
                pass

            # Record in signal history
            with self._data_lock:
                self._signal_history.append({
                    "id": int(signal.signal_id),
                    "ts": int(now_dt.timestamp()),
                    "symbol": symbol,
                    "side": signal.action,
                    "entry": float(executed_price),
                    "tp": [float(x) for x in signal.targets[:4]],
                    "sl": float(signal.stop_loss),
                    "conf": float(signal.confidence),
                    "reason": str(signal.reasoning),
                    "order_mode": "live_patient_filled",
                    "order_id": str(order_meta.get("order_id", "") or ""),
                    "order_status": "filled",
                    "order_error": "",
                    "patient_wait_sec": int(order_meta.get("patient_wait_sec", 0) or 0),
                })

            _wait = int(order_meta.get("patient_wait_sec", 0) or 0)
            _hrs = _wait // 3600
            _mins = (_wait % 3600) // 60
            logger.info("Patient fill activated: %s %s @ %.8f (waited %dh%dm, amount=%.6f)",
                        symbol, signal.action, executed_price, _hrs, _mins, real_amount)
            print(f"   ✅ PATIENT TRADE ACTIVATED: {symbol} {signal.action} @ {executed_price:.8f} (waited {_hrs}h{_mins}m)", flush=True)
            return True
        except Exception as e:
            logger.error("Failed to activate patient fill for %s: %s", signal.symbol, e)
            return False

    def _cancel_limit_order_safe(self, order_id: str, pair: str) -> bool:
        """Cancel a regular limit order. Thread-safe with timeout."""
        if not order_id or not pair or self._kraken is None:
            return False
        try:
            _ok = [False]
            def _do():
                try:
                    self._kraken.cancel_order(order_id, pair)
                    _ok[0] = True
                except Exception:
                    pass
            _t = threading.Thread(target=_do, daemon=True)
            _t.start()
            _t.join(timeout=8.0)
            return _ok[0]
        except Exception:
            return False

    # ── Exchange-side TP/SL trigger orders ──────────────────────────────
    def _place_tp_sl_triggers(self, symbol: str, side: str, amount: float,
                              tp_price: float, sl_price: float) -> Dict[str, object]:
        """Place TP + SL trigger orders on OKX so they execute even if bot is offline.

        For a BUY trade: places two SELL trigger orders
          - TP trigger: sells when price >= tp_price
          - SL trigger: sells when price <= sl_price

        Returns dict with tp_order_id, sl_order_id (None if failed).
        Uses 'triggerPrice' param which works on OKX EEA spot.
        """
        result: Dict[str, object] = {"tp_order_id": None, "sl_order_id": None,
                                      "tp_error": "", "sl_error": ""}
        try:
            pair = self._kraken_supported_pair(symbol)
            if not pair or self._kraken is None:
                result["tp_error"] = result["sl_error"] = "no_pair_or_exchange"
                return result

            exit_side = "sell" if side.upper() == "BUY" else "buy"
            rounded_amt = self._kraken_round_amount(pair, float(amount))
            if rounded_amt <= 0:
                result["tp_error"] = result["sl_error"] = "zero_amount"
                return result

            try:
                _tout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
            except Exception:
                _tout = 15.0

            # ── TP trigger order ──
            try:
                if tp_price is None or float(tp_price) <= 0:
                    raise ValueError("no_tp_price")
                tp_px = self._kraken_round_price(pair, float(tp_price))
                print(f"   🎯 Placing TP trigger {exit_side} {rounded_amt} @ trigger={tp_px}", flush=True)
                _tp_box = [None]
                def _do_tp_trigger():
                    try:
                        _tp_box[0] = self._kraken.create_order(pair, "market", exit_side,
                            float(rounded_amt), None, {"triggerPrice": tp_px})
                    except Exception as _e:
                        _tp_box[0] = _e
                _tp_t = threading.Thread(target=_do_tp_trigger, daemon=True)
                _tp_t.start()
                _tp_t.join(timeout=min(8.0, max(3.0, _tout)))
                if _tp_t.is_alive():
                    result["tp_error"] = "timeout"
                    print(f"   ⚠️ TP trigger timeout", flush=True)
                elif isinstance(_tp_box[0], Exception):
                    result["tp_error"] = str(_tp_box[0])[:200]
                    print(f"   ⚠️ TP trigger failed: {_tp_box[0]}", flush=True)
                else:
                    tp_resp = _tp_box[0]
                    tp_id = tp_resp.get("id") if isinstance(tp_resp, dict) else None
                    result["tp_order_id"] = tp_id
                    print(f"   🎯 TP trigger placed: id={tp_id}", flush=True)
            except Exception as e:
                result["tp_error"] = str(e)[:200]
                print(f"   ⚠️ TP trigger failed: {e}", flush=True)

            # ── SL trigger order ──
            try:
                if sl_price is None or float(sl_price) <= 0:
                    raise ValueError("no_sl_price")
                sl_px = self._kraken_round_price(pair, float(sl_price))
                print(f"   🛡️ Placing SL trigger {exit_side} {rounded_amt} @ trigger={sl_px}", flush=True)
                _sl_box = [None]
                def _do_sl_trigger():
                    try:
                        _sl_box[0] = self._kraken.create_order(pair, "market", exit_side,
                            float(rounded_amt), None, {"triggerPrice": sl_px})
                    except Exception as _e:
                        _sl_box[0] = _e
                _sl_t = threading.Thread(target=_do_sl_trigger, daemon=True)
                _sl_t.start()
                _sl_t.join(timeout=min(8.0, max(3.0, _tout)))
                if _sl_t.is_alive():
                    result["sl_error"] = "timeout"
                    print(f"   ⚠️ SL trigger timeout", flush=True)
                elif isinstance(_sl_box[0], Exception):
                    result["sl_error"] = str(_sl_box[0])[:200]
                    print(f"   ⚠️ SL trigger failed: {_sl_box[0]}", flush=True)
                else:
                    sl_resp = _sl_box[0]
                    sl_id = sl_resp.get("id") if isinstance(sl_resp, dict) else None
                    result["sl_order_id"] = sl_id
                    print(f"   🛡️ SL trigger placed: id={sl_id}", flush=True)
            except Exception as e:
                result["sl_error"] = str(e)[:200]
                print(f"   ⚠️ SL trigger failed: {e}", flush=True)

        except Exception as e:
            result["tp_error"] = result["sl_error"] = f"outer:{e}"
        return result

    def _cancel_trigger_order(self, order_id: str, pair: str) -> bool:
        """Cancel a trigger/algo order on OKX. Returns True if cancelled.
        OKX trigger orders require {"stop": True} param — try that first.
        Uses dedicated threads to avoid blocking on stalled _ccxt_executor."""
        if not order_id or not pair or self._kraken is None:
            return False
        try:
            _tout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
        except Exception:
            _tout = 15.0
        _tout = min(_tout, 8.0)
        # Try algo cancel first (trigger orders need stop=True), then normal
        for params in [{"stop": True}, None]:
            try:
                _ok_box = [False]
                def _do_cancel():
                    try:
                        if params:
                            self._kraken.cancel_order(order_id, pair, params)
                        else:
                            self._kraken.cancel_order(order_id, pair)
                        _ok_box[0] = True
                    except Exception:
                        pass
                t = threading.Thread(target=_do_cancel, daemon=True)
                t.start()
                t.join(timeout=max(3.0, _tout))
                if _ok_box[0]:
                    return True
                if t.is_alive():
                    self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
            except Exception:
                continue
        return False

    def _fetch_trigger_order_safe(self, order_id: str, pair: str, timeout_s: float = 6.0) -> Optional[dict]:
        """Fetch status of a trigger/algo order on OKX.
        Normal fetch_order fails for trigger orders — must use {"stop": True}.
        Uses a dedicated thread to avoid blocking on stalled _ccxt_executor."""
        if not order_id or not pair or self._kraken is None:
            return None
        try:
            import threading as _thr
            _result_box = [None]
            _err_box = [None]
            def _do_fetch():
                try:
                    _result_box[0] = self._kraken.fetch_order(order_id, pair, {"stop": True})
                except Exception as _e:
                    _err_box[0] = _e
            t = _thr.Thread(target=_do_fetch, daemon=True)
            t.start()
            t.join(timeout=max(1.0, timeout_s))
            if t.is_alive():
                self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                return None
            res = _result_box[0]
            return res if isinstance(res, dict) else None
        except Exception:
            return None

    def _update_sl_trigger(self, symbol: str, trade_data: dict, new_sl_price: float) -> None:
        """Cancel old SL trigger and place a new one at a higher level (trailing stop on exchange)."""
        try:
            old_sl_id = trade_data.get("sl_order_id")
            pair = self._kraken_supported_pair(symbol)
            if not pair:
                return

            # Cancel old SL
            if old_sl_id:
                ok = self._cancel_trigger_order(str(old_sl_id), pair)
                if ok:
                    print(f"   🔄 Old SL trigger {old_sl_id} cancelled", flush=True)

            # Place new SL
            signal = trade_data.get("signal")
            if not signal:
                return
            side = str(signal.action).upper()
            amt = float(trade_data.get("amount", 0.0) or 0.0)
            if amt <= 0:
                return

            exit_side = "sell" if side == "BUY" else "buy"
            rounded_amt = self._kraken_round_amount(pair, amt)
            sl_px = self._kraken_round_price(pair, float(new_sl_price))

            try:
                _tout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
            except Exception:
                _tout = 15.0

            _resp_box = [None]
            def _do_create_sl():
                try:
                    _resp_box[0] = self._kraken.create_order(pair, "market", exit_side,
                        float(rounded_amt), None, {"triggerPrice": sl_px})
                except Exception:
                    pass
            _t = threading.Thread(target=_do_create_sl, daemon=True)
            _t.start()
            _t.join(timeout=min(8.0, max(3.0, _tout)))
            resp = _resp_box[0]
            if _t.is_alive():
                self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
            new_id = resp.get("id") if isinstance(resp, dict) else None
            trade_data["sl_order_id"] = new_id
            print(f"   🛡️ New SL trigger @ {sl_px}: id={new_id}", flush=True)
        except Exception as e:
            logger.warning("Failed to update SL trigger for %s: %s", symbol, e)

    # ── End exchange-side TP/SL ─────────────────────────────────────────

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price — live only when OKX is enabled, no fake simulation.
        Returns None if price is stale (>120s old) to prevent false SL/TP triggers."""
        try:
            if self._kraken_live_prices_enabled:
                live = self._kraken_fetch_last_price(symbol)
                if live is not None:
                    with self._data_lock:
                        self._last_prices[symbol] = float(live)
                        self._last_prices_ts[symbol] = time.time()
                    return float(live)
                # Fallback: return last known price ONLY if fresh enough
                with self._data_lock:
                    cached = self._last_prices.get(symbol)
                    cached_ts = self._last_prices_ts.get(symbol, 0.0)
                if cached is not None and cached > 0:
                    if cached_ts <= 0:
                        # Never fetched live — base_prices init value, not stale, just unknown
                        return None
                    age = time.time() - cached_ts
                    if age <= self._stale_price_max_age_sec:
                        return float(cached)
                    # Rate-limit stale warnings: once per symbol per 5min to avoid log spam
                    _stale_log = getattr(self, "_stale_price_log_ts", {})
                    _now_st = time.time()
                    if symbol not in _stale_log or (_now_st - _stale_log.get(symbol, 0)) > 300:
                        logger.warning("Stale price for %s (%.0fs old) — skipping", symbol, age)
                        _stale_log[symbol] = _now_st
                        self._stale_price_log_ts = _stale_log
                    return None
                return None

            # Non-live mode: use last known price or base price (no random simulation)
            with self._data_lock:
                cached = self._last_prices.get(symbol)
            if cached is not None and cached > 0:
                return float(cached)
            base_price = self.base_prices.get(symbol)
            if base_price is not None and base_price > 0:
                return float(base_price)
            return None
            
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    def generate_signals(self) -> List[QuantumTradingSignal]:
        """Generate trading signals - FIXED VERSION"""
        # Kraken multi-timeframe strategy (real signals) when enabled
        try:
            if bool(self._kraken_enabled() and self._kraken is not None):
                try:
                    r = self._kraken_readiness_check(force=False) if hasattr(self, "_kraken_readiness_check") else {}
                except Exception:
                    r = {}
                try:
                    if not bool((r or {}).get("ohlcv_ok") is True):
                        try:
                            with self._data_lock:
                                self._verification_audit.append({
                                    "t": int(time.time()),
                                    "type": "signal_skip",
                                    "reason": "live_ohlcv_not_ready",
                                    "quote_ccy": str((r or {}).get("quote_ccy") or ""),
                                })
                        except Exception:
                            pass
                        return []
                except Exception:
                    pass
                sigs = self._generate_signals_kraken_mtf()
                try:
                    self._signal_scan_last_raw = int(len(sigs or []))
                except Exception:
                    pass
                return sigs
        except Exception:
            pass

        # No fallback random signal generation — only real OKX MTF signals are used.
        logger.warning("OKX not active — no signals generated this cycle.")
        return []

    def _generate_signals_kraken_mtf(self) -> List[QuantumTradingSignal]:
        out: List[QuantumTradingSignal] = []
        universe = self._get_signal_universe()
        if not universe:
            return out

        # Time budget: never let this function consume the whole cycle timeout.
        # Kraken/ccxt can stall intermittently; we prefer partial coverage over stalling the system.
        started = time.time()
        try:
            cycle_timeout = float(getattr(self, "_signal_gen_timeout_sec", 45.0) or 45.0)
        except Exception:
            cycle_timeout = 45.0
        budget = max(5.0, min(90.0, cycle_timeout * 0.70))

        try:
            per_cycle = int(getattr(self, "_kraken_symbols_per_cycle", 8) or 8)
        except Exception:
            per_cycle = 8
        per_cycle = max(1, min(200, int(per_cycle)))

        start = int(getattr(self, "_kraken_universe_cursor", 0) or 0)
        n = len(universe)
        if start < 0:
            start = 0
        if n > 0 and start >= n:
            start = 0
        end = min(n, start + per_cycle)
        subset = universe[start:end]
        if end >= n:
            subset = subset + universe[0:max(0, per_cycle - len(subset))]
            end = (per_cycle - len(universe[start:end]))

        _skip_counts: Dict[str, int] = {}  # Skip reason distribution for diagnostics

        def _set_skip(reason: str):
            """Set skip reason + count for diagnostics."""
            try:
                self._signal_scan_last_skip = reason
                # Group by prefix (e.g. "LV(r=0.12)" → "LV")
                key = reason.split("(")[0] if "(" in reason else reason
                _skip_counts[key] = _skip_counts.get(key, 0) + 1
            except Exception:
                pass

        try:
            self._signal_scan_total = int(len(subset or []))
            self._signal_scan_index = 0
            self._signal_scan_symbol = ""
            self._signal_scan_last_skip = ""
            self._signal_scan_started_ts = float(time.time())
        except Exception:
            pass

        # Purge expired illiquid cache entries (Freqtrade pattern: periodic cleanup)
        # Without this, cache grows unbounded as symbols are added but never removed.
        try:
            _now = time.time()
            _ttl = float(self._illiquid_cache_ttl)
            _expired = [k for k, v in self._illiquid_cache.items() if (_now - float(v)) >= _ttl]
            for k in _expired:
                del self._illiquid_cache[k]
        except Exception:
            pass

        try:
            self._kraken_universe_cursor = int((start + per_cycle) % max(1, n))
        except Exception:
            pass

        try:
            call_timeout = float(getattr(self, "_kraken_call_timeout_sec", 6.0) or 6.0)
        except Exception:
            call_timeout = 6.0
        call_timeout = float(max(1.0, min(15.0, call_timeout)))

        for i_sym, symbol in enumerate(subset):
            try:
                if (time.time() - started) >= budget:
                    try:
                        _set_skip("BUDGET_X")
                    except Exception:
                        pass
                    break
            except Exception:
                pass
            # Brief pause between symbols to respect OKX rate limits (~20 req/2s for public endpoints)
            # 3 concurrent fetches per symbol → ~150ms gap keeps us well under 20 req/2s
            if i_sym > 0:
                try:
                    time.sleep(0.15)
                except Exception:
                    pass
            try:
                try:
                    self._signal_scan_index = int(i_sym + 1)
                    self._signal_scan_symbol = str(symbol)
                    self._signal_scan_last_skip = ""
                    # Progress indicator every 20 symbols so console doesn't look hung
                    if (i_sym + 1) % 20 == 0 or (i_sym + 1) == len(subset):
                        print(f"   Scanning... {i_sym + 1}/{len(subset)} symbols ({len(out)} signals so far)", flush=True)
                except Exception:
                    pass
                try:
                    min_c = int(getattr(self, "_kraken_mtf_min_candles", 60) or 60)
                except Exception:
                    min_c = 60
                try:
                    min_c_1h = int(getattr(self, "_kraken_mtf_min_candles_1h", 60) or 60)
                except Exception:
                    min_c_1h = 60
                # Keep these modest: some symbols (new listings) don't have long 1h history.
                min_c = int(max(30, min(300, min_c)))
                min_c_1h = int(max(30, min(300, min_c_1h)))

                limit_1m_5m = int(min_c)
                limit_1h = int(min_c_1h)
                pair = self._kraken_supported_pair(symbol)
                if not pair:
                    try:
                        _set_skip("NO_PAIR")
                    except Exception:
                        pass
                    continue

                # Liquidity cache: skip symbols known to be dead (saves 3 OHLCV calls per symbol)
                try:
                    _ilq_ts = self._illiquid_cache.get(symbol)
                    if _ilq_ts is not None and (time.time() - float(_ilq_ts)) < self._illiquid_cache_ttl:
                        try:
                            _set_skip("ILQ_CACHE")
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

                # Fetch 2 timeframes (5m entry + 1h trend). 1m skipped: USDC pairs on OKX EEA
                # have zero 1m liquidity (flat candles, vol=0). 5m has real data for top coins.
                t_submit = time.time()
                # Use dedicated threads for parallel OHLCV fetch to avoid saturating _ccxt_executor
                _ohlcv_5m_box = [None]
                _ohlcv_1h_box = [None]
                _ohlcv_err_box = [None, None]  # [5m_err, 1h_err]
                def _fetch_5m():
                    try:
                        _ohlcv_5m_box[0] = self._kraken.fetch_ohlcv(pair, "5m", None, int(limit_1m_5m))
                    except Exception as _e5:
                        _ohlcv_err_box[0] = _e5
                def _fetch_1h():
                    try:
                        _ohlcv_1h_box[0] = self._kraken.fetch_ohlcv(pair, "1h", None, int(limit_1h))
                    except Exception as _e1h:
                        _ohlcv_err_box[1] = _e1h
                _t5 = threading.Thread(target=_fetch_5m, daemon=True)
                _t1h = threading.Thread(target=_fetch_1h, daemon=True)
                _t5.start()
                _t1h.start()
                _ohlcv_join_timeout = float(call_timeout)
                _t5.join(timeout=_ohlcv_join_timeout)
                _t1h.join(timeout=max(0.1, _ohlcv_join_timeout - (time.time() - t_submit)))

                # Build results dict mimicking old futures interface
                results: Dict[str, object] = {}
                not_done_count = 0
                if _t5.is_alive():
                    not_done_count += 1
                if _t1h.is_alive():
                    not_done_count += 1

                # Only count a "timeout failure" when tasks did not complete in time.
                if not_done_count > 0:
                    try:
                        self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                    except Exception:
                        pass
                    try:
                        _set_skip("OHLCV_TO")
                    except Exception:
                        pass

                # Collect results from dedicated threads
                _rate_limited = False
                try:
                    for _oerr in _ohlcv_err_box:
                        if _oerr is not None:
                            _oerr_s = str(_oerr).lower()
                            if "429" in _oerr_s or "ratelimit" in _oerr_s or "too many" in _oerr_s:
                                _rate_limited = True
                                break
                except Exception:
                    pass
                results["5m"] = _ohlcv_5m_box[0] if not _t5.is_alive() else None
                results["1h"] = _ohlcv_1h_box[0] if not _t1h.is_alive() else None
                # Exponential backoff on rate limit (Freqtrade pattern)
                if _rate_limited:
                    _rl_wait = min(10.0, 1.0 + i_sym * 0.1)
                    logger.warning("Rate limit hit on %s — backing off %.1fs", symbol, _rl_wait)
                    try:
                        _set_skip(f"RL({_rl_wait:.1f}s)")
                    except Exception:
                        pass
                    time.sleep(_rl_wait)
                    continue

                c5 = results.get("5m")
                c1h = results.get("1h")

                try:
                    self._signal_scan_last_ohlcv_ms = {
                        "wait_ms": float(max(0.0, (time.time() - t_submit) * 1000.0)),
                        "call_timeout_sec": float(call_timeout),
                        "budget_sec": float(budget),
                        "n_5m": int(len(c5)) if isinstance(c5, list) else 0,
                        "n_1h": int(len(c1h)) if isinstance(c1h, list) else 0,
                        "min_5m": int(limit_1m_5m),
                        "min_1h": int(limit_1h),
                    }
                except Exception:
                    pass

                # Normalize OHLCV data
                try:
                    if isinstance(c5, list) and c5:
                        c5 = [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] if len(x) > 5 else 0.0)] for x in c5]
                    else:
                        c5 = None
                except Exception:
                    c5 = None
                try:
                    if isinstance(c1h, list) and c1h:
                        c1h = [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] if len(x) > 5 else 0.0)] for x in c1h]
                    else:
                        c1h = None
                except Exception:
                    c1h = None

                if not c5 or not c1h:
                    try:
                        _set_skip("OHLCV_0")
                    except Exception:
                        pass
                    continue

                # ── SMART COIN ACTIVITY FILTER ──
                # Skip stagnant coins EARLY (before any indicator calc) to save CPU
                try:
                    _coin_active, _coin_reason = self._coin_is_active(c5, c1h)
                    if not _coin_active:
                        try:
                            # Only cache structurally dead symbols (1h/5m price stale).
                            # VOL_DROUGHT is temporary on EUR/OKX EEA — 5m volume
                            # comes and goes with market activity, re-check every cycle.
                            if _coin_reason.startswith("STALE_"):
                                self._illiquid_cache[symbol] = time.time()
                            _set_skip(_coin_reason)
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

                # 5m = entry timeframe (RSI + price), 1h = trend (EMA)
                # 1m skipped: USDC on OKX EEA has zero 1m liquidity
                closes_5m = [float(x[4]) for x in c5]
                closes_1h = [float(x[4]) for x in c1h]
                # Per-timeframe minimum candle requirements:
                # 5m: need slow_ema+1 or rsi_period+3 (whichever is larger) for RSI + EMA
                # 1h: only used for trend (EMA fast vs slow) — require fast_ema+1.
                try:
                    _need_ema = int(max(self._kraken_mtf_fast_ema, self._kraken_mtf_slow_ema)) + 1
                    _need_rsi = int(self._kraken_mtf_rsi_period) + 3
                    _need_5m = int(max(_need_ema, _need_rsi))
                    _need_1h = int(self._kraken_mtf_fast_ema) + 1  # trend only, 21 candles min
                except Exception:
                    _need_5m = 52
                    _need_1h = 21
                if len(closes_5m) < _need_5m or len(closes_1h) < _need_1h:
                    try:
                        _set_skip(f"FEW_C(5m={len(closes_5m)}/{_need_5m},1h={len(closes_1h)}/{_need_1h})")
                        # Cache symbols with too few 1h candles — they won't grow mid-session
                        if len(closes_1h) < _need_1h:
                            self._illiquid_cache[symbol] = time.time()
                    except Exception:
                        pass
                    continue

                ema_fast_1h = self._ema_series(closes_1h, int(self._kraken_mtf_fast_ema))
                ema_slow_1h = self._ema_series(closes_1h, int(self._kraken_mtf_slow_ema))
                trend_up = float(ema_fast_1h[-1]) > float(ema_slow_1h[-1])
                trend_down = float(ema_fast_1h[-1]) < float(ema_slow_1h[-1])

                # EMA separation strength (Jesse pattern: stronger trend = higher confidence)
                _ema_sep_1h = abs(float(ema_fast_1h[-1]) - float(ema_slow_1h[-1]))
                _ema_sep_pct = (_ema_sep_1h / max(1e-12, float(ema_slow_1h[-1]))) * 100.0

                # EMA crossover freshness (Freqtrade pattern): trend must have crossed within
                # last 5 candles. Old crossovers (trend established hours ago) have diminishing edge.
                _ema_cross_fresh = False
                try:
                    if len(ema_fast_1h) >= 6 and len(ema_slow_1h) >= 6:
                        for _ci in range(1, 6):
                            _prev_f = float(ema_fast_1h[-1 - _ci])
                            _prev_s = float(ema_slow_1h[-1 - _ci])
                            if trend_up and _prev_f <= _prev_s:
                                _ema_cross_fresh = True
                                break
                            if trend_down and _prev_f >= _prev_s:
                                _ema_cross_fresh = True
                                break
                    else:
                        _ema_cross_fresh = True  # Not enough data to check, allow
                except Exception:
                    _ema_cross_fresh = True

                ema_fast_5m = self._ema_series(closes_5m, int(self._kraken_mtf_fast_ema))
                ema_slow_5m = self._ema_series(closes_5m, int(self._kraken_mtf_slow_ema))
                conf_up = float(ema_fast_5m[-1]) > float(ema_slow_5m[-1])
                conf_down = float(ema_fast_5m[-1]) < float(ema_slow_5m[-1])

                # Entry data: prefer 5m if it has real data, otherwise fallback to 1h.
                # USDC on OKX EEA often has flat 5m candles (all closes identical → RSI=100/0).
                # Detect flat 5m: if unique close count in last 14 candles < 3, use 1h.
                _5m_uniq = len(set(closes_5m[-14:])) if len(closes_5m) >= 14 else len(set(closes_5m))
                _5m_is_flat = _5m_uniq < 4
                if not _5m_is_flat:
                    _entry_closes = closes_5m
                    rsi_5m = self._rsi_last(closes_5m, int(self._kraken_mtf_rsi_period))
                    last_price = float(closes_5m[-1])
                else:
                    # 5m is flat — fallback to 1h for ALL entry data
                    _entry_closes = closes_1h
                    rsi_5m = self._rsi_last(closes_1h, int(self._kraken_mtf_rsi_period))
                    last_price = float(closes_1h[-1])
                    # Override 5m EMA confirmation with 1h (5m EMA meaningless on flat data)
                    conf_up = trend_up
                    conf_down = trend_down
                if rsi_5m is None or rsi_5m < 5.0 or rsi_5m > 95.0:
                    try:
                        _set_skip(f"RSI_X({rsi_5m})" if rsi_5m is not None else "RSI_X(none)")
                    except Exception:
                        pass
                    continue
                action = None
                gates = {
                    "trd_1h": bool(trend_up or trend_down),
                    "cnf_5m": bool(conf_up or conf_down),
                    "rsi_5m": float(rsi_5m),
                }

                try:
                    gates["trd_up"] = bool(trend_up)
                    gates["trd_dn"] = bool(trend_down)
                    gates["cnf_up"] = bool(conf_up)
                    gates["cnf_dn"] = bool(conf_down)
                except Exception:
                    pass

                try:
                    rsi_buy_max = float(getattr(self, "_kraken_mtf_rsi_buy_max", 42.0) or 42.0)
                except Exception:
                    rsi_buy_max = 42.0
                try:
                    rsi_sell_min = float(getattr(self, "_kraken_mtf_rsi_sell_min", 60.0) or 60.0)
                except Exception:
                    rsi_sell_min = 60.0

                try:
                    req_trend = bool(getattr(self, "_kraken_mtf_require_trend", True))
                except Exception:
                    req_trend = True
                try:
                    req_conf = bool(getattr(self, "_kraken_mtf_require_confirm", True))
                except Exception:
                    req_conf = True

                try:
                    gates["req_trd"] = bool(req_trend)
                    gates["req_cnf"] = bool(req_conf)
                    gates["rsi_buy_max"] = float(rsi_buy_max)
                    gates["rsi_sell_min"] = float(rsi_sell_min)
                except Exception:
                    pass

                # _ema_sep_pct already computed above (near EMA crossover freshness check)

                # ── SAFETY LAYER 1: Volatility regime filter ──
                # Skip if ATR% is too low (flat/dead market → fakeouts) or too high (chaos → SL wipeout)
                # USDC on OKX EEA: 5m candles are often flat (H=L=C). Fallback to 1h ATR.
                _vol_regime_ok = True
                try:
                    # Try 5m ATR first
                    _tr_vals = []
                    for _k in range(1, min(len(c5), 15)):
                        _hi = float(c5[-_k][2]); _lo = float(c5[-_k][3])
                        _pc = float(c5[-_k - 1][4]) if (_k + 1) <= len(c5) else _lo
                        _tr_vals.append(max(_hi - _lo, abs(_hi - _pc), abs(_lo - _pc)))
                    _atr14 = sum(_tr_vals) / len(_tr_vals) if _tr_vals else 0.0
                    _atr_pct = (_atr14 / max(1e-12, last_price)) * 100.0
                    # If 5m ATR is near-zero, fallback to 1h ATR (always has real data on USDC)
                    if _atr_pct < 0.005 and c1h and len(c1h) >= 5:
                        _tr_1h = []
                        for _k in range(1, min(len(c1h), 15)):
                            _hi = float(c1h[-_k][2]); _lo = float(c1h[-_k][3])
                            _pc = float(c1h[-_k - 1][4]) if (_k + 1) <= len(c1h) else _lo
                            _tr_1h.append(max(_hi - _lo, abs(_hi - _pc), abs(_lo - _pc)))
                        _atr14_1h = sum(_tr_1h) / len(_tr_1h) if _tr_1h else 0.0
                        _atr_pct = (_atr14_1h / max(1e-12, last_price)) * 100.0
                        _atr14 = _atr14_1h  # Use 1h ATR for TP/SL calculation too
                    # Too flat (<0.01%) = noise, too wild (>5%) = untradeable
                    if _atr_pct < 0.01 or _atr_pct > 5.0:
                        _vol_regime_ok = False
                    gates["atr_pct"] = round(_atr_pct, 4)
                    gates["vr_ok"] = _vol_regime_ok
                except Exception:
                    pass
                if not _vol_regime_ok:
                    try:
                        _set_skip(f"VR(atr={_atr_pct:.4f}%)")
                    except Exception:
                        _set_skip("VR")
                    continue

                # ── SAFETY LAYER 2: Volume ratio (soft gate — informational only) ──
                # EUR/OKX EEA: 5m volume is structurally low/sporadic. Many active
                # pairs have 0 volume on the latest 5m candle — this is normal, not
                # dangerous. Upstream filters (VOL_DROUGHT, STALE_1H, ZV) already
                # catch truly dead pairs. Volume ratio is recorded in gates for
                # diagnostics but does NOT block signal generation.
                _vol_ratio = 0.0
                try:
                    _vols = [float(x[5]) for x in c5[-20:] if len(x) > 5]
                    if len(_vols) >= 5:
                        _avg_vol = sum(_vols) / len(_vols)
                        if _avg_vol > 0:
                            _cur_vol = _vols[-1]
                            _vol_ratio = _cur_vol / max(1e-12, _avg_vol)
                            gates["vol_ratio"] = round(_vol_ratio, 2)
                        else:
                            gates["vol_ratio"] = -1
                        gates["v_ok"] = True  # always pass — soft gate
                except Exception:
                    pass

                # ── SAFETY LAYER 3: Market regime (trending vs ranging) ──
                # Quick check: if last 5 close prices barely move AND RSI near 50 → ranging
                _regime_ok = True
                _price_range_pct = 0.0
                try:
                    _last5 = _entry_closes[-5:]
                    if len(_last5) >= 5 and last_price > 0:
                        _price_range_pct = (max(_last5) - min(_last5)) / last_price * 100.0
                        # Dead market: zero price movement in last 5 candles → skip regardless of RSI
                        if _price_range_pct < 0.005:
                            _regime_ok = False
                        # If RSI is neutral (42-58) and price moved <0.05% in last 5 candles → flat
                        elif rsi_5m is not None and 42.0 < float(rsi_5m) < 58.0 and _price_range_pct < 0.05:
                            _regime_ok = False
                        gates["price_range_5c_pct"] = round(_price_range_pct, 3)
                        gates["r_ok"] = _regime_ok
                except Exception:
                    pass
                if not _regime_ok:
                    try:
                        _set_skip(f"RNG(rng={_price_range_pct:.3f}%)")
                    except Exception:
                        _set_skip("RNG")
                    continue

                # ── SAFETY LAYER 4: RSI momentum direction ──
                # RSI must be moving in the right direction (falling for BUY, rising for SELL)
                _rsi_momentum = 0.0
                try:
                    # Compare current RSI vs RSI from 2 candles ago (single extra calc)
                    if len(_entry_closes) > int(self._kraken_mtf_rsi_period) + 4:
                        _rsi_prev = self._rsi_last(_entry_closes[:-2], int(self._kraken_mtf_rsi_period))
                        if _rsi_prev is not None and rsi_5m is not None:
                            _rsi_momentum = float(rsi_5m) - float(_rsi_prev)
                    gates["rsi_momentum"] = round(_rsi_momentum, 2)
                except Exception:
                    pass

                # ── SAFETY LAYER 5: Smart market intelligence ──
                # Pattern detection, volume spike, price structure (top-bot patterns)
                _bull_pattern, _bull_name = False, ""
                _bear_pattern, _bear_name = False, ""
                _vol_spike, _vol_spike_ratio = False, 0.0
                _higher_lows = False
                _lower_highs = False
                try:
                    _bull_pattern, _bull_name = self._detect_bullish_pattern(c5)
                    _bear_pattern, _bear_name = self._detect_bearish_pattern(c5)
                    _vol_spike, _vol_spike_ratio = self._volume_spike_detected(c5)
                    _higher_lows = self._price_making_higher_lows(_entry_closes, 10)
                    _lower_highs = self._price_making_lower_highs(_entry_closes, 10)
                except Exception:
                    pass

                # ── SAFETY LAYER 6: Modern indicators (MACD, BB squeeze, RSI divergence) ──
                _macd_val, _macd_hist, _macd_bull_cross = 0.0, 0.0, False
                _bb_squeeze, _bb_bw, _bb_dir = False, 0.0, ""
                _rsi_bull_div, _rsi_bear_div = False, False
                try:
                    _macd_val, _macd_hist, _macd_bull_cross = self._macd_histogram(_entry_closes)
                    _bb_squeeze, _bb_bw, _bb_dir = self._bollinger_squeeze(_entry_closes)
                    _rsi_bull_div, _rsi_bear_div = self._rsi_divergence(_entry_closes, int(self._kraken_mtf_rsi_period))
                except Exception:
                    pass

                try:
                    gates["pattern"] = _bull_name or _bear_name or ""
                    gates["vol_spike"] = _vol_spike_ratio
                    gates["hl"] = _higher_lows
                    gates["macd_h"] = round(_macd_hist, 6)
                    gates["macd_x"] = _macd_bull_cross
                    gates["bb_sq"] = _bb_squeeze
                    gates["bb_bw"] = _bb_bw
                    gates["rsi_div"] = _rsi_bull_div
                except Exception:
                    pass

                def _ok_buy() -> bool:
                    # ── INTELLIGENT BUY DECISION (adaptive, profit-optimized) ──
                    # Two modes: (A) Trend-follow when 1h uptrend, (B) Mean-reversion
                    # dip-buy when RSI deeply oversold with strong confirmations.
                    # Mode B is crucial for spot BUY-ONLY — catches bottoms in
                    # downtrends/ranging which is 50-70% of market time.

                    # 1. Count confirmation signals (each adds real edge)
                    _confirms = 0
                    _has_classic = False
                    _has_modern = False
                    if _bull_pattern:
                        _confirms += 1; _has_classic = True
                    if _vol_spike:
                        _confirms += 1; _has_classic = True
                    if _higher_lows:
                        _confirms += 1; _has_classic = True
                    if _macd_bull_cross:
                        _confirms += 1; _has_modern = True
                    if _bb_squeeze and _bb_dir == "UP":
                        _confirms += 1; _has_modern = True
                    if _rsi_bull_div:
                        _confirms += 1; _has_modern = True

                    # 2. Trend alignment check — with deep-dip override
                    #    Normal entries: require 1h uptrend (strongest edge)
                    #    Deep dip (RSI<38 + 1+ confirms): trend override allowed
                    #    Extreme dip (RSI<30): buy regardless — deep oversold always bounces
                    #    This catches mean-reversion bottoms in bear/ranging markets
                    _is_deep_dip = float(rsi_5m) < 38.0 and _confirms >= 1
                    _is_extreme_dip = float(rsi_5m) < 30.0
                    if not bool(trend_up) and not _is_deep_dip and not _is_extreme_dip:
                        return False

                    # 3. Adaptive RSI limit based on confirmation count:
                    #    0 confirms: strict dip-buy only (base RSI limit from env)
                    #    1 confirm:  small pullback allowed (+2)
                    #    2+ confirms: pullback entry (+4)
                    #    RSI divergence: strongest reversal (+2)
                    #    Hard cap: RSI 50 (neutral). Above 50 = no longer oversold = no edge.
                    #    OLD cap was 58 → AAVE entered at RSI=52.2 → neutral territory → lost money.
                    rsi_limit = float(rsi_buy_max)  # base from env (default 45)
                    if _confirms >= 2 and (_ema_sep_pct > 0.2 or _is_deep_dip):
                        rsi_limit += 4.0
                    elif _confirms >= 1 and (bool(trend_up) or _is_deep_dip):
                        rsi_limit += 2.0
                    if _rsi_bull_div:
                        rsi_limit += 2.0
                    rsi_limit = min(50.0, rsi_limit)  # hard cap: never buy above neutral RSI
                    if float(rsi_5m) > rsi_limit:
                        return False

                    # 4. Must have at least ONE confirmation OR deep oversold
                    _deep_oversold = float(rsi_5m) < 35.0
                    if _confirms == 0 and not _deep_oversold:
                        return False

                    # 5. RSI momentum: block if RSI accelerating hard upward (>+10)
                    #    = buying top of bounce. RSI divergence overrides.
                    if _rsi_momentum > 10.0 and not _rsi_bull_div:
                        return False

                    # 6. 5m confirmation (EMA fast > slow) — relaxed when 2+ confirms
                    #    or deep dip (mean-reversion doesn't need 5m EMA alignment)
                    if req_conf and not bool(conf_up):
                        if _confirms < 2 and not _is_deep_dip and not _is_extreme_dip:
                            return False

                    return True

                def _ok_sell() -> bool:
                    # Mirror of _ok_buy() for short signals (disabled on spot, kept for symmetry)
                    rsi_limit = float(rsi_sell_min)
                    _has_classic_confirm = (_bear_pattern or _vol_spike or _lower_highs)
                    _has_modern_confirm = ((_macd_hist < 0 and not _macd_bull_cross) or (_bb_squeeze and _bb_dir == "DOWN") or _rsi_bear_div)
                    _has_any_confirm = (_has_classic_confirm or _has_modern_confirm)
                    if bool(trend_down) and _ema_sep_pct > 0.3 and _has_any_confirm:
                        rsi_limit = max(50.0, rsi_limit - 5.0)
                    if _rsi_bear_div and bool(trend_down):
                        rsi_limit = max(48.0, rsi_limit - 2.0)
                    if float(rsi_5m) < rsi_limit:
                        return False
                    if _rsi_momentum < -6.0 and not _rsi_bear_div:
                        return False
                    if not bool(trend_down):
                        return False
                    _deep_overbought = float(rsi_5m) > 70.0
                    if not (_has_any_confirm or _deep_overbought):
                        return False
                    if req_conf and not bool(conf_down):
                        return False
                    return True

                if _ok_buy():
                    action = "BUY"
                    gates["sd"] = "BUY"
                    gates["ok"] = True
                elif _ok_sell() and not bool(getattr(self, "_spot_buy_only", False)):
                    action = "SELL"
                    gates["sd"] = "SELL"
                    gates["ok"] = True
                else:
                    gates["sd"] = "NONE"
                    gates["ok"] = False

                try:
                    self._signal_scan_last_gates = dict(gates)
                except Exception:
                    pass

                try:
                    try:
                        lk = getattr(self, "_data_lock", None)
                        got = bool(lk.acquire(timeout=0.05)) if lk is not None else False
                    except Exception:
                        got = False
                    if got:
                        try:
                            self._verification_audit.append({
                                "ts": int(time.time()),
                                "id": 0,
                                "symbol": symbol,
                                "sd": str(gates.get("sd")),
                                "gate": "mtf",
                                "trd_1h": bool(trend_up) if (trend_up or trend_down) else False,
                                "cnf_5m": bool(conf_up) if (conf_up or conf_down) else False,
                                "rsi_5m": float(rsi_5m),
                                "ok": bool(gates.get("ok")),
                                "price": float(last_price),
                            })
                        finally:
                            try:
                                lk.release()
                            except Exception:
                                pass
                except Exception:
                    pass

                if not action:
                    try:
                        # Detailed skip reasons — match adaptive _ok_buy() logic
                        rsi_v = float(rsi_5m)
                        # Count confirms for diagnostic (mirrors _ok_buy step 1)
                        _dc = sum([bool(_bull_pattern), bool(_vol_spike), bool(_higher_lows),
                                   bool(_macd_bull_cross), bool(_bb_squeeze and _bb_dir == "UP"), bool(_rsi_bull_div)])
                        # Deep dip checks (mirrors _ok_buy step 2)
                        _dd = rsi_v < 38.0 and _dc >= 1
                        _ed = rsi_v < 30.0
                        # Compute effective adaptive RSI limit (mirrors _ok_buy step 3)
                        _eff_lim = float(rsi_buy_max)
                        if _dc >= 2 and (_ema_sep_pct > 0.2 or _dd):
                            _eff_lim += 4.0
                        elif _dc >= 1 and (bool(trend_up) or _dd):
                            _eff_lim += 2.0
                        if _rsi_bull_div:
                            _eff_lim += 2.0
                        _eff_lim = min(50.0, _eff_lim)

                        if not bool(trend_up) and not bool(trend_down):
                            _set_skip("NO_TRD")
                        elif not bool(trend_up) and not _dd and not _ed:
                            # Downtrend but RSI not deep enough or not enough confirms for dip buy
                            if rsi_v < 38.0 and _dc < 1:
                                _set_skip(f"DIP_NO_CNF(rsi={rsi_v:.0f},cnf={_dc})")
                            else:
                                _set_skip(f"TRD_DN(rsi={rsi_v:.0f})")
                        elif rsi_v > _eff_lim and rsi_v < float(rsi_sell_min):
                            _set_skip(f"RSI_HI({rsi_v:.0f}>{_eff_lim:.0f})")
                        elif _dc == 0 and rsi_v >= 35.0:
                            _set_skip("NO_CONFIRM")
                        elif _rsi_momentum > 10.0 and not _rsi_bull_div:
                            _set_skip(f"RSI_MOM({_rsi_momentum:+.1f})")
                        elif req_conf and not bool(conf_up) and _dc < 2 and not _dd and not _ed:
                            _set_skip("NO_CNF")
                        else:
                            _set_skip("NO_DIR")
                    except Exception:
                        pass
                    continue

                # Liquidity guard: skip dead markets where closes barely move
                try:
                    _uniq = len(set(_entry_closes[-30:]))
                    if _uniq < 4:
                        try:
                            self._illiquid_cache[symbol] = time.time()
                            _set_skip(f"ILQ(uc={_uniq}/30)")
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

                # Min trade size guard: skip symbols where $position_usd can't buy min_amount
                # (e.g. BTC at $65k with $5 position → 0.00007 < min 0.0001)
                try:
                    _pair_check = self._kraken_supported_pair(symbol)
                    if _pair_check and self._kraken:
                        _min_a = self._kraken_min_amount(_pair_check)
                        if _min_a > 0 and last_price > 0:
                            _pos_usd = float(self._kraken_effective_position_usd())
                            _max_buyable = _pos_usd / last_price
                            if _max_buyable < _min_a:
                                try:
                                    # Don't cache MIN_A — balance changes as account grows
                                    _set_skip(f"MIN_A(n={_min_a},b={_max_buyable:.8f})")
                                except Exception:
                                    pass
                                continue
                except Exception:
                    pass

                # Volume sanity: reject only truly zero-volume markets (avg < 0.01)
                # EUR/OKX EEA: fractional volumes (0.1-1.0) are real trades.
                # LV2 (volume_confirm) removed — was blocking valid signals on low-liquidity
                # EUR pairs. Upstream VOL_DROUGHT + STALE_1H are sufficient safety.
                try:
                    vols = [float(x[5]) for x in c5 if len(x) > 5]
                    avg_vol = sum(vols) / len(vols) if vols else 0.0
                    if avg_vol > 0 and avg_vol < 0.01:
                        try:
                            self._illiquid_cache[symbol] = time.time()
                            _set_skip(f"ZV(avg={avg_vol:.4f})")
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

                # Build TP/SL using ATR already computed in SAFETY LAYER 1 (with 1h fallback)
                # _atr14 was set above during volatility regime check
                try:
                    atr = max(1e-12, _atr14)
                except Exception:
                    atr = max(1e-12, float(last_price) * 0.01)

                # ATR sanity guard: if ATR is essentially zero, market is dead — skip
                atr_pct_check = (atr / max(1e-12, float(last_price))) * 100.0
                if atr_pct_check < 0.005:
                    try:
                        self._illiquid_cache[symbol] = time.time()
                        _set_skip(f"ATR0({atr_pct_check:.4f}%)")
                    except Exception:
                        pass
                    continue

                # Adaptive ATR multipliers — REACHABLE within 8h max trade duration:
                # KEY INSIGHT: Old TP1 at 4-5x ATR = 3-5% move = UNREACHABLE in 8h on most coins.
                # Result: trades expire at MAX_TIME with small loss. Edge = negative.
                # NEW: TP1 at 1.5-2x ATR = 1-2% move = reachable in 2-4h on active coins.
                # TP2 at 3-4x = stretch goal. SL at 2x ATR = tighter, better R:R.
                # Example: AAVE ATR=0.94%, entry €97.55
                #   OLD: TP1=4×0.94%=3.76% → €101.22 (never reached in 8h) → MAX_TIME loss
                #   NEW: TP1=1.8×0.94%=1.69% → €99.20 (reachable in 3-5h) → profit locked
                _atr_pct_regime = (atr / max(1e-12, float(last_price))) * 100.0
                if _atr_pct_regime < 0.3:
                    # Ultra-low vol: skip — can't make money here
                    try:
                        self._illiquid_cache[symbol] = time.time()
                        _set_skip(f"LOW_VOL({_atr_pct_regime:.2f}%)")
                    except Exception:
                        pass
                    continue
                elif _atr_pct_regime < 0.8:
                    # Low vol (0.3-0.8%): wider multiples but still reachable
                    # 0.5% ATR → TP1=2.5×0.5%=1.25% (€0.48 on €38 trade, net €0.33 after fees)
                    _sl_mult, _tp_mults = 2.0, [2.5, 5.0, 8.0, 12.0]
                elif _atr_pct_regime > 2.0:
                    # High vol (>2%): fast moves, take profit quickly but R:R must pass 1.5
                    # 3% ATR → TP1=1.5×3%=4.5% (€1.71 on €38), TP2=3.0×3%=9% → R:R=3.0/1.5=2.0 ✓
                    _sl_mult, _tp_mults = 1.5, [1.5, 3.0, 5.0, 8.0]
                else:
                    # Normal vol (0.8-2%): balanced for reachability + profit
                    # 1% ATR → TP1=1.8×1%=1.8% (€0.68 on €38 trade, net €0.53 after fees)
                    _sl_mult, _tp_mults = 2.0, [1.8, 3.5, 6.0, 9.0]

                if action == "BUY":
                    entry = float(last_price)
                    sl = entry - (atr * _sl_mult)
                    tps = [entry + atr * m for m in _tp_mults]
                else:
                    entry = float(last_price)
                    sl = entry + (atr * _sl_mult)
                    tps = [entry - atr * m for m in _tp_mults]

                # Minimum 40 pips profit on TP1. Pip size scales with price magnitude:
                # BTC ~$65000 → pip=1.0, ETH ~$1900 → pip=0.01, XRP ~$1.35 → pip=0.0001
                try:
                    if entry > 10000:
                        _pip = 1.0
                    elif entry > 100:
                        _pip = 0.01
                    elif entry > 1:
                        _pip = 0.0001
                    elif entry > 0.01:
                        _pip = 0.000001
                    else:
                        _pip = 0.00000001
                    _min_tp1_dist = 40.0 * _pip  # 40 pips minimum
                    _tp1_dist = abs(tps[0] - entry) if tps else 0.0
                    if _tp1_dist < _min_tp1_dist:
                        # Scale all TPs proportionally to meet 40-pip minimum on TP1
                        _scale = _min_tp1_dist / max(1e-15, _tp1_dist) if _tp1_dist > 0 else 2.0
                        if action == "BUY":
                            tps = [entry + (t - entry) * _scale for t in tps]
                            sl = entry - abs(entry - sl) * _scale  # Keep R:R ratio
                        else:
                            tps = [entry - (entry - t) * _scale for t in tps]
                            sl = entry + abs(sl - entry) * _scale
                except Exception:
                    pass

                # Round TP/SL to exchange price precision (prevents float comparison drift)
                try:
                    _pair = self._kraken_supported_pair(symbol)
                    if _pair:
                        sl = self._kraken_round_price(_pair, sl)
                        tps = [self._kraken_round_price(_pair, t) for t in tps]
                except Exception:
                    pass

                # Sanity: SL and all TPs must be positive (ATR could exceed price for micro-caps)
                if sl <= 0 or any(t <= 0 for t in tps):
                    try:
                        _set_skip(f"NEG(sl={sl},tp1={tps[0]})")
                    except Exception:
                        pass
                    continue

                # Confidence score: multi-factor scoring with market intelligence
                # Each factor contributes real edge — no inflated base scores
                # Score range: 75 (minimum to pass) → 99 (perfect setup)
                try:
                    # RSI depth: deeper oversold/overbought = stronger mean-reversion
                    rsi_ext = abs(float(rsi_5m) - 50.0) / 50.0  # 0..1
                    rsi_ext = min(0.95, rsi_ext)
                    rsi_deep = max(0.0, (rsi_ext - 0.3) * 3.0)  # scores from RSI 35/65

                    # EMA trend strength: bigger separation = stronger trend
                    ema_bonus = min(1.0, _ema_sep_pct / 0.5)

                    # Timeframe alignment: 1h trend + 5m confirm in same direction
                    align_bonus = 1.0 if ((trend_up and conf_up) or (trend_down and conf_down)) else 0.0

                    # EMA crossover freshness: recent cross = stronger edge
                    cross_bonus = 1.0 if _ema_cross_fresh else 0.0

                    # ATR strength: higher ATR = more room for profit (scale 0→1 over 0→1% ATR)
                    atr_bonus = min(1.0, _atr_pct_regime / 1.0)

                    # NEW: Pattern recognition bonus (hammer/engulfing/morning star)
                    pattern_bonus = 1.0 if (_bull_pattern or _bear_pattern) else 0.0

                    # NEW: Volume spike bonus (institutional buying/selling)
                    vol_spike_bonus = min(1.0, (_vol_spike_ratio - 1.0) / 1.0) if _vol_spike else 0.0

                    # NEW: Price structure bonus (higher lows for BUY, lower highs for SELL)
                    structure_bonus = 0.0
                    if action == "BUY" and _higher_lows:
                        structure_bonus = 1.0
                    elif action == "SELL" and _lower_highs:
                        structure_bonus = 1.0

                    # MODERN: MACD histogram cross bonus (momentum shift confirmation)
                    macd_bonus = 0.0
                    if action == "BUY" and _macd_bull_cross:
                        macd_bonus = 1.0
                    elif action == "SELL" and (_macd_hist < 0 and not _macd_bull_cross):
                        macd_bonus = 1.0

                    # MODERN: Bollinger Band squeeze bonus (breakout timing)
                    bb_bonus = 0.0
                    if _bb_squeeze:
                        if (action == "BUY" and _bb_dir == "UP") or (action == "SELL" and _bb_dir == "DOWN"):
                            bb_bonus = 1.0

                    # MODERN: RSI divergence bonus (strongest reversal signal — highest weight)
                    div_bonus = 0.0
                    if action == "BUY" and _rsi_bull_div:
                        div_bonus = 1.0
                    elif action == "SELL" and _rsi_bear_div:
                        div_bonus = 1.0

                    # Deep dip bonus: RSI<38 in downtrend/ranging = mean-reversion entry
                    # Compensates for align_bonus=0 (trend≠5m) on counter-trend dips
                    # Statistically strongest spot BUY setup — catching the bottom
                    dip_bonus = 0.0
                    if action == "BUY" and float(rsi_5m) < 38.0 and not bool(trend_up):
                        dip_bonus = 1.0  # deep dip in non-uptrend

                    # Base 78 + factors (profit-optimized weights):
                    # RSI depth (0-4), EMA (0-2), alignment (0-4), cross (0-1.5),
                    # ATR (0-3), pattern (0-3), volume (0-2), structure (0-2),
                    # MACD (0-2), BB squeeze (0-1.5), RSI divergence (0-3), dip (0-4)
                    # Max possible: 78 + 4+2+4+1.5+3+3+2+2+2+1.5+3+4 = 110 → capped 99
                    # Trend-follow: base78+align4+ATR3+ema2 = 87 (passes 85)
                    # Deep dip: base78+dip4+ATR3+rsi_deep2 = 87 (passes 85)
                    conf = (78.0
                            + 4.0 * rsi_deep
                            + 2.0 * ema_bonus
                            + 4.0 * align_bonus
                            + 1.5 * cross_bonus
                            + 3.0 * atr_bonus
                            + 3.0 * pattern_bonus
                            + 2.0 * vol_spike_bonus
                            + 2.0 * structure_bonus
                            + 2.0 * macd_bonus
                            + 1.5 * bb_bonus
                            + 3.0 * div_bonus
                            + 4.0 * dip_bonus)
                    conf = max(0.0, min(99.0, conf))
                except Exception:
                    conf = 85.0

                # Detailed reasoning with all intelligence factors
                try:
                    atr_pct = (atr / max(1e-12, entry)) * 100.0
                    _parts = [f"RSI={float(rsi_5m):.1f}"]
                    _parts.append(f"ATR={atr_pct:.2f}%")
                    if _bull_name:
                        _parts.append(f"PAT={_bull_name}")
                    if _bear_name:
                        _parts.append(f"PAT={_bear_name}")
                    if _vol_spike:
                        _parts.append(f"VOL={_vol_spike_ratio:.1f}x")
                    if _higher_lows:
                        _parts.append("HL+")
                    if _lower_highs:
                        _parts.append("LH+")
                    if _ema_cross_fresh:
                        _parts.append("XFRESH")
                    if align_bonus > 0:
                        _parts.append("ALIGN")
                    if _macd_bull_cross:
                        _parts.append("MACD_X")
                    elif _macd_hist > 0:
                        _parts.append("MACD+")
                    if _bb_squeeze:
                        _parts.append(f"BB_SQ({_bb_dir})")
                    if _rsi_bull_div:
                        _parts.append("RSI_DIV+")
                    if _rsi_bear_div:
                        _parts.append("RSI_DIV-")
                    if dip_bonus > 0:
                        _parts.append("DEEP_DIP")
                    reasoning = f"MTF: 1h EMA({self._kraken_mtf_fast_ema}/{self._kraken_mtf_slow_ema}) | {' '.join(_parts)}"
                except Exception:
                    reasoning = f"MTF: 1h EMA + 5m RSI={float(rsi_5m):.1f}"

                out.append(
                    QuantumTradingSignal(
                        signal_id=0,
                        symbol=symbol,
                        action=action,
                        entry_price=float(entry),
                        targets=[float(x) for x in tps],
                        stop_loss=float(sl),
                        confidence=float(conf),
                        reasoning=reasoning,
                    )
                )
            except Exception:
                try:
                    _set_skip("ERR")
                except Exception:
                    pass
                continue

        out.sort(key=lambda x: float(getattr(x, "confidence", 0.0)), reverse=True)
        try:
            self._signal_scan_skip_dist = dict(_skip_counts)
        except Exception:
            pass
        return out

    def _filter_signals(self, signals: List[QuantumTradingSignal]) -> List[QuantumTradingSignal]:
        try:
            raw_n = 0
            kept_n = 0
            drop_low_conf = 0
            drop_tp1_pips = 0
            drop_tp1_pct = 0
            drop_rr = 0
            drop_cooldown = 0
            out: List[QuantumTradingSignal] = []
            for s in signals:
                raw_n += 1

                # Skip symbols that already have an active trade (early filter)
                if str(getattr(s, "symbol", "") or "") in self.active_signals:
                    continue

                _s_conf = float(getattr(s, "confidence", 0.0))
                if _s_conf < self._min_signal_confidence:
                    drop_low_conf += 1
                    try:
                        self._signal_filter_last_drop = f"CONF({_s_conf:.1f}<{self._min_signal_confidence})"
                        self._signal_filter_last_sym = str(getattr(s, "symbol", ""))
                    except Exception:
                        pass
                    continue

                # SL cooldown: prevent revenge trading on the same symbol after a recent SL hit
                try:
                    cd_map = getattr(self, "_sl_cooldown_map", {})
                    cd_sec = float(getattr(self, "_sl_cooldown_sec", 300.0) or 300.0)
                    sym = str(getattr(s, "symbol", "") or "")
                    if cd_sec > 0 and sym in cd_map:
                        elapsed = time.time() - float(cd_map[sym])
                        if elapsed < cd_sec:
                            drop_cooldown += 1
                            continue
                except Exception:
                    pass

                # Require enough room to TP1 (pips) to better cover fees/spread
                try:
                    if getattr(s, "targets", None) and getattr(s, "entry_price", None):
                        tp1 = float(s.targets[0])
                        entry = float(s.entry_price)
                        sym = str(getattr(s, "symbol", "") or "")

                        # For crypto, a fixed "pips" rule is misleading (often becomes dollars). Use TP1 percent instead.
                        if not self._is_crypto_symbol(sym):
                            pip_size = float(self._pip_size_for_symbol(sym, entry))
                            if pip_size > 0:
                                tp1_pips = abs(tp1 - entry) / pip_size
                                if tp1_pips < float(self._min_tp1_pips):
                                    drop_tp1_pips += 1
                                    continue

                        # Minimum TP1 percent move (applied across all markets)
                        if entry > 0 and float(self._min_tp1_pct) > 0:
                            tp1_pct = (abs(tp1 - entry) / entry) * 100.0
                            if tp1_pct < float(self._min_tp1_pct):
                                drop_tp1_pct += 1
                                try:
                                    self._signal_filter_last_drop = f"TP1%({tp1_pct:.2f}<{self._min_tp1_pct})"
                                    self._signal_filter_last_sym = str(getattr(s, 'symbol', ''))
                                except Exception:
                                    pass
                                continue

                        # Risk-Reward ratio filter: use TP2 (primary target) vs SL
                        # TP1 is a quick partial exit; TP2 is the real profit target
                        sl = float(getattr(s, "stop_loss", 0.0) or 0.0)
                        risk = abs(entry - sl) if sl > 0 else 0.0
                        tgts = getattr(s, "targets", []) or []
                        rr_target = float(tgts[1]) if len(tgts) >= 2 else tp1
                        reward = abs(rr_target - entry)
                        if risk > 0:
                            rr = reward / risk
                            if rr < float(getattr(self, "_min_rr_ratio", 1.5) or 1.5):
                                drop_rr += 1
                                try:
                                    self._signal_filter_last_drop = f"RR({rr:.2f}<{self._min_rr_ratio})"
                                    self._signal_filter_last_sym = str(getattr(s, 'symbol', ''))
                                except Exception:
                                    pass
                                continue
                except Exception:
                    pass

                out.append(s)
                kept_n += 1
            out.sort(key=lambda x: float(getattr(x, "confidence", 0.0)), reverse=True)

            try:
                try:
                    lk = getattr(self, "_data_lock", None)
                    got = bool(lk.acquire(timeout=0.05)) if lk is not None else False
                except Exception:
                    got = False
                if got:
                    try:
                        self._verification_audit.append({
                            "t": int(time.time()),
                            "type": "signal_filter",
                            "raw": int(raw_n),
                            "kept": int(kept_n),
                            "drop_low_conf": int(drop_low_conf),
                            "drop_tp1_pips": int(drop_tp1_pips),
                            "drop_tp1_pct": int(drop_tp1_pct),
                            "drop_bad_rr": int(drop_rr),
                            "drop_sl_cooldown": int(drop_cooldown),
                            "min_conf": float(self._min_signal_confidence),
                            "min_tp1_pips": float(self._min_tp1_pips),
                            "min_tp1_pct": float(self._min_tp1_pct),
                            "min_rr": float(getattr(self, "_min_rr_ratio", 1.5) or 1.5),
                        })
                    finally:
                        try:
                            lk.release()
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                self._signal_scan_last_raw = int(raw_n)
                self._signal_scan_last_filtered = int(kept_n)
            except Exception:
                pass

            # Reset scan timer so dashboard doesn't show STALLED between cycles
            try:
                self._signal_scan_started_ts = 0.0
            except Exception:
                pass

            return out
        except Exception:
            try:
                self._signal_scan_started_ts = 0.0
            except Exception:
                pass
            return signals
    
    def check_exit_conditions(self, signal: QuantumTradingSignal, current_price: float, position_size: float, amount: float = 0.0) -> Tuple[bool, float, str]:
        """Check if trade should be closed — only triggers on SL hit.
        TP exits are handled by exchange triggers + trailing stop ladder.
        The trailing stop progressively moves SL up as TP levels are reached,
        so SL-based exit captures TP profits automatically."""
        pnl = 0.0
        reason = ""
        # Use real amount (units of base asset) for PnL calculation
        qty = amount if amount > 0 else 0.0
        
        if signal.action == 'BUY':
            if current_price <= signal.stop_loss:
                pnl = (current_price - signal.entry_price) * qty
                reason = "SL"
                return True, pnl, reason
        
        elif signal.action == 'SELL':
            if current_price >= signal.stop_loss:
                pnl = (signal.entry_price - current_price) * qty
                reason = "SL"
                return True, pnl, reason
        
        return False, pnl, reason
    
    async def send_telegram_notification(self, symbol: str, action: str, pnl: float, reason: str, result: str):
        """Backward-compatible wrapper"""
        try:
            sig = None
            pos_size = None
            try:
                trade = self.active_signals.get(symbol, {})
                sig = trade.get('signal')
                pos_size = trade.get('position_size')
            except Exception:
                sig = None
                pos_size = None

            if sig is not None:
                await self._send_telegram_notification_for_signal(sig, pnl=pnl, reason=reason, result=result, position_size=pos_size)
            else:
                # fallback to old-style without ID
                await self._send_telegram_notification_for_signal(
                    QuantumTradingSignal(signal_id=0, symbol=symbol, action=action, entry_price=0.0, targets=[], stop_loss=0.0, confidence=0.0, reasoning=""),
                    pnl=pnl,
                    reason=reason,
                    result=result,
                    position_size=pos_size,
                )
        except Exception as e:
            logger.error(f"Failed to send Telegram notification wrapper: {e}")

    async def _send_telegram_notification_for_signal(self, signal: QuantumTradingSignal, pnl: float, reason: str, result: str, position_size: Optional[float]):
        """Send professional Telegram notification with signal_id, duration, ROI%, entry/exit (Freqtrade-level)"""
        try:
            # derive price move from pnl when possible
            move = None

            # If we still can't infer move, use the known TP/SL level
            if (move is None or move == 0) and signal.entry_price:
                try:
                    if reason.startswith("TP") and signal.targets:
                        idx = int(reason.replace("TP", "").replace("_TRIGGER(exchange)", "")) - 1
                        if 0 <= idx < len(signal.targets):
                            move = abs(float(signal.targets[idx]) - float(signal.entry_price))
                    elif "SL" in reason and signal.stop_loss:
                        move = abs(float(signal.stop_loss) - float(signal.entry_price))
                except Exception:
                    pass

            if move is None:
                move = 0.0

            pip_size = self._pip_size_for_symbol(signal.symbol, float(signal.entry_price or 0.0))
            if pip_size <= 0:
                pip_size = 1.0
            pips = int(max(1, round(float(move) / float(pip_size))))

            # Duration + ROI% for Freqtrade-level detail
            _dur_str = ""
            try:
                _td = self.active_signals.get(signal.symbol, {})
                _et = _td.get("entry_time")
                if _et:
                    _secs = int((datetime.now() - _et).total_seconds())
                    if _secs >= 3600:
                        _dur_str = f"\n⏱ {_secs//3600}h{(_secs%3600)//60}m"
                    elif _secs >= 60:
                        _dur_str = f"\n⏱ {_secs//60}m{_secs%60}s"
                    else:
                        _dur_str = f"\n⏱ {_secs}s"
            except Exception:
                pass
            _roi_str = ""
            try:
                _usd = float(position_size or 0) or float(self.active_signals.get(signal.symbol, {}).get("usd_size", 0) or 0)
                if _usd > 0:
                    _roi_str = f" ({abs(pnl)/_usd*100:.1f}%)"
            except Exception:
                pass

            if result == "WIN" and reason.startswith("TP"):
                tp_level = reason.replace("TP", "").replace("_TRIGGER(exchange)", "")
                message = f"#{signal.signal_id} {signal.symbol} {signal.action}\n\nTP {tp_level} 𝐇𝐈𝐓 {pips}+ PIPS ✅\n💰 +${abs(pnl):.2f}{_roi_str}{_dur_str}"
            elif result == "WIN":
                message = f"✅ #{signal.signal_id} {signal.symbol} {signal.action}\n\n{reason}\n💰 +${abs(pnl):.2f}{_roi_str}{_dur_str}"
            elif result == "LOSS":
                message = f"❌ #{signal.signal_id} {signal.symbol} {signal.action}\n\n{reason}\n💸 -${abs(pnl):.2f}{_roi_str}{_dur_str}"
            else:
                return

            if self._telegram_enabled():
                threading.Thread(target=self._send_telegram_message_sync, args=(message,), daemon=True).start()
            print(f"\n📱 TELEGRAM NOTIFICATION:")
            print(f"   {message}")
            print(f"   {'='*50}")
            
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    async def send_entry_signal(self, signal: QuantumTradingSignal) -> None:
        try:
            profile = self._market_profile(signal.symbol)
            if profile is not None:
                dec = int(profile["dec"])
                entry_low = signal.entry_price
                entry_high = signal.entry_price + profile["range"]
                fmt = f"{{:.{dec}f}}"
            else:
                # Default (incl. crypto): derive decimals from entry price magnitude
                entry_price = float(signal.entry_price)
                if entry_price >= 10000:
                    dec = 1
                elif entry_price >= 100:
                    dec = 2
                elif entry_price >= 1:
                    dec = 4
                elif entry_price >= 0.01:
                    dec = 6
                elif entry_price >= 0.0001:
                    dec = 8
                else:
                    dec = 10

                # Entry range ~0.1% of price, clamped to a sensible minimum
                range_value = max(entry_price * 0.001, 10 ** (-dec))
                entry_low = entry_price
                entry_high = entry_price + range_value
                fmt = f"{{:.{dec}f}}"

            # Unified format for ALL markets (Freqtrade-level detail)
            _tgts = list(signal.targets or [])[:4]
            _tp_lines = "\n".join(f"TP{i+1} {fmt.format(t)}" for i, t in enumerate(_tgts)) if _tgts else "TP —"
            # R:R from TP2 (main target) vs SL
            _rr_str = ""
            try:
                if len(_tgts) >= 2 and signal.stop_loss and signal.entry_price:
                    _risk = abs(float(signal.entry_price) - float(signal.stop_loss))
                    _reward = abs(float(_tgts[1]) - float(signal.entry_price))
                    if _risk > 0:
                        _rr_str = f"\nR:R {_reward/_risk:.1f}:1"
            except Exception:
                pass
            # Confidence + reasoning summary
            _conf_str = f"\n⚡ Conf: {signal.confidence:.0f}%" if signal.confidence else ""
            _reason_short = ""
            try:
                if signal.reasoning:
                    _reason_short = f"\n📊 {signal.reasoning[:80]}"
            except Exception:
                pass
            message = (
                f"#{signal.signal_id} {signal.symbol} {signal.action} {fmt.format(entry_low)}/{fmt.format(entry_high)}\n\n"
                f"{_tp_lines}\n"
                f"SL {fmt.format(signal.stop_loss)}"
                f"{_rr_str}{_conf_str}{_reason_short}"
            )

            if self._telegram_enabled():
                threading.Thread(target=self._send_telegram_message_sync, args=(message,), daemon=True).start()

            print(f"\n📱 TELEGRAM NOTIFICATION:")
            print(f"   {message}")
            print(f"   {'='*50}")
        except Exception as e:
            logger.error(f"Failed to send entry signal: {e}")
    
    async def manage_active_trades(self):
        """Manage active trades - WORKING VERSION"""
        if not self.active_signals:
            print(f"\nNO ACTIVE TRADES TO MANAGE")
            return
        
        print(f"\nMANAGING {len(self.active_signals)} ACTIVE TRADES...")
        
        completed_trades = []
        
        _manage_t0 = time.time()
        # Pre-check: if executor is heavily stalled, reset it before managing trades
        try:
            if int(getattr(self, "_ccxt_timeout_failures", 0) or 0) > 3:
                self._maybe_reset_ccxt_executor()
        except Exception:
            pass
        for symbol, trade_data in list(self.active_signals.items()):
            signal = trade_data['signal']
            entry_time = trade_data['entry_time']
            _trade_t0 = time.time()
            
            # Per-trade timeout guard: if total manage time exceeds 60s, skip remaining trades
            if (time.time() - _manage_t0) > 60.0:
                print(f"   ⚠️ manage_active_trades timeout (>{60}s) — skipping {symbol}", flush=True)
                continue
            
            try:
                # ── Check if exchange-side TP/SL trigger already fired ──
                _trigger_exit = False
                _trigger_reason = ""
                _trigger_pnl = 0.0
                _trigger_exit_px = 0.0
                try:
                    _is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
                    _tp_oid = trade_data.get("tp_order_id")
                    _sl_oid = trade_data.get("sl_order_id")
                    if _is_live and (_tp_oid or _sl_oid):
                        pair = self._kraken_supported_pair(symbol)
                        # Check TP trigger status (trigger orders need {"stop":True})
                        if _tp_oid and pair:
                            _tp_st = self._fetch_trigger_order_safe(str(_tp_oid), pair)
                            if isinstance(_tp_st, dict) and str(_tp_st.get("status", "")).lower() in {"closed", "filled"}:
                                _trigger_exit = True
                                _tp_filled_px = float(_tp_st.get("average") or _tp_st.get("price") or _tp_st.get("triggerPrice") or 0)
                                _tp_filled_amt = float(_tp_st.get("filled") or 0)
                                _trigger_exit_px = _tp_filled_px
                                _trigger_reason = f"TP_TRIGGER(exchange)"
                                amt = float(trade_data.get("amount", 0) or 0)
                                if signal.action == "BUY" and _tp_filled_px > 0:
                                    _trigger_pnl = (_tp_filled_px - signal.entry_price) * (amt if amt > 0 else _tp_filled_amt)
                                elif _tp_filled_px > 0:
                                    _trigger_pnl = (signal.entry_price - _tp_filled_px) * (amt if amt > 0 else _tp_filled_amt)
                                _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                                print(f"   🎯 {symbol}: TP TRIGGER FILLED on exchange! avg={_tp_filled_px:.6f} PnL={_ccy}{_trigger_pnl:+.4f}", flush=True)
                                # Cancel SL trigger (OCO)
                                if _sl_oid and pair:
                                    self._cancel_trigger_order(str(_sl_oid), pair)
                                    trade_data["sl_order_id"] = None
                                trade_data["tp_order_id"] = None
                        # Check SL trigger status (only if TP didn't fire)
                        if not _trigger_exit and _sl_oid and pair:
                            _sl_st = self._fetch_trigger_order_safe(str(_sl_oid), pair)
                            if isinstance(_sl_st, dict) and str(_sl_st.get("status", "")).lower() in {"closed", "filled"}:
                                _trigger_exit = True
                                _sl_filled_px = float(_sl_st.get("average") or _sl_st.get("price") or _sl_st.get("triggerPrice") or 0)
                                _sl_filled_amt = float(_sl_st.get("filled") or 0)
                                _trigger_exit_px = _sl_filled_px
                                _trigger_reason = f"SL_TRIGGER(exchange)"
                                amt = float(trade_data.get("amount", 0) or 0)
                                if signal.action == "BUY" and _sl_filled_px > 0:
                                    _trigger_pnl = (_sl_filled_px - signal.entry_price) * (amt if amt > 0 else _sl_filled_amt)
                                elif _sl_filled_px > 0:
                                    _trigger_pnl = (signal.entry_price - _sl_filled_px) * (amt if amt > 0 else _sl_filled_amt)
                                _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                                print(f"   🛡️ {symbol}: SL TRIGGER FILLED on exchange! avg={_sl_filled_px:.6f} PnL={_ccy}{_trigger_pnl:+.4f}", flush=True)
                                # Cancel TP trigger (OCO)
                                if _tp_oid and pair:
                                    self._cancel_trigger_order(str(_tp_oid), pair)
                                    trade_data["tp_order_id"] = None
                                trade_data["sl_order_id"] = None
                except Exception as e:
                    logger.debug("Trigger check error for %s: %s", symbol, e)

                # ── Freqtrade pattern: SL/TP trigger recovery ──
                # If SL or TP trigger was canceled/expired on exchange, re-place it.
                # Without this, a single canceled trigger = unlimited loss potential.
                if not _trigger_exit:
                    try:
                        _is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
                        if _is_live:
                            _sl_oid = trade_data.get("sl_order_id")
                            _tp_oid = trade_data.get("tp_order_id")
                            pair = self._kraken_supported_pair(symbol)
                            amt = float(trade_data.get("amount", 0) or 0)
                            _DEAD_STATUSES = {"canceled", "cancelled", "expired", "rejected"}
                            # Check SL trigger health
                            if _sl_oid and pair and amt > 0:
                                _sl_check = self._fetch_trigger_order_safe(str(_sl_oid), pair)
                                if isinstance(_sl_check, dict) and str(_sl_check.get("status", "")).lower() in _DEAD_STATUSES:
                                    logger.warning("SL trigger %s DEAD (%s) for %s — re-placing at %.6f",
                                                   _sl_oid, _sl_check.get("status"), symbol, float(signal.stop_loss))
                                    print(f"   ⚠️ {symbol}: SL trigger DEAD — re-placing at {float(signal.stop_loss):.6f}", flush=True)
                                    trade_data["sl_order_id"] = None
                                    _recovery = self._place_tp_sl_triggers(symbol, signal.action, amt, None, float(signal.stop_loss))
                                    if _recovery.get("sl_order_id"):
                                        trade_data["sl_order_id"] = _recovery["sl_order_id"]
                                        print(f"   ✅ {symbol}: SL trigger restored", flush=True)
                            # Check TP trigger health
                            if _tp_oid and pair and amt > 0:
                                _tp_check = self._fetch_trigger_order_safe(str(_tp_oid), pair)
                                if isinstance(_tp_check, dict) and str(_tp_check.get("status", "")).lower() in _DEAD_STATUSES:
                                    logger.warning("TP trigger %s DEAD (%s) for %s — re-placing",
                                                   _tp_oid, _tp_check.get("status"), symbol)
                                    trade_data["tp_order_id"] = None
                                    # Determine current TP level based on trailing stop progress
                                    _tp_level = None
                                    if trade_data.get("_sl_moved_tp2") and len(signal.targets) > 3:
                                        _tp_level = float(signal.targets[3])  # TP4
                                    elif trade_data.get("_sl_moved_tp1") and len(signal.targets) > 2:
                                        _tp_level = float(signal.targets[2])  # TP3
                                    elif trade_data.get("_sl_moved_be") and len(signal.targets) > 1:
                                        _tp_level = float(signal.targets[1])  # TP2
                                    elif signal.targets:
                                        _tp_level = float(signal.targets[1]) if len(signal.targets) > 1 else float(signal.targets[0])
                                    if _tp_level:
                                        _recovery = self._place_tp_sl_triggers(symbol, signal.action, amt, _tp_level, None)
                                        if _recovery.get("tp_order_id"):
                                            trade_data["tp_order_id"] = _recovery["tp_order_id"]
                                            print(f"   ✅ {symbol}: TP trigger restored at {_tp_level:.6f}", flush=True)
                    except Exception as _recov_e:
                        logger.debug("Trigger recovery error for %s: %s", symbol, _recov_e)

                if _trigger_exit:
                    # Trade was closed by exchange trigger — full bookkeeping, skip exit order
                    try:
                        _trigger_pnl -= self._estimate_fees(float(trade_data.get("usd_size", 0) or 0))
                    except Exception:
                        pass

                    # Update internal PnL
                    self.active_signals[symbol]['status'] = 'CLOSED'
                    self.active_signals[symbol]['pnl'] = _trigger_pnl
                    self.daily_pnl += _trigger_pnl
                    self.total_pnl += _trigger_pnl

                    self.total_trades += 1
                    _trig_result = "WIN" if _trigger_pnl > 0 else "LOSS"
                    if _trigger_pnl > 0:
                        self.winning_trades += 1
                        self._consecutive_losses = 0
                        self._consecutive_wins += 1
                        await self.send_telegram_notification(symbol, signal.action, _trigger_pnl, _trigger_reason, "WIN")
                    else:
                        self._consecutive_losses += 1
                        self._consecutive_wins = 0
                        await self.send_telegram_notification(symbol, signal.action, _trigger_pnl, _trigger_reason, "LOSS")

                    # Compute time elapsed for trade history
                    _trig_elapsed = (datetime.now() - entry_time).total_seconds() if entry_time else 0

                    # Derive exit price from trigger fill info
                    _trig_exit_px = 0.0
                    _trig_pips = 0.0
                    try:
                        _trig_exit_px = float(_trigger_exit_px) if _trigger_exit_px else 0.0
                        if _trig_exit_px > 0 and signal.entry_price > 0:
                            pip_size = self._pip_size_for_symbol(symbol, float(signal.entry_price))
                            if pip_size <= 0:
                                pip_size = 1.0
                            if signal.action == 'BUY':
                                _trig_pips = (_trig_exit_px - signal.entry_price) / pip_size
                            else:
                                _trig_pips = (signal.entry_price - _trig_exit_px) / pip_size
                    except Exception:
                        pass

                    # Record in trade history
                    with self._data_lock:
                        self._trade_history.append({
                            "id": int(getattr(signal, 'signal_id', 0)),
                            "ts": int(datetime.now().timestamp()),
                            "symbol": symbol,
                            "result": _trig_result,
                            "pnl": float(_trigger_pnl),
                            "exit": float(_trig_exit_px),
                            "pips": float(_trig_pips),
                            "reason": str(_trigger_reason),
                            "duration_sec": int(_trig_elapsed),
                            "mfe": float(trade_data.get('mfe', 0.0)),
                            "mae": float(trade_data.get('mae', 0.0)),
                            "tp_reached": list(trade_data.get('tp_reached', [])),
                            "exit_order_ok": True,
                            "exit_order_id": "",
                            "exit_order_error": "",
                        })

                    # Cooldown (SL trigger = full cooldown, TP trigger = half)
                    try:
                        if "SL" in _trigger_reason:
                            self._sl_cooldown_map[symbol] = time.time()
                        else:
                            self._sl_cooldown_map[symbol] = time.time() - (float(self._sl_cooldown_sec) / 2.0)
                    except Exception:
                        pass

                    # Invalidate balance + readiness cache so compound sizing and dashboard see updated EUR balance
                    try:
                        self._kraken_balance_cache = None
                        self._kraken_balance_cache_ts = 0.0
                        self._kraken_last_readiness_ts = 0.0
                    except Exception:
                        pass

                    completed_trades.append(symbol)
                    _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                    print(f"   TRIGGER CLOSED {_ccy}{_trigger_pnl:+.2f} ({_trigger_reason})")
                    continue

                # Minimum duration (30 seconds) affects ONLY closing, not monitoring
                time_elapsed = (datetime.now() - entry_time).total_seconds() if entry_time else 0
                
                # Get current price
                current_price = self.get_current_price(symbol)
                
                if current_price:
                    # Calculate current P&L using real amount (units of base asset)
                    amt = float(trade_data.get('amount', 0.0) or 0.0)
                    if amt <= 0:
                        # Fallback: derive amount from USD size and entry price
                        _fb_usd = float(trade_data.get('usd_size', 0.0) or 0.0) or float(self._kraken_position_usd)
                        _fb_entry = float(signal.entry_price) if signal.entry_price > 0 else 1.0
                        amt = _fb_usd / _fb_entry
                    if signal.action == 'BUY':
                        current_pnl = (current_price - signal.entry_price) * amt
                    else:
                        current_pnl = (signal.entry_price - current_price) * amt

                    # Subtract estimated fees from unrealized PnL (like Freqtrade does)
                    try:
                        usd_size = float(trade_data.get("usd_size", 0.0) or 0.0)
                        current_pnl -= self._estimate_fees(usd_size)
                    except Exception:
                        pass
                    # Always update running P&L for dashboard
                    self.active_signals[symbol]['pnl'] = current_pnl
                    
                    # Track excursions (verification / audit)
                    prev_mfe = float(trade_data.get('mfe', 0.0))
                    prev_mae = float(trade_data.get('mae', 0.0))
                    trade_data['mfe'] = max(prev_mfe, current_pnl)
                    trade_data['mae'] = min(prev_mae, current_pnl)

                    # Track which TP levels were reached at any time
                    tp_reached = trade_data.get('tp_reached')
                    if not isinstance(tp_reached, list):
                        tp_reached = []
                        trade_data['tp_reached'] = tp_reached
                    try:
                        if signal.action == 'BUY':
                            for i, tp in enumerate(signal.targets[:4], 1):
                                if current_price >= tp and f"TP{i}" not in tp_reached:
                                    tp_reached.append(f"TP{i}")
                        else:
                            for i, tp in enumerate(signal.targets[:4], 1):
                                if current_price <= tp and f"TP{i}" not in tp_reached:
                                    tp_reached.append(f"TP{i}")
                    except Exception:
                        pass

                    # Trailing stop: when TP levels hit, move SL up + update on exchange
                    try:
                        _is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
                        if "TP1" in tp_reached and not trade_data.get("_sl_moved_be"):
                            # ATR-based dynamic trailing stop at TP1:
                            # With tighter TP targets (1.5-2.5x ATR), lock MORE profit at TP1.
                            # Low vol → 50% lock (guarantee net-positive after fees)
                            # Normal vol → 60% lock (lock >1x ATR of profit)
                            # High vol (>2%) → 70% lock (volatile = sharp reversals, lock fast)
                            _entry_px = float(signal.entry_price)
                            _tp1_px = float(signal.targets[0]) if signal.targets else _entry_px
                            _trade_atr_pct = float(trade_data.get("atr_pct", 0.5) or 0.5)
                            if _trade_atr_pct < 0.3:
                                _lock_pct = 0.50
                            elif _trade_atr_pct > 2.0:
                                _lock_pct = 0.70
                            else:
                                _lock_pct = 0.60
                            new_sl = _entry_px + (_tp1_px - _entry_px) * _lock_pct if signal.action == 'BUY' else _entry_px - (_entry_px - _tp1_px) * _lock_pct
                            # Fee-aware floor: ensure locked profit > 2× round-trip fees
                            # Without this, low-vol TP1 lock can be net-negative after fees
                            try:
                                _usd_sz = float(trade_data.get("usd_size", 10.0) or 10.0)
                                _min_lock_usd = self._estimate_fees(_usd_sz) * 2.5
                                _amt = float(trade_data.get("amount", 0.0) or 0.0)
                                if _amt > 0 and _entry_px > 0:
                                    _locked_usd = abs(new_sl - _entry_px) * _amt
                                    if _locked_usd < _min_lock_usd:
                                        _min_sl_dist = _min_lock_usd / _amt
                                        _fee_floor_sl = (_entry_px + _min_sl_dist) if signal.action == 'BUY' else (_entry_px - _min_sl_dist)
                                        # Only use floor if it's between entry and TP1 (don't overshoot)
                                        if signal.action == 'BUY' and _entry_px < _fee_floor_sl < _tp1_px:
                                            new_sl = _fee_floor_sl
                                            _lock_pct = abs(new_sl - _entry_px) / max(1e-12, abs(_tp1_px - _entry_px))
                                        elif signal.action == 'SELL' and _tp1_px < _fee_floor_sl < _entry_px:
                                            new_sl = _fee_floor_sl
                                            _lock_pct = abs(_entry_px - new_sl) / max(1e-12, abs(_entry_px - _tp1_px))
                            except Exception:
                                pass
                            signal.stop_loss = new_sl
                            trade_data["_sl_moved_be"] = True
                            logger.info("Trailing stop: %s SL moved to %.0f%%-TP1 @ %.6f (entry=%.6f, TP1=%.6f, atr=%.2f%%)", symbol, _lock_pct*100, new_sl, _entry_px, _tp1_px, _trade_atr_pct)
                            if _is_live and trade_data.get("sl_order_id"):
                                self._update_sl_trigger(symbol, trade_data, new_sl)
                            # TP1 hit by bot — exchange trigger is at TP2, so DON'T cancel it.
                            print(f"   📈 {symbol}: TP1 hit — SL locked at {_lock_pct*100:.0f}% of TP1 (ATR={_trade_atr_pct:.2f}%), TP2 trigger active", flush=True)
                        if "TP2" in tp_reached and not trade_data.get("_sl_moved_tp1"):
                            if len(signal.targets) > 0:
                                new_sl = float(signal.targets[0])
                                signal.stop_loss = new_sl
                                trade_data["_sl_moved_tp1"] = True
                                logger.info("Trailing stop: %s SL moved to TP1 @ %.6f", symbol, new_sl)
                                if _is_live and trade_data.get("sl_order_id"):
                                    self._update_sl_trigger(symbol, trade_data, new_sl)
                                # TP2 hit — exchange trigger was at TP2, check if it already filled
                                if _is_live and trade_data.get("tp_order_id"):
                                    pair = self._kraken_supported_pair(symbol)
                                    if pair:
                                        _tp2_filled = False
                                        try:
                                            _tp2_st = self._fetch_trigger_order_safe(str(trade_data["tp_order_id"]), pair)
                                            if isinstance(_tp2_st, dict) and str(_tp2_st.get("status", "")).lower() in {"closed", "filled"}:
                                                _tp2_filled = True
                                                print(f"   ✅ {symbol}: TP2 trigger FILLED on exchange — tokens sold", flush=True)
                                        except Exception:
                                            pass
                                        if _tp2_filled:
                                            trade_data["tp_order_id"] = None
                                            _rem_sl = trade_data.get("sl_order_id")
                                            if _rem_sl:
                                                self._cancel_trigger_order(str(_rem_sl), pair)
                                                trade_data["sl_order_id"] = None
                                            trade_data["_trigger_sold_on_exchange"] = True
                                        else:
                                            # TP2 passed but trigger not yet filled — upgrade to TP3
                                            if len(signal.targets) > 2:
                                                try:
                                                    self._cancel_trigger_order(str(trade_data["tp_order_id"]), pair)
                                                    trade_data["tp_order_id"] = None
                                                    tp3 = float(signal.targets[2])
                                                    amt = float(trade_data.get("amount", 0) or 0)
                                                    if amt > 0:
                                                        tpsl3 = self._place_tp_sl_triggers(symbol, signal.action, amt, tp3, new_sl)
                                                        trade_data["tp_order_id"] = tpsl3.get("tp_order_id")
                                                        if tpsl3.get("sl_order_id"):
                                                            self._cancel_trigger_order(str(tpsl3["sl_order_id"]), pair)
                                                        print(f"   🚀 {symbol}: TP3 trigger placed @ {tp3:.6f}", flush=True)
                                                except Exception:
                                                    pass
                                print(f"   📈 {symbol}: TP2 hit — SL locked at TP1", flush=True)
                        if "TP3" in tp_reached and not trade_data.get("_sl_moved_tp2"):
                            if len(signal.targets) > 1:
                                new_sl = float(signal.targets[1])
                                signal.stop_loss = new_sl
                                trade_data["_sl_moved_tp2"] = True
                                logger.info("Trailing stop: %s SL moved to TP2 @ %.6f", symbol, new_sl)
                                if _is_live and trade_data.get("sl_order_id"):
                                    self._update_sl_trigger(symbol, trade_data, new_sl)
                                # Check if TP3 trigger filled, if not upgrade to TP4
                                if _is_live and trade_data.get("tp_order_id"):
                                    pair = self._kraken_supported_pair(symbol)
                                    if pair:
                                        _tp3_filled = False
                                        try:
                                            _tp3_st = self._fetch_trigger_order_safe(str(trade_data["tp_order_id"]), pair)
                                            if isinstance(_tp3_st, dict) and str(_tp3_st.get("status", "")).lower() in {"closed", "filled"}:
                                                _tp3_filled = True
                                                print(f"   ✅ {symbol}: TP3 trigger FILLED on exchange", flush=True)
                                        except Exception:
                                            pass
                                        if _tp3_filled:
                                            trade_data["tp_order_id"] = None
                                            _rem_sl = trade_data.get("sl_order_id")
                                            if _rem_sl:
                                                self._cancel_trigger_order(str(_rem_sl), pair)
                                                trade_data["sl_order_id"] = None
                                            trade_data["_trigger_sold_on_exchange"] = True
                                        elif len(signal.targets) > 3:
                                            try:
                                                self._cancel_trigger_order(str(trade_data["tp_order_id"]), pair)
                                                trade_data["tp_order_id"] = None
                                                tp4 = float(signal.targets[3])
                                                amt = float(trade_data.get("amount", 0) or 0)
                                                if amt > 0:
                                                    tpsl4 = self._place_tp_sl_triggers(symbol, signal.action, amt, tp4, new_sl)
                                                    trade_data["tp_order_id"] = tpsl4.get("tp_order_id")
                                                    if tpsl4.get("sl_order_id"):
                                                        self._cancel_trigger_order(str(tpsl4["sl_order_id"]), pair)
                                                    print(f"   🚀 {symbol}: TP4 trigger placed @ {tp4:.6f}", flush=True)
                                            except Exception:
                                                pass
                                print(f"   📈 {symbol}: TP3 hit — SL locked at TP2", flush=True)
                    except Exception:
                        pass

                    # Add audit snapshot keyed by signal_id (non-blocking to avoid stalling trade management)
                    try:
                        _got = self._data_lock.acquire(timeout=0.05)
                        if _got:
                            try:
                                self._verification_audit.append({
                                    "ts": int(datetime.now().timestamp()),
                                    "id": int(getattr(signal, 'signal_id', 0)),
                                    "symbol": symbol,
                                    "side": signal.action,
                                    "price": float(current_price),
                                    "pnl": float(current_pnl),
                                    "mfe": float(trade_data.get('mfe', 0.0)),
                                    "mae": float(trade_data.get('mae', 0.0)),
                                    "tp_reached": list(trade_data.get('tp_reached', [])),
                                })
                            finally:
                                self._data_lock.release()
                    except Exception:
                        pass
                    
                    _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                    print(f"   {symbol}: {_ccy}{current_price:.4f} P&L: {_ccy}{current_pnl:+.2f}", end=" ")
                    
                    # Don't close too early
                    if time_elapsed < 30:
                        print(f"TOO EARLY")
                        continue

                    should_exit = False
                    pnl = 0.0
                    reason = ""

                    # Trade timeout warning: alert at 75% of max duration (Freqtrade pattern)
                    # Gives trader time to manually intervene before forced MAX_TIME exit
                    try:
                        _warn_threshold = float(self._max_trade_sec) * 0.75
                        if time_elapsed >= _warn_threshold and not trade_data.get("_timeout_warned"):
                            trade_data["_timeout_warned"] = True
                            _remain_min = int((float(self._max_trade_sec) - time_elapsed) / 60)
                            print(f"   ⚠️ {symbol}: approaching max duration — {_remain_min}min remaining (PnL: {_ccy}{current_pnl:+.2f})", flush=True)
                            if self._telegram_enabled():
                                _warn_msg = f"⏰ #{getattr(signal, 'signal_id', 0)} {symbol} {signal.action}\n\nApproaching max duration\n{_remain_min}min remaining\nPnL: {_ccy}{current_pnl:+.2f}"
                                threading.Thread(target=self._send_telegram_message_sync, args=(_warn_msg,), daemon=True).start()
                    except Exception:
                        pass

                    # Max-time exit: force close if trade exceeds max duration (prevents capital lock)
                    if time_elapsed >= self._max_trade_sec:
                        if signal.action == 'BUY':
                            pnl = (current_price - signal.entry_price) * amt
                        else:
                            pnl = (signal.entry_price - current_price) * amt
                        reason = "MAX_TIME"
                        should_exit = True

                    # ROI-based exit (Freqtrade minimal_roi pattern):
                    # Accept smaller profits as trade ages. Lock in gains before they reverse.
                    # Table: {minutes: min_profit_pct} — e.g., after 30min accept 1.5% profit
                    if not should_exit and signal.entry_price > 0 and amt > 0:
                        try:
                            if signal.action == 'BUY':
                                _roi_pct = ((current_price - signal.entry_price) / signal.entry_price) * 100.0
                            else:
                                _roi_pct = ((signal.entry_price - current_price) / signal.entry_price) * 100.0
                            _elapsed_min = time_elapsed / 60.0
                            # ROI table: take profit if trade stalls at moderate gain.
                            # REALISTIC: old table (8%/5%/3.5%/2.5%) was NEVER reached on EUR spot.
                            # Result: 100% of trades went to MAX_TIME → small loss.
                            # NEW: accept smaller profits earlier. On €64 trade:
                            #   3% instant = €1.92 (spike), 1.5% @1h = €0.96, 1.0% @2h = €0.64
                            #   0.7% @4h = €0.45 (still > €0.13 fees). Better than MAX_TIME loss!
                            _roi_table = [(0, 3.0), (30, 2.0), (60, 1.5), (120, 1.0), (240, 0.7)]
                            for _min_t, _min_pct in reversed(_roi_table):
                                if _elapsed_min >= _min_t and _roi_pct >= _min_pct:
                                    _roi_pnl = (current_price - signal.entry_price) * amt if signal.action == 'BUY' else (signal.entry_price - current_price) * amt
                                    # Fee-aware guard: don't close ROI if profit < 2× round-trip fees
                                    _usd_sz = float(trade_data.get("usd_size", 10.0) or 10.0)
                                    _min_profit = self._estimate_fees(_usd_sz) * 2.0
                                    if _roi_pnl >= _min_profit:
                                        pnl = _roi_pnl
                                        reason = f"ROI({_roi_pct:.1f}%@{int(_elapsed_min)}m)"
                                        should_exit = True
                                    break
                        except Exception:
                            pass

                    if not should_exit:
                        # Check exit conditions
                        should_exit, pnl, reason = self.check_exit_conditions(signal, current_price, trade_data['position_size'], amount=amt)
                    
                    if should_exit:
                        # Cancel any outstanding TP/SL trigger orders on exchange before placing exit
                        try:
                            _exit_pair = self._kraken_supported_pair(symbol)
                            if _exit_pair:
                                for _trig_key in ("tp_order_id", "sl_order_id"):
                                    _trig_id = trade_data.get(_trig_key)
                                    if _trig_id:
                                        self._cancel_trigger_order(str(_trig_id), _exit_pair)
                                        trade_data[_trig_key] = None
                        except Exception:
                            pass

                        # If trailing stop already detected that trigger sold tokens, skip exit order
                        _already_sold = bool(trade_data.get("_trigger_sold_on_exchange"))

                        # Place exit order on exchange (sell what we bought, buy back what we sold)
                        exit_order_meta = {}
                        if _already_sold:
                            print(f"   ℹ️ {symbol}: trigger already sold on exchange — skipping exit order", flush=True)
                            exit_order_meta = {"ok": True, "mode": "trigger_already_sold", "order_id": "",
                                               "exit_note": "exchange trigger executed before bot exit"}
                        if not _already_sold:
                          try:
                            is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
                            if is_live and amt > 0:
                                exit_side = "SELL" if signal.action == "BUY" else "BUY"
                                pair = self._kraken_supported_pair(symbol)
                                if pair and self._kraken:
                                    # Exit with exact amount via market order (guaranteed fill, no amount recalculation drift)
                                    rounded_amt = self._kraken_round_amount(pair, float(amt))
                                    if rounded_amt <= 0:
                                        rounded_amt = float(amt)
                                    # Min notional guard: if exit value < exchange minimum cost, skip order
                                    # (tokens worth < $1 can't be sold — accept dust loss)
                                    _skip_exit_order = False
                                    _exit_notional = float(rounded_amt) * float(current_price)
                                    try:
                                        _m = getattr(self._kraken, "markets", {}).get(pair) or {}
                                        _cost_min = float(((_m.get("limits") or {}).get("cost") or {}).get("min") or 0)
                                        if _cost_min > 0 and _exit_notional < _cost_min:
                                            exit_order_meta = {"ok": False, "exit_error": f"exit_below_min_notional:{_exit_notional:.4f}<{_cost_min}"}
                                            logger.warning("Exit skipped for %s: notional $%.4f < min $%.2f (dust)", symbol, _exit_notional, _cost_min)
                                            _skip_exit_order = True
                                    except Exception:
                                        pass
                                    if not _skip_exit_order:
                                        try:
                                            _exit_tout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
                                            _exit_box = [None]
                                            def _do_exit_order():
                                                try:
                                                    _exit_box[0] = self._kraken.create_order(pair, "market", exit_side.lower(), float(rounded_amt))
                                                except Exception as _eex:
                                                    _exit_box[0] = _eex
                                            _exit_t = threading.Thread(target=_do_exit_order, daemon=True)
                                            _exit_t.start()
                                            _exit_t.join(timeout=min(10.0, max(3.0, _exit_tout)))
                                            if _exit_t.is_alive():
                                                self._ccxt_timeout_failures = int(getattr(self, "_ccxt_timeout_failures", 0) or 0) + 1
                                                raise TimeoutError("exit order timeout")
                                            if isinstance(_exit_box[0], Exception):
                                                raise _exit_box[0]
                                            mkt = _exit_box[0]
                                            mkt_id = mkt.get("id") if isinstance(mkt, dict) else None
                                            exit_order_meta = {"ok": True, "mode": "market_exit", "order_id": mkt_id}
                                            # Verify exit fill (Freqtrade emergency_exit pattern)
                                            try:
                                                if mkt_id:
                                                    _est = self._kraken_fetch_order_safe(str(mkt_id), pair)
                                                    if isinstance(_est, dict):
                                                        _ef = _est.get("filled")
                                                        if _ef is not None and float(_ef) <= 0:
                                                            exit_order_meta["ok"] = False
                                                            exit_order_meta["exit_error"] = "market_exit_not_filled"
                                                            logger.warning("Exit market order accepted but filled=0 for %s", symbol)
                                            except Exception:
                                                pass
                                        except Exception as e_mkt:
                                            _mkt_err = str(e_mkt)
                                            _is_insuf = ("51008" in _mkt_err or "insufficient" in _mkt_err.lower())
                                            if _is_insuf:
                                                # Amount mismatch: trigger may have partially filled, or fees reduced balance.
                                                # Read REAL balance and retry sell with actual available amount.
                                                logger.warning("Exit market insufficient for %s (tried %.6f). Checking real balance...", symbol, float(rounded_amt))
                                                try:
                                                    _base_ccy = pair.split("/")[0] if "/" in pair else ""
                                                    _real_bal = self._kraken_free_balance(_base_ccy) if _base_ccy else 0.0
                                                    _real_rounded = self._kraken_round_amount(pair, _real_bal) if _real_bal > 0 else 0.0
                                                    _real_notional = _real_rounded * float(current_price) if _real_rounded > 0 else 0.0
                                                    _min_cost = 0.0
                                                    try:
                                                        _mm = getattr(self._kraken, "markets", {}).get(pair) or {}
                                                        _min_cost = float(((_mm.get("limits") or {}).get("cost") or {}).get("min") or 0)
                                                    except Exception:
                                                        pass
                                                    if _real_rounded > 0 and (_min_cost <= 0 or _real_notional >= _min_cost):
                                                        # Retry sell with real balance
                                                        logger.info("Retrying exit for %s with real balance: %.6f (was %.6f)", symbol, _real_rounded, float(rounded_amt))
                                                        print(f"   🔄 Retrying sell {symbol}: {_real_rounded:.6f} (real balance, was {float(rounded_amt):.6f})", flush=True)
                                                        _retry_box = [None]
                                                        def _do_retry_exit():
                                                            try:
                                                                _retry_box[0] = self._kraken.create_order(pair, "market", exit_side.lower(), float(_real_rounded))
                                                            except Exception:
                                                                pass
                                                        _retry_t = threading.Thread(target=_do_retry_exit, daemon=True)
                                                        _retry_t.start()
                                                        _retry_t.join(timeout=min(10.0, max(3.0, _exit_tout)))
                                                        _retry_mkt = _retry_box[0]
                                                        _retry_id = _retry_mkt.get("id") if isinstance(_retry_mkt, dict) and _retry_mkt else None
                                                        exit_order_meta = {"ok": True, "mode": "market_exit_retry", "order_id": _retry_id,
                                                                           "exit_note": f"retried with real balance {_real_rounded:.6f}"}
                                                    else:
                                                        # Real balance is zero or dust — trigger truly sold everything
                                                        logger.info("Exit: %s real balance=%.8f (dust/zero) — trigger sold tokens on exchange.", symbol, _real_bal)
                                                        print(f"   ℹ️ {symbol}: balance={_real_bal:.8f} (dust) — trigger sold on exchange", flush=True)
                                                        exit_order_meta = {"ok": True, "mode": "trigger_already_sold", "order_id": "",
                                                                           "exit_note": "real balance zero/dust after insufficient error"}
                                                except Exception as e_retry:
                                                    logger.error("Exit retry with real balance failed for %s: %s", symbol, e_retry)
                                                    exit_order_meta = {"ok": False, "exit_error": f"insufficient_retry_failed:{e_retry}"}
                                            else:
                                                logger.warning("Exit market order failed for %s, trying limit: %s", symbol, _mkt_err)
                                                # Fallback to limit order with exact amount
                                                try:
                                                    exit_order_meta = self._kraken_place_order_limit_timeout(symbol, exit_side, float(rounded_amt) * current_price)
                                                except Exception as e_lmt:
                                                    exit_order_meta = {"ok": False, "exit_error": str(e_lmt)}
                                                    logger.error("Exit limit order also failed for %s: %s", symbol, e_lmt)
                          except Exception as e_exit:
                            logger.error("Exit order failed for %s: %s", symbol, e_exit)
                            exit_order_meta["exit_error"] = str(e_exit)

                        # Deduct estimated round-trip fees from PnL (top bots always do this)
                        try:
                            usd_size = float(trade_data.get("usd_size", 0.0) or 0.0)
                            fees = self._estimate_fees(usd_size)
                            pnl -= fees
                        except Exception:
                            pass

                        # Close trade
                        self.active_signals[symbol]['status'] = 'CLOSED'
                        self.active_signals[symbol]['pnl'] = pnl
                        
                        # Update P&L
                        self.daily_pnl += pnl
                        self.total_pnl += pnl
                        self.total_trades += 1
                        
                        result = "WIN" if pnl > 0 else "LOSS"
                        if pnl > 0:
                            self.winning_trades += 1
                            self._consecutive_losses = 0
                            self._consecutive_wins += 1
                            await self.send_telegram_notification(symbol, signal.action, pnl, reason, "WIN")
                        else:
                            self._consecutive_losses += 1
                            self._consecutive_wins = 0
                            await self.send_telegram_notification(symbol, signal.action, pnl, reason, "LOSS")

                        # Compute pips for audit/dashboard
                        try:
                            pip_size = self._pip_size_for_symbol(symbol, float(signal.entry_price or 0.0))
                            if pip_size <= 0:
                                pip_size = 1.0
                            if signal.action == 'BUY':
                                signed_move = float(current_price) - float(signal.entry_price)
                            else:
                                signed_move = float(signal.entry_price) - float(current_price)
                            pips_value = signed_move / float(pip_size)
                        except Exception:
                            pips_value = 0.0

                        with self._data_lock:
                            self._trade_history.append({
                                "id": int(signal.signal_id),
                                "ts": int(datetime.now().timestamp()),
                                "symbol": symbol,
                                "result": result,
                                "pnl": float(pnl),
                                "exit": float(current_price),
                                "pips": float(pips_value),
                                "reason": str(reason),
                                "duration_sec": int(time_elapsed),
                                "mfe": float(trade_data.get('mfe', 0.0)),
                                "mae": float(trade_data.get('mae', 0.0)),
                                "tp_reached": list(trade_data.get('tp_reached', [])),
                                "exit_order_ok": bool(exit_order_meta.get("ok", False)),
                                "exit_order_id": str(exit_order_meta.get("order_id", "") or ""),
                                "exit_order_error": str(exit_order_meta.get("error", "") or exit_order_meta.get("exit_error", "") or ""),
                            })
                        
                        # Record symbol cooldown after ANY exit to prevent whipsaw re-entry
                        # SL exits get full cooldown, other exits get half cooldown (Freqtrade pattern)
                        try:
                            if reason == "SL":
                                self._sl_cooldown_map[symbol] = time.time()
                            else:
                                # Half cooldown for TP/ROI/MAX_TIME exits — still prevents immediate re-entry
                                self._sl_cooldown_map[symbol] = time.time() - (float(self._sl_cooldown_sec) / 2.0)
                        except Exception:
                            pass

                        # Invalidate balance + readiness cache so next compound sizing and dashboard see updated EUR balance
                        try:
                            self._kraken_balance_cache = None
                            self._kraken_balance_cache_ts = 0.0
                            self._kraken_last_readiness_ts = 0.0
                        except Exception:
                            pass

                        completed_trades.append(symbol)
                        
                        _exit_ok = "✓" if exit_order_meta.get("ok") else "✗"
                        _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                        print(f"CLOSED {_ccy}{pnl:+.2f} ({reason}) exit_order={_exit_ok}")
                        print(f"TRADE COMPLETED: {symbol}")
                        print(f"   P&L: {_ccy}{pnl:+.2f} | Amount: {amt:.4f}")
                        print(f"   Win Rate: {self.get_win_rate():.1f}%")
                    else:
                        print(f"ACTIVE")
                else:
                    print(f"   {symbol}: Price fetch failed")
                    
            except Exception as e:
                logger.error(f"Error managing trade {symbol}: {e}")
                continue
            finally:
                _trade_elapsed = time.time() - _trade_t0
                if _trade_elapsed > 5.0:
                    print(f"   ⚠️ [{symbol}] manage took {_trade_elapsed:.1f}s", flush=True)
        
        _manage_elapsed = time.time() - _manage_t0
        if _manage_elapsed > 3.0:
            print(f"   ⏱️ manage_active_trades total: {_manage_elapsed:.1f}s", flush=True)
        
        # Remove completed trades
        for symbol in completed_trades:
            try:
                del self.active_signals[symbol]
            except KeyError:
                logger.debug("completed_trades: symbol %s already removed", symbol)
    
    def execute_trade(self, signal: QuantumTradingSignal) -> bool:
        """Execute a trading signal - WORKING VERSION"""
        try:
            print(f"   ⚙️ [{signal.symbol}] execute_trade() started", flush=True)
            _et0 = time.time()
            # Allocate sequential ID at execution time to avoid gaps after filtering
            try:
                if int(getattr(signal, 'signal_id', 0) or 0) <= 0:
                    signal.signal_id = int(self._next_signal_id)
                    self._next_signal_id += 1
                    self._save_next_signal_id()
                    print(f"   ⚙️ [{signal.symbol}] signal ID={signal.signal_id} allocated", flush=True)
            except Exception as _id_err:
                print(f"   ⚠️ [{signal.symbol}] signal ID alloc failed: {_id_err}", flush=True)

            position_size = PositionSize(
                position_size_percent=5.0,
                risk_amount=100.0,
                max_loss=50.0
            )

            order_meta: Dict[str, object] = {"mode": "sim"}
            executed_price = float(signal.entry_price)

            is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
            wants_orders = bool(self._kraken_trading_enabled)
            _exec_t0 = time.time()
            print(f"   ⚙️ [{signal.symbol}] pre-drift setup done ({time.time()-_et0:.1f}s)", flush=True)

            # Stale-price guard: compare signal entry vs live bid/ask (not just last).
            # For BUY: compare vs ask (what we'd actually pay). For SELL: compare vs bid.
            print(f"   🔍 [{signal.symbol}] Drift guard check...", flush=True)
            if wants_orders and signal.entry_price > 0:
                try:
                    pair = self._kraken_supported_pair(signal.symbol)
                    live_px = None
                    if pair:
                        try:
                            # Always fetch fresh ticker for drift guard — cached only has 'last',
                            # but we need ask (BUY) or bid (SELL) for accurate drift calculation.
                            if True:
                                _drift_ticker_box = [None]
                                def _do_drift_ticker():
                                    try:
                                        _drift_ticker_box[0] = self._kraken.fetch_ticker(pair)
                                    except Exception:
                                        pass
                                _dt = threading.Thread(target=_do_drift_ticker, daemon=True)
                                _dt.start()
                                _dt.join(timeout=6.0)
                                ticker = _drift_ticker_box[0]
                                if ticker and isinstance(ticker, dict):
                                    if signal.action == "BUY":
                                        live_px = float(ticker.get("ask") or ticker.get("last") or 0)
                                    else:
                                        live_px = float(ticker.get("bid") or ticker.get("last") or 0)
                        except Exception:
                            # Fallback: use cached price from signal scan (better than blocking)
                            live_px = None
                    if live_px is not None and live_px > 0:
                        drift_pct = abs(live_px - signal.entry_price) / signal.entry_price * 100.0
                        max_drift = float(getattr(self, "_kraken_max_entry_drift_pct", 2.0) or 2.0)
                        # Hard reject only if drift is extreme (>5%) — signal is truly stale
                        _hard_reject_pct = max(max_drift * 3.0, 5.0)
                        if drift_pct > _hard_reject_pct:
                            with self._data_lock:
                                self._signal_history.append({
                                    "id": int(getattr(signal, "signal_id", 0) or 0),
                                    "ts": int(datetime.now().timestamp()),
                                    "symbol": signal.symbol,
                                    "side": signal.action,
                                    "entry": float(signal.entry_price),
                                    "tp": [float(x) for x in signal.targets[:4]],
                                    "sl": float(signal.stop_loss),
                                    "conf": float(signal.confidence),
                                    "reason": str(signal.reasoning),
                                    "order_mode": "live" if is_live else "paper",
                                    "order_id": "",
                                    "order_status": "rejected",
                                    "order_error": f"stale_price:drift={drift_pct:.2f}%>{_hard_reject_pct:.1f}%(live={live_px:.6f},sig={signal.entry_price:.6f})",
                                })
                            logger.warning("Signal rejected (extreme drift %.2f%%): %s %s", drift_pct, signal.symbol, signal.action)
                            print(f"   ❌ Rejected: extreme price drift {drift_pct:.1f}% (live={live_px:.6f} vs sig={signal.entry_price:.6f})", flush=True)
                            return False
                        elif drift_pct > max_drift:
                            # Moderate drift: adjust entry price + shift TP/SL proportionally
                            old_entry = float(signal.entry_price)
                            ratio = live_px / old_entry if old_entry > 0 else 1.0
                            signal.entry_price = live_px
                            signal.stop_loss = float(signal.stop_loss) * ratio
                            signal.targets = [float(t) * ratio for t in signal.targets]
                            executed_price = live_px
                            logger.info("Drift adjust %s: entry %.6f→%.6f (%.2f%%), TP/SL shifted proportionally",
                                        signal.symbol, old_entry, live_px, drift_pct)
                            print(f"   🔄 Drift adjusted: entry {old_entry:.6f}→{live_px:.6f} ({drift_pct:.1f}%), TP/SL shifted", flush=True)
                except Exception:
                    pass

            print(f"   🔍 [{signal.symbol}] Drift guard done ({time.time()-_exec_t0:.1f}s)", flush=True)
            # Duplicate signal prevention: never open two trades on the same symbol (top bots always check this)
            if signal.symbol in self.active_signals:
                logger.info("Duplicate signal rejected: %s already has an active trade", signal.symbol)
                return False

            # Consecutive loss streak protection: pause trading, then auto-resume after cooldown
            if self._max_consecutive_losses > 0 and self._consecutive_losses >= self._max_consecutive_losses:
                _cd = float(getattr(self, "_loss_streak_cooldown_sec", 1800) or 1800)
                _paused_ts = float(getattr(self, "_loss_streak_paused_ts", 0) or 0)
                _now = time.time()
                if _paused_ts <= 0:
                    self._loss_streak_paused_ts = _now
                    logger.warning("Loss streak %d/%d — pausing for %ds", self._consecutive_losses, self._max_consecutive_losses, int(_cd))
                    print(f"   ⛔ LOSS STREAK: {self._consecutive_losses} losses — pausing {int(_cd/60)}min")
                    return False
                _elapsed = _now - _paused_ts
                if _elapsed < _cd:
                    _remain = int(_cd - _elapsed)
                    print(f"   ⏸️ LOSS STREAK COOLDOWN: {_remain}s remaining")
                    return False
                # Cooldown expired — reset and resume trading
                logger.info("Loss streak cooldown expired (%.0fs). Resetting consecutive losses %d→0.", _elapsed, self._consecutive_losses)
                print(f"   ▶️ LOSS STREAK RESET after {int(_elapsed/60)}min cooldown — resuming trading")
                self._consecutive_losses = 0
                self._consecutive_wins = 0
                self._loss_streak_paused_ts = 0.0

            # Max drawdown protection: track session peak EQUITY and pause if drawdown exceeds threshold
            # EQUITY = free cash + value of active positions (not just free balance!)
            # Without this, buying SOL for €33 drops free from €45→€11 = false 75% "drawdown"
            try:
                if self._max_drawdown_pct > 0 and self._kraken_trading_enabled:
                    quote = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
                    _cur_bal = self._kraken_free_balance(quote) if self._kraken is not None else 0.0
                    # Add value of active positions to get total equity
                    try:
                        for _sym, _td in dict(self.active_signals).items():
                            _cur_bal += float(_td.get("usd_size", 0) or 0)
                    except Exception:
                        pass
                    # Add value of pending patient orders (funds locked on exchange)
                    try:
                        for _sym, _po in dict(self._pending_orders).items():
                            _po_amt = float(_po.get("amount", 0) or 0)
                            _po_px = float(_po.get("price", 0) or 0)
                            if _po_amt > 0 and _po_px > 0:
                                _cur_bal += _po_amt * _po_px
                    except Exception:
                        pass
                    if _cur_bal > 0:
                        if self._session_start_balance <= 0:
                            self._session_start_balance = _cur_bal
                        if _cur_bal > self._session_peak_balance:
                            self._session_peak_balance = _cur_bal
                            self._drawdown_paused = False  # new peak = reset pause
                        if self._session_peak_balance > 0:
                            _dd_pct = ((self._session_peak_balance - _cur_bal) / self._session_peak_balance) * 100.0
                            if _dd_pct >= self._max_drawdown_pct:
                                _dd_cd = 1800.0  # 30min cooldown
                                if not self._drawdown_paused:
                                    self._drawdown_paused = True
                                    self._drawdown_pause_ts = time.time()
                                    logger.warning("MAX DRAWDOWN: %.1f%% (peak=%.2f, now=%.2f) — pausing 30min", _dd_pct, self._session_peak_balance, _cur_bal)
                                    print(f"   ⛔ MAX DRAWDOWN: {_dd_pct:.1f}% from peak €{self._session_peak_balance:.2f} → €{_cur_bal:.2f} — pausing 30min")
                                    return False
                                _dd_elapsed = time.time() - self._drawdown_pause_ts
                                if _dd_elapsed < _dd_cd:
                                    print(f"   ⏸️ DRAWDOWN PAUSE: {int(_dd_cd - _dd_elapsed)}s remaining (DD={_dd_pct:.1f}%)")
                                    return False
                                # Cooldown expired — RESET peak to current equity to break the infinite loop.
                                # Without this, DD stays above threshold and re-triggers every cycle forever.
                                self._drawdown_paused = False
                                self._session_peak_balance = _cur_bal
                                logger.info("Drawdown cooldown expired — peak reset to %.2f, resuming (was DD=%.1f%%)", _cur_bal, _dd_pct)
                                print(f"   ▶️ DRAWDOWN RESET: peak €{_cur_bal:.2f} — resuming trading")
            except Exception:
                pass

            # Portfolio heat check: total USD exposure cap (top bots limit total risk)
            if self._max_portfolio_heat_usd > 0:
                current_heat = self._current_portfolio_heat()
                new_usd = float(self._kraken_effective_position_usd()) if self._kraken_trading_enabled else float(self._kraken_position_usd)
                if (current_heat + new_usd) > self._max_portfolio_heat_usd:
                    logger.warning("Portfolio heat limit: $%.2f + $%.2f > $%.2f max", current_heat, new_usd, self._max_portfolio_heat_usd)
                    _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                    print(f"   ⛔ PORTFOLIO HEAT: {_ccy}{current_heat:.2f} + {_ccy}{new_usd:.2f} exceeds {_ccy}{self._max_portfolio_heat_usd:.2f} limit")
                    return False

            # ── SPOT SELL SAFETY: verify token balance before placing sell order ──
            # On OKX spot, SELL means you sell tokens you already hold.
            # You CANNOT short sell on spot — you must own the base asset.
            if str(signal.action).upper() == "SELL" and wants_orders:
                _sell_allowed = False
                try:
                    # Extract base currency from symbol (handles both "PENGU/USDC" and "PENGUUSDC")
                    _sym_str = str(signal.symbol).strip().upper()
                    if "/" in _sym_str:
                        _base_ccy = _sym_str.split("/")[0].strip()
                    else:
                        _q = str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").strip().upper()
                        _base_ccy = _sym_str[:-len(_q)] if _q and _sym_str.endswith(_q) else _sym_str
                    # Check if we already have an active BUY trade on this symbol
                    # (the exit system will handle closing it — this is for standalone SELL signals)
                    if signal.symbol in self.active_signals:
                        # Already have active trade — duplicate guard above should catch this
                        pass
                    elif is_live:
                        # Live mode: fetch actual token balance from OKX
                        try:
                            if self._kraken is None:
                                self._init_kraken()
                            _bal = self._kraken.fetch_balance({"type": "trading"})
                            _free = float((_bal.get("free") or {}).get(_base_ccy, 0.0) or 0.0)
                            _min_sell_usd = float(self._kraken_position_usd) * 0.5  # need at least 50% of position size
                            _token_usd = _free * float(executed_price) if executed_price > 0 else 0.0
                            if _token_usd >= _min_sell_usd:
                                _sell_allowed = True
                                logger.info("SELL balance OK: %s %.6f units = $%.2f (min $%.2f)", _base_ccy, _free, _token_usd, _min_sell_usd)
                            else:
                                logger.warning("SELL rejected: %s balance $%.2f < min $%.2f", _base_ccy, _token_usd, _min_sell_usd)
                                _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                                print(f"   ⛔ SELL BLOCKED: {_base_ccy} balance {_ccy}{_token_usd:.2f} < min {_ccy}{_min_sell_usd:.2f}")
                        except Exception as e_bal:
                            logger.warning("SELL balance check failed: %s — %s", _base_ccy, e_bal)
                            print(f"   ⛔ SELL BLOCKED: balance check failed for {_base_ccy}")
                    else:
                        # Paper/sim mode: allow SELL if we have a simulated BUY position or just simulate
                        _sell_allowed = True
                except Exception:
                    pass
                if not _sell_allowed:
                    with self._data_lock:
                        self._signal_history.append({
                            "id": int(getattr(signal, "signal_id", 0) or 0),
                            "ts": int(datetime.now().timestamp()),
                            "symbol": signal.symbol,
                            "side": signal.action,
                            "entry": float(executed_price),
                            "tp": [float(x) for x in signal.targets[:4]],
                            "sl": float(signal.stop_loss),
                            "conf": float(signal.confidence),
                            "reason": str(signal.reasoning),
                            "order_mode": "live" if is_live else "paper",
                            "order_id": "",
                            "order_status": "rejected",
                            "order_error": "sell_no_token_balance",
                        })
                    return False

            if is_live:
                try:
                    reason = str(getattr(self, "_kraken_trading_blocked_reason", "") or "")
                except Exception:
                    reason = ""
                if reason and reason in {"no_usdt", "no_quote", "balance_unknown", "auth_failed"}:
                    with self._data_lock:
                        self._signal_history.append({
                            "id": int(getattr(signal, "signal_id", 0) or 0),
                            "ts": int(datetime.now().timestamp()),
                            "symbol": signal.symbol,
                            "side": signal.action,
                            "entry": float(executed_price),
                            "tp": [float(x) for x in signal.targets[:4]],
                            "sl": float(signal.stop_loss),
                            "conf": float(signal.confidence),
                            "reason": str(signal.reasoning),
                            "order_mode": "live",
                            "order_id": "",
                            "order_status": "blocked",
                            "order_error": f"trading_blocked:{reason}",
                        })
                    print(f"   LIVE TRADING BLOCKED: {reason}. No order placed.")
                    return False

            if wants_orders:
                if self._kraken is None:
                    self._init_kraken()
                side = "BUY" if str(signal.action).upper() == "BUY" else "SELL"

                if is_live:
                    print(f"   💶 [{signal.symbol}] Calculating position size... ({time.time()-_exec_t0:.1f}s)", flush=True)
                    usd_size = float(self._kraken_effective_position_usd())

                    # Patient Orders: place at mid-price and wait hours/days for fill
                    # Only for BUY entry orders (SELL exits still use immediate market orders)
                    _use_patient = bool(getattr(self, "_patient_orders_enabled", False)) and side.upper() == "BUY"
                    if _use_patient:
                        print(f"   🔄 [{signal.symbol}] Placing PATIENT {side} order (€{usd_size:.2f}) at mid-price... ({time.time()-_exec_t0:.1f}s)", flush=True)
                        order_meta = self._patient_place_order(signal.symbol, side, usd_size, signal)
                        order_meta["mode"] = "live"
                        # If order is pending (not instantly filled), don't activate trade yet
                        if order_meta.get("patient_pending"):
                            print(f"   📊 [{signal.symbol}] Patient order PENDING — will monitor each cycle until filled or expired ({time.time()-_exec_t0:.1f}s)", flush=True)
                            with self._data_lock:
                                self._signal_history.append({
                                    "id": int(signal.signal_id),
                                    "ts": int(datetime.now().timestamp()),
                                    "symbol": signal.symbol,
                                    "side": signal.action,
                                    "entry": float(executed_price),
                                    "tp": [float(x) for x in signal.targets[:4]],
                                    "sl": float(signal.stop_loss),
                                    "conf": float(signal.confidence),
                                    "reason": str(signal.reasoning),
                                    "order_mode": "live_patient",
                                    "order_id": str(order_meta.get("order_id", "") or ""),
                                    "order_status": "pending_patient",
                                    "order_error": "",
                                })
                            return True  # Order placed successfully, will be activated on fill
                        print(f"   📊 [{signal.symbol}] Patient order result: ok={order_meta.get('ok')} status={order_meta.get('status','')} ({time.time()-_exec_t0:.1f}s)", flush=True)
                    else:
                        print(f"   🔄 [{signal.symbol}] Placing live {side} order (€{usd_size:.2f})... ({time.time()-_exec_t0:.1f}s)", flush=True)
                        order_meta = self._kraken_place_order_limit_timeout(signal.symbol, side, usd_size)
                        order_meta["mode"] = "live"
                        print(f"   📊 [{signal.symbol}] Order result: ok={order_meta.get('ok')} status={order_meta.get('status','')} error={order_meta.get('error','')} ({time.time()-_exec_t0:.1f}s)", flush=True)
                else:
                    order_meta = self._kraken_build_order_plan(signal.symbol, side, float(self._kraken_position_usd))
                    order_meta["mode"] = "paper"

                    # In paper mode, if planning failed, don't open an internal trade.
                    if not bool(order_meta.get("ok")):
                        with self._data_lock:
                            self._signal_history.append({
                                "id": int(signal.signal_id),
                                "ts": int(datetime.now().timestamp()),
                                "symbol": signal.symbol,
                                "side": signal.action,
                                "entry": float(executed_price),
                                "tp": [float(x) for x in signal.targets[:4]],
                                "sl": float(signal.stop_loss),
                                "conf": float(signal.confidence),
                                "reason": str(signal.reasoning),
                                "order_mode": "paper",
                                "order_id": "",
                                "order_status": str(order_meta.get("status", "plan_failed")),
                                "order_error": str(order_meta.get("error", "")),
                            })
                        print(f"   PLAN FAILED (paper): {signal.symbol} {signal.action} | {order_meta.get('error','')}")
                        return False

                try:
                    if order_meta.get("ok"):
                        # Prefer average fill price from exchange (actual execution price)
                        # over our limit price (intended price). Handles slippage accurately.
                        _avg = order_meta.get("average") or order_meta.get("avg_price")
                        if _avg is not None and float(_avg) > 0:
                            executed_price = float(_avg)
                        elif order_meta.get("price") is not None:
                            executed_price = float(order_meta.get("price") or executed_price)
                        # CRITICAL: Recalculate TP targets and SL relative to actual fill price.
                        # If signal entry was 0.0760 but we filled at 0.0776 (+2.1% slippage),
                        # all TP/SL levels must shift by the same delta so trailing stops don't
                        # fire instantly (TP1 was 0.0766 < fill 0.0776 = already breached!).
                        _orig_entry = float(signal.entry_price)
                        if _orig_entry > 0 and executed_price > 0 and abs(executed_price - _orig_entry) / _orig_entry > 0.001:
                            _price_shift = executed_price - _orig_entry
                            signal.entry_price = executed_price
                            signal.targets = [float(t) + _price_shift for t in signal.targets]
                            signal.stop_loss = float(signal.stop_loss) + _price_shift
                            print(f"   🔄 [{signal.symbol}] Adjusted entry/TP/SL for fill slippage: shift={_price_shift:+.6f} ({_price_shift/_orig_entry*100:+.2f}%)", flush=True)
                except Exception:
                    pass

                # Strict live mode: if we failed to place a live order, don't open an internal trade.
                if is_live and self._kraken_strict_execution and not bool(order_meta.get("ok")):
                    with self._data_lock:
                        self._signal_history.append({
                            "id": int(signal.signal_id),
                            "ts": int(datetime.now().timestamp()),
                            "symbol": signal.symbol,
                            "side": signal.action,
                            "entry": float(executed_price),
                            "tp": [float(x) for x in signal.targets[:4]],
                            "sl": float(signal.stop_loss),
                            "conf": float(signal.confidence),
                            "reason": str(signal.reasoning),
                            "order_mode": str(order_meta.get("mode", "live")),
                            "order_id": str(order_meta.get("order_id", "")) if order_meta.get("order_id") is not None else "",
                            "order_status": str(order_meta.get("status", "rejected")),
                            "order_error": str(order_meta.get("error", "")),
                        })
                    print(f"   ORDER REJECTED (live strict): {signal.symbol} {signal.action} | {order_meta.get('error','')}")
                    return False
            else:
                order_meta["mode"] = "sim"
            
            # Add to active trades
            now_dt = datetime.now()
            # Compute real amount (units of base asset) for accurate PnL
            # Prefer filled amount from exchange (handles partial fills) over requested amount
            try:
                real_amount = 0.0
                try:
                    _filled = order_meta.get("filled")
                    if _filled is not None and float(_filled) > 0:
                        real_amount = float(_filled)
                except Exception:
                    pass
                if real_amount <= 0:
                    real_amount = float(order_meta.get("amount") or 0.0)
                if real_amount <= 0 and executed_price > 0:
                    try:
                        real_amount = float(self._kraken_effective_position_usd()) / executed_price
                    except Exception:
                        real_amount = float(self._kraken_position_usd) / executed_price
            except Exception:
                real_amount = 0.0
            # Derive ATR% from signal targets/SL for ATR-based trailing stop
            _trade_atr_pct = 0.5  # default normal vol
            try:
                if executed_price > 0 and signal.stop_loss > 0:
                    _sl_dist = abs(executed_price - float(signal.stop_loss))
                    _trade_atr_pct = (_sl_dist / executed_price) * 100.0 / 2.5  # rough: SL ~ 2.5x ATR
            except Exception:
                pass

            self.active_signals[signal.symbol] = {
                'signal': signal,
                'entry_time': now_dt,
                'position_size': position_size.position_size_percent,
                'amount': real_amount,
                'usd_size': float(real_amount * executed_price) if (real_amount > 0 and executed_price > 0) else float(self._kraken_effective_position_usd()),
                'status': 'ACTIVE',
                'pnl': 0.0,
                'mfe': 0.0,  # max favorable excursion (PnL)
                'mae': 0.0,  # max adverse excursion (PnL)
                'tp_reached': [],
                'order': dict(order_meta),
                'tp_order_id': None,
                'sl_order_id': None,
                'atr_pct': _trade_atr_pct,
            }

            # Invalidate balance + readiness cache after trade so compound sizing and dashboard see reduced balance
            try:
                self._kraken_balance_cache = None
                self._kraken_balance_cache_ts = 0.0
                self._kraken_last_readiness_ts = 0.0
            except Exception:
                pass

            # Place TP + SL trigger orders on exchange (execute even if bot goes offline)
            # KEY OPTIMIZATION: Place TP trigger at TP2 (not TP1) so the trade runs longer.
            # Bot's trailing stop handles TP1 (moves SL to lock profit). Exchange sells at TP2 = bigger win.
            # If only 1 target exists, use TP1 as fallback.
            if is_live and real_amount > 0 and order_meta.get("ok") and len(signal.targets) > 0:
                try:
                    _tp_trigger_price = float(signal.targets[1]) if len(signal.targets) > 1 else float(signal.targets[0])
                    sl = float(signal.stop_loss)
                    _tp_label = "TP2" if len(signal.targets) > 1 else "TP1"
                    print(f"   📋 [{signal.symbol}] Placing {_tp_label}/SL triggers ({_tp_label}={_tp_trigger_price:.6f} SL={sl:.6f})... ({time.time()-_exec_t0:.1f}s)", flush=True)
                    tpsl = self._place_tp_sl_triggers(signal.symbol, signal.action, real_amount, _tp_trigger_price, sl)
                    self.active_signals[signal.symbol]['tp_order_id'] = tpsl.get("tp_order_id")
                    self.active_signals[signal.symbol]['sl_order_id'] = tpsl.get("sl_order_id")
                    if tpsl.get("tp_order_id") and tpsl.get("sl_order_id"):
                        print(f"   ✅ TP/SL on exchange! TP={tpsl['tp_order_id']} SL={tpsl['sl_order_id']}", flush=True)
                    else:
                        _errs = []
                        if tpsl.get("tp_error"):
                            _errs.append(f"TP:{tpsl['tp_error']}")
                        if tpsl.get("sl_error"):
                            _errs.append(f"SL:{tpsl['sl_error']}")
                        print(f"   ⚠️ TP/SL partial: {', '.join(_errs)}", flush=True)
                except Exception as e:
                    logger.warning("Failed to place TP/SL triggers for %s: %s", signal.symbol, e)
                    print(f"   ⚠️ TP/SL trigger placement failed: {e}", flush=True)

            with self._data_lock:
                self._signal_history.append({
                    "id": int(signal.signal_id),
                    "ts": int(now_dt.timestamp()),
                    "symbol": signal.symbol,
                    "side": signal.action,
                    "entry": float(executed_price),
                    "tp": [float(x) for x in signal.targets[:4]],
                    "sl": float(signal.stop_loss),
                    "conf": float(signal.confidence),
                    "reason": str(signal.reasoning),
                    "order_mode": str(order_meta.get("mode", "sim")),
                    "order_id": str(order_meta.get("order_id", "")) if order_meta.get("order_id") is not None else "",
                    "order_status": str(order_meta.get("status", "")),
                    "order_error": str(order_meta.get("error", "")),
                })
            
            # NOTE: total_trades is incremented at trade CLOSE (exit handler), not here.
            # Counting at open would double-count every trade (Freqtrade pattern: count at close).
            
            print(f"   TRADE EXECUTED ({time.time()-_exec_t0:.1f}s total)", flush=True)
            try:
                om = str(order_meta.get("mode", "sim"))
                oid = str(order_meta.get("order_id", ""))
                ost = str(order_meta.get("status", ""))
                oerr = str(order_meta.get("error", "") or order_meta.get("order_error", ""))
                extra = f" error={oerr}" if (not ost and oerr) else ""
                _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                print(f"   #{signal.signal_id} {signal.action} {signal.symbol} @ {_ccy}{float(executed_price):.6f} | mode={om} id={oid} status={ost}{extra}")
            except Exception:
                _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                print(f"   #{signal.signal_id} {signal.action} {signal.symbol} @ {_ccy}{signal.entry_price:.4f}")
            _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
            print(f"   Size: {_ccy}{real_amount * executed_price:.2f} ({real_amount:.6f} units)")
            _pfmt = lambda v: f"{v:.8f}" if abs(v) < 0.01 else (f"{v:.6f}" if abs(v) < 1 else f"{v:.4f}")
            print(f"   Targets: {_ccy}{', '.join([_pfmt(t) for t in signal.targets[:3]])}")
            print(f"   Stop Loss: {_ccy}{_pfmt(signal.stop_loss)}")
            
        except Exception as e:
            logger.error(f"Error executing trade: {e}")
            print(f"   ❌ execute_trade EXCEPTION: {e}", flush=True)
            return False
    
        return True
    
    def get_win_rate(self) -> float:
        """Calculate win rate from CLOSED trades only"""
        try:
            with self._data_lock:
                trades = list(self._trade_history)
            closed = [t for t in trades if str(t.get("result", "")) in {"WIN", "LOSS"}]
            if not closed:
                return 0.0
            wins = [t for t in closed if str(t.get("result", "")) == "WIN"]
            return (len(wins) / len(closed)) * 100.0
        except Exception:
            return 0.0
    
    async def run_trading_cycle(self):
        """Run main trading cycle - WORKING VERSION"""
        try:
            try:
                self._last_cycle_ts = float(time.time())
                self._last_cycle_error = ""
                self._last_cycle_error_ts = 0.0
                self._last_cycle_stage = "cycle_start"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass

            await self._maybe_send_startup_test()

            # Daily PnL reset at midnight (top bots track daily performance separately)
            try:
                today = datetime.now().date()
                if today != self._daily_pnl_date:
                    logger.info("Daily PnL reset: %s -> %s (yesterday: $%.2f, consec_losses: %d)", self._daily_pnl_date, today, self.daily_pnl, self._consecutive_losses)
                    # Send daily summary via Telegram (Freqtrade /daily pattern)
                    try:
                        if self._telegram_enabled() and (self.total_trades > 0 or abs(self.daily_pnl) > 0.001):
                            _wr = self.get_win_rate()
                            _emoji = "📈" if self.daily_pnl >= 0 else "📉"
                            _day_msg = (
                                f"{_emoji} Daily Summary — {self._daily_pnl_date}\n\n"
                                f"Day PnL: ${self.daily_pnl:+.2f}\n"
                                f"Total PnL: ${self.total_pnl:+.2f}\n"
                                f"Trades: {self.total_trades} | Win Rate: {_wr:.0f}%\n"
                                f"Losses streak: {self._consecutive_losses}"
                            )
                            threading.Thread(target=self._send_telegram_message_sync, args=(_day_msg,), daemon=True).start()
                    except Exception:
                        pass
                    self.daily_pnl = 0.0
                    self._consecutive_losses = 0
                    self._consecutive_wins = 0
                    self._loss_streak_paused_ts = 0.0
                    self._daily_pnl_date = today
            except Exception:
                pass

            _cycle_t0 = time.time()
            logger.info("Cycle step: readiness_check start")
            try:
                if self._kraken_enabled():
                    if not bool(getattr(self, "_kraken_readiness_checked", False)):
                        self._kraken_readiness_checked = True
                        self._kraken_readiness_check(force=True)
                    else:
                        self._kraken_readiness_check(force=False)
            except Exception:
                pass
            logger.info("Cycle step: readiness_check done (%.1fs)", time.time() - _cycle_t0)

            # Capture starting balance on first cycle (so health/drawdown tracking works from cycle #1)
            # IMPORTANT: must use TOTAL EQUITY (free + active positions), not just quote_free!
            # Otherwise buying €99 of ARB drops free from €142→€43 = false 70% "drawdown".
            try:
                if float(self._session_start_balance) <= 0:
                    _lr = getattr(self, "_kraken_last_readiness", None)
                    if isinstance(_lr, dict):
                        _qf = _lr.get("quote_free")
                        if _qf is not None and float(_qf) > 0:
                            _equity = float(_qf)
                            # Add active positions value
                            try:
                                for _s, _td in dict(self.active_signals).items():
                                    _equity += float(_td.get("usd_size", 0) or 0)
                            except Exception:
                                pass
                            # Add pending patient orders value
                            try:
                                for _s, _po in dict(self._pending_orders).items():
                                    _pa = float(_po.get("amount", 0) or 0)
                                    _pp = float(_po.get("price", 0) or 0)
                                    if _pa > 0 and _pp > 0:
                                        _equity += _pa * _pp
                            except Exception:
                                pass
                            self._session_start_balance = _equity
                            self._session_peak_balance = _equity
                            logger.info("Session start equity captured: %.2f %s (free=%.2f + positions)", _equity, self._okx_quote_ccy, float(_qf))
            except Exception:
                pass

            _t0_univ = time.time()
            logger.info("Cycle step: universe_refresh start")
            try:
                if self._kraken_enabled() and self._kraken is not None:
                    now = time.time()
                    last = float(getattr(self, "_kraken_universe_last_refresh_ts", 0.0) or 0.0)
                    if float(self._kraken_universe_refresh_sec) > 0 and (now - last) >= float(self._kraken_universe_refresh_sec):
                        self._kraken_refresh_universe()
            except Exception:
                pass
            logger.info("Cycle step: universe_refresh done (%.1fs)", time.time() - _t0_univ)

            if self._daily_stop_usd and self.daily_pnl <= -abs(self._daily_stop_usd):
                logger.error("DAILY_STOP_USD hit. Trading paused for this session.")
                _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                print(f"\n\U0001F6D1 DAILY STOP HIT (Daily P&L: {_ccy}{self.daily_pnl:+.2f}). Trading paused.")
                await self.manage_active_trades()
                self.display_current_status()
                return

            logger.info("Cycle step: pre_analyze (base_prices=%d)", len(self.base_prices))
            print(f"\nANALYZING {len(self.base_prices)} SYMBOLS...")
            try:
                self._last_cycle_stage = "generate_signals"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass

            async def _gen_and_filter():
                # generate_signals() may do blocking I/O (ccxt). Run it off the event loop
                # so wait_for() can enforce a hard timeout and keep the system alive.
                def _work():
                    sigs = self.generate_signals()
                    return self._filter_signals(sigs)

                return await asyncio.to_thread(_work)

            try:
                timeout_s = float(getattr(self, "_signal_gen_timeout_sec", 45.0) or 45.0)
            except Exception:
                timeout_s = 45.0

            try:
                signals = await asyncio.wait_for(_gen_and_filter(), timeout=timeout_s)
            except asyncio.TimeoutError:
                msg = f"Signal generation timed out after {timeout_s:.1f}s (Kraken/ccxt may be stalled). Skipping this cycle."
                logger.error(msg)
                try:
                    self._last_cycle_error = msg
                    self._last_cycle_error_ts = float(time.time())
                except Exception:
                    pass
                signals = []
            except Exception as e:
                msg = f"Signal generation failed: {e}"
                logger.error(msg)
                try:
                    self._last_cycle_error = msg
                    self._last_cycle_error_ts = float(time.time())
                except Exception:
                    pass
                signals = []

            try:
                self._last_cycle_stage = "post_signals"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass
            
            if not signals:
                try:
                    raw = getattr(self, "_signal_scan_last_raw", 0)
                    filt = getattr(self, "_signal_scan_last_filtered", 0)
                    illiq = len(getattr(self, "_illiquid_cache", {}))
                    print(f"\n📊 Scan complete: {len(self.base_prices)} symbols | {raw} raw signals | {filt} passed filters | {illiq} illiquid cached")
                    # Show skip reason distribution (Jesse/Freqtrade diagnostic pattern)
                    _sd = getattr(self, "_signal_scan_skip_dist", {})
                    if _sd:
                        _top5 = sorted(_sd.items(), key=lambda x: x[1], reverse=True)[:5]
                        _dist_str = " ".join(f"{k}={v}" for k, v in _top5)
                        print(f"   Skip reasons: {_dist_str}")
                    # Show filter drop reason if raw > 0 but filtered = 0
                    if raw > 0 and filt == 0:
                        _fld = getattr(self, "_signal_filter_last_drop", "")
                        _fls = getattr(self, "_signal_filter_last_sym", "")
                        if _fld:
                            print(f"   ⚠️ Filter drop: {_fls} → {_fld}")
                    print(f"   No tradeable signals this cycle. Waiting 60s...")
                except Exception:
                    print(f"\n   No signals found this cycle. Waiting 60s...")

            if signals:
                print(f"\nFOUND {len(signals)} HIGH QUALITY SIGNALS:")
                for i, signal in enumerate(signals[:5], 1):
                    # Compute real R:R for display
                    try:
                        _sl = float(signal.stop_loss)
                        _risk = abs(signal.entry_price - _sl) if _sl > 0 else 0.0
                        _tgts = signal.targets or []
                        _rr_tgt = float(_tgts[1]) if len(_tgts) >= 2 else (float(_tgts[0]) if _tgts else 0.0)
                        _reward = abs(_rr_tgt - signal.entry_price)
                        _rr = (_reward / _risk) if _risk > 0 else 0.0
                    except Exception:
                        _rr = 0.0
                    _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                    print(f"   {i}. {signal.symbol} - {signal.action} @ {_ccy}{signal.entry_price:.6f}")
                    print(f"      Confidence: {signal.confidence:.1f}% | R/R: {_rr:.2f}:1")
                    print(f"      Reasoning: {signal.reasoning}")
                
                print(f"\nEXECUTING BEST SIGNALS (Max {self.max_concurrent_trades} trades)...", flush=True)
                
                for signal in signals[: max(1, self._max_signals_per_cycle)]:
                    _n_pending = len(getattr(self, "_pending_orders", {}) or {})
                    if (len(self.active_signals) + _n_pending) < self.max_concurrent_trades:
                        if signal.symbol not in self.active_signals and signal.symbol not in getattr(self, "_pending_orders", {}):
                            print(f"\nEXECUTING: {signal.symbol}", flush=True)
                            opened = self.execute_trade(signal)
                            if opened:
                                print(f"   ✅ TRADE OPENED: {signal.symbol} {signal.action}", flush=True)
                                await self.send_entry_signal(signal)
                            else:
                                print(f"   ❌ TRADE FAILED: {signal.symbol} — check logs above", flush=True)
            
            # Monitor patient pending orders (fills / expiry / amendments)
            try:
                if getattr(self, "_patient_orders_enabled", False) and getattr(self, "_pending_orders", {}):
                    self._last_cycle_stage = "patient_check"
                    self._last_cycle_stage_ts = float(time.time())
                    _n_pending = len(self._pending_orders)
                    print(f"\n📋 Checking {_n_pending} patient order(s)...", flush=True)

                    # Snapshot ALL pending orders BEFORE check runs.
                    # _patient_check_pending sets po["_filled"]=True on the dict THEN removes key.
                    # Since we hold refs to the same dicts, _filled will be visible after check.
                    _pre_snapshot = {k: v for k, v in self._pending_orders.items()}

                    self._patient_check_pending()

                    # Check for orders that were removed AND got _filled set during check
                    for _ps, _po in _pre_snapshot.items():
                        if _ps not in self._pending_orders and _po.get("_filled"):
                            # This patient order just got filled — activate the trade
                            _sig = _po.get("signal")
                            if _sig and _ps not in self.active_signals:
                                _avg_px = float(_po.get("_filled_avg", 0) or 0)
                                _filled_amt = float(_po.get("_filled_amount", 0) or 0)
                                _wait_sec = int(_po.get("_wait_sec", 0) or 0)
                                if _avg_px > 0:
                                    _sig.entry_price = _avg_px  # Update signal entry to actual fill price
                                print(f"   🎉 Activating filled patient trade: {_ps} {_sig.action} @ {_avg_px:.8f} (waited {_wait_sec}s)", flush=True)
                                # Execute trade with the filled signal (it will skip order placement since we already have the fill)
                                _po_meta = {
                                    "ok": True, "mode": "live", "patient": True,
                                    "order_id": str(_po.get("order_id", "")),
                                    "filled": _filled_amt, "average": _avg_px,
                                    "amount": _filled_amt, "price": _avg_px,
                                    "pair": str(_po.get("pair", "")),
                                    "side": str(_po.get("side", "")),
                                    "patient_wait_sec": _wait_sec,
                                }
                                self._activate_patient_fill(_sig, _po_meta)
                                await self.send_entry_signal(_sig)
            except Exception as e_pat:
                logger.debug("Patient check error: %s", e_pat)

            # Manage active trades
            try:
                self._last_cycle_stage = "manage_active_trades"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass
            await self.manage_active_trades()
            
            # Display status
            try:
                self._last_cycle_stage = "display_status"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass
            self.display_current_status()

            # Update cycle timestamp AFTER all work is done (before sleep).
            # Without this, cycle_age_sec includes the 60s sleep → false STALLED.
            try:
                self._last_cycle_ts = float(time.time())
                self._last_cycle_stage = "cycle_done"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass

            # Refresh dashboard caches at end of cycle (trading loop holds _data_lock
            # during scan/manage, so API endpoints often get lock timeout → stale cache).
            try:
                self._refresh_dashboard_caches()
            except Exception:
                pass
            
        except Exception as e:
            logger.error(f"Error in trading cycle: {e}")
            print(f"\n⚠️ Error in trading cycle: {e}")
            try:
                self._last_cycle_error = str(e)
                self._last_cycle_error_ts = float(time.time())
                self._last_cycle_stage = "cycle_exception"
                self._last_cycle_stage_ts = float(time.time())
            except Exception:
                pass
    
    def _refresh_dashboard_caches(self) -> None:
        """Snapshot active trades + summary into dashboard caches (called at end of each cycle).
        This lets dashboard API endpoints serve fresh data even when _data_lock is contended."""
        try:
            now = int(time.time())
            # Active trades cache
            trades = []
            try:
                for sym, t in list(self.active_signals.items()):
                    sig = t.get('signal')
                    entry_time = t.get('entry_time')
                    time_in_trade = 0
                    if entry_time:
                        try:
                            time_in_trade = int((datetime.now() - entry_time).total_seconds())
                        except Exception:
                            time_in_trade = 0
                    price_now = float(self._last_prices.get(sym, getattr(sig, 'entry_price', 0.0) or 0.0))
                    targets = [float(x) for x in getattr(sig, 'targets', [])[:4]]
                    tp_reached = list(t.get('tp_reached', []))
                    next_tp = None
                    try:
                        idx = len(tp_reached)
                        if 0 <= idx < len(targets):
                            next_tp = targets[idx]
                    except Exception:
                        pass
                    trades.append({
                        "id": int(getattr(sig, 'signal_id', 0)),
                        "symbol": sym,
                        "side": getattr(sig, 'action', ''),
                        "entry": float(getattr(sig, 'entry_price', 0.0)),
                        "price": price_now,
                        "sl": float(getattr(sig, 'stop_loss', 0.0)),
                        "tp": targets,
                        "next_tp": next_tp,
                        "pnl": float(t.get('pnl', 0.0)),
                        "mfe": float(t.get('mfe', 0.0)),
                        "mae": float(t.get('mae', 0.0)),
                        "amount": float(t.get('amount', 0.0) or 0.0),
                        "usd_size": float(t.get('usd_size', 0.0) or 0.0),
                        "tp_reached": tp_reached,
                        "time_in_trade_sec": time_in_trade,
                        "ts": int(entry_time.timestamp()) if entry_time else now,
                    })
            except Exception:
                pass
            self._dashboard_active_trades_cache = {"trades": trades, "status": "OK", "ts": now}

            # Summary cache
            try:
                stats = self._calc_performance_stats()
                unrealized = 0.0
                for _, t in self.active_signals.items():
                    try:
                        unrealized += float(t.get("pnl", 0.0) or 0.0)
                    except Exception:
                        pass
                realized = float(self.total_pnl)
                session_up = round(time.time() - float(getattr(self, "_session_start_ts", time.time())), 0)
                self._dashboard_summary_cache = {
                    "status": "ACTIVE" if self.running else "STOPPED",
                    "ts": now,
                    "active_trades": len(self.active_signals),
                    "max_concurrent_trades": int(self.max_concurrent_trades),
                    "win_rate": float(self.get_win_rate()),
                    "daily_pnl": float(self.daily_pnl),
                    "total_pnl": float(self.total_pnl),
                    "realized_pnl": float(realized),
                    "unrealized_pnl": float(unrealized),
                    "total_pnl_combined": float(realized + unrealized),
                    "total_trades": int(self.total_trades),
                    "win_count": int(stats.get("win_count", 0)),
                    "loss_count": int(stats.get("loss_count", 0)),
                    "profit_factor": float(stats.get("profit_factor", 0.0)),
                    "expectancy": float(stats.get("expectancy", 0.0)),
                    "max_drawdown": float(stats.get("max_drawdown", 0.0)),
                    "avg_win": float(stats.get("avg_win", 0.0)),
                    "avg_loss": float(stats.get("avg_loss", 0.0)),
                    "best_trade": float(stats.get("best_trade", 0.0)),
                    "worst_trade": float(stats.get("worst_trade", 0.0)),
                    "sharpe_ratio": float(stats.get("sharpe_ratio", 0.0)),
                    "avg_trade_duration_sec": float(stats.get("avg_trade_duration_sec", 0.0)),
                    "session_uptime_sec": float(session_up),
                    "equity_curve": stats.get("equity_curve", []),
                    "health": {
                        "okx": bool(self._kraken_enabled() and self._kraken is not None),
                        "okx_universe": int(len(self._kraken_universe_symbols or [])),
                        "telegram": bool(self._telegram_enabled()),
                        "dash_port": int(getattr(self, "dashboard_port", 0) or 0),
                        "okx_readiness": dict(getattr(self, "_kraken_last_readiness", {}) or {}),
                    },
                }
            except Exception:
                pass

            # Signals cache
            try:
                self._dashboard_signals_cache = {"signals": list(self._signal_history)[-200:][::-1], "status": "OK", "ts": now}
            except Exception:
                pass

            # History cache
            try:
                self._dashboard_trade_history_cache = {"trades": list(self._trade_history)[-50:][::-1], "status": "OK", "ts": now}
            except Exception:
                pass
        except Exception:
            pass

    def _estimate_fees(self, usd_size: float) -> float:
        """Estimate round-trip fees for a trade (entry + exit). Top bots always account for fees."""
        try:
            return float(usd_size) * float(self._taker_fee_pct) / 100.0 * 2.0
        except Exception:
            return 0.0

    def _current_portfolio_heat(self) -> float:
        """Total USD exposure across all active trades. Top bots limit total exposure."""
        try:
            total = 0.0
            for _, td in self.active_signals.items():
                total += float(td.get("usd_size", 0.0) or 0.0)
            return total
        except Exception:
            return 0.0

    def _graceful_shutdown(self) -> None:
        """Close active positions and export trade journal on shutdown. All top bots do this."""
        self.running = False

        # Cancel all pending patient orders on exchange
        _pending = getattr(self, "_pending_orders", {}) or {}
        if _pending:
            print(f"\n⚠️ SHUTDOWN: Cancelling {len(_pending)} pending patient order(s)...")
            for _ps, _po in list(_pending.items()):
                try:
                    _oid = str(_po.get("order_id", "") or "")
                    _pair = str(_po.get("pair", "") or "")
                    if _oid and _pair:
                        self._cancel_limit_order_safe(_oid, _pair)
                        print(f"   🔄 Cancelled pending order: {_ps} ({_oid})")
                except Exception:
                    pass
            self._pending_orders.clear()

        n_active = len(self.active_signals)
        if n_active > 0:
            print(f"\n⚠️ GRACEFUL SHUTDOWN: {n_active} active trade(s) — placing exit orders...")
            is_live = bool(self._kraken_trading_enabled and self._kraken_trading_mode == "live")
            for symbol, trade_data in list(self.active_signals.items()):
                try:
                    signal = trade_data.get("signal")
                    if signal is None:
                        logger.warning("Shutdown: %s has no signal object — skipping", symbol)
                        continue
                    amt = float(trade_data.get("amount", 0.0) or 0.0)
                    current_price = self.get_current_price(symbol)
                    _sd_exit_ok = False  # Track whether exit order succeeded
                    if is_live and amt > 0 and self._kraken is not None:
                        exit_side = "SELL" if signal.action == "BUY" else "BUY"
                        try:
                            pair = self._kraken_supported_pair(symbol)
                            if pair:
                                # Cancel TP/SL triggers before placing shutdown exit
                                for _trig_key in ("tp_order_id", "sl_order_id"):
                                    _trig_id = trade_data.get(_trig_key)
                                    if _trig_id:
                                        try:
                                            self._cancel_trigger_order(str(_trig_id), pair)
                                        except Exception:
                                            pass
                                        trade_data[_trig_key] = None
                                rounded_amt = self._kraken_round_amount(pair, float(amt))
                                if rounded_amt <= 0:
                                    rounded_amt = float(amt)
                                # Min notional guard (dust positions can't be sold)
                                _sd_skip = False
                                try:
                                    _sd_px = current_price if current_price and current_price > 0 else float(signal.entry_price)
                                    _sd_notional = float(rounded_amt) * _sd_px
                                    _sd_m = getattr(self._kraken, "markets", {}).get(pair) or {}
                                    _sd_cmin = float(((_sd_m.get("limits") or {}).get("cost") or {}).get("min") or 0)
                                    if _sd_cmin > 0 and _sd_notional < _sd_cmin:
                                        _sd_skip = True
                                        _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
                                        print(f"   {symbol}: DUST ({_ccy}{_sd_notional:.4f} < min {_ccy}{_sd_cmin:.2f}) — skipped")
                                except Exception:
                                    pass
                                if not _sd_skip:
                                    try:
                                        _sd_tout = float(getattr(self, "_kraken_call_timeout_sec", 15.0) or 15.0)
                                        _sd_box = [None]
                                        def _do_sd_sell():
                                            try:
                                                _sd_box[0] = self._kraken.create_order(pair, "market", exit_side.lower(), float(rounded_amt))
                                            except Exception as _e:
                                                _sd_box[0] = _e
                                        _sd_t = threading.Thread(target=_do_sd_sell, daemon=True)
                                        _sd_t.start()
                                        _sd_t.join(timeout=max(3.0, min(_sd_tout, 10.0)))
                                        if _sd_t.is_alive():
                                            raise Exception("shutdown_sell_timeout")
                                        if isinstance(_sd_box[0], Exception):
                                            raise _sd_box[0]
                                        mkt = _sd_box[0]
                                        oid = mkt.get("id") if isinstance(mkt, dict) else None
                                        # Verify fill
                                        _sd_filled = True
                                        try:
                                            if oid:
                                                _sd_st = self._kraken_fetch_order_safe(str(oid), pair)
                                                if isinstance(_sd_st, dict):
                                                    _sd_f = _sd_st.get("filled")
                                                    if _sd_f is not None and float(_sd_f) <= 0:
                                                        _sd_filled = False
                                        except Exception:
                                            pass
                                        _sd_exit_ok = _sd_filled
                                        _fill_tag = "✓" if _sd_filled else "⚠ NOT FILLED"
                                        print(f"   {symbol}: market {exit_side} {rounded_amt:.6f} — order_id={oid} {_fill_tag}")
                                    except Exception as _sd_e:
                                        _sd_err = str(_sd_e)
                                        if "51008" in _sd_err or "insufficient" in _sd_err.lower():
                                            # Retry with real balance instead of assuming trigger sold
                                            try:
                                                _sd_base = pair.split("/")[0] if "/" in pair else ""
                                                _sd_real = self._kraken_free_balance(_sd_base) if _sd_base else 0.0
                                                _sd_real_r = self._kraken_round_amount(pair, _sd_real) if _sd_real > 0 else 0.0
                                                if _sd_real_r > 0:
                                                    print(f"   {symbol}: retrying sell with real balance {_sd_real_r:.6f} (was {rounded_amt:.6f})")
                                                    _sd_r_box = [None]
                                                    def _do_sd_retry():
                                                        try:
                                                            _sd_r_box[0] = self._kraken.create_order(pair, "market", exit_side.lower(), float(_sd_real_r))
                                                        except Exception as _e:
                                                            _sd_r_box[0] = _e
                                                    _sd_rt = threading.Thread(target=_do_sd_retry, daemon=True)
                                                    _sd_rt.start()
                                                    _sd_rt.join(timeout=max(3.0, min(_sd_tout, 10.0)))
                                                    if _sd_rt.is_alive() or isinstance(_sd_r_box[0], Exception):
                                                        raise Exception(_sd_r_box[0] if isinstance(_sd_r_box[0], Exception) else "retry_timeout")
                                                    _sd_r_mkt = _sd_r_box[0]
                                                    _sd_r_oid = _sd_r_mkt.get("id") if isinstance(_sd_r_mkt, dict) else None
                                                    _sd_exit_ok = True
                                                    print(f"   {symbol}: ✅ retry sold {_sd_real_r:.6f} — order_id={_sd_r_oid}")
                                                else:
                                                    _sd_exit_ok = True  # trigger sold on exchange = exit ok
                                                    print(f"   {symbol}: ℹ️ balance={_sd_real:.8f} (dust) — trigger sold on exchange")
                                            except Exception as _sd_re:
                                                print(f"   {symbol}: ❌ retry also failed — {_sd_re}")
                                        else:
                                            raise
                        except Exception as e:
                            logger.error("Shutdown exit order failed for %s: %s", symbol, e)
                            print(f"   {symbol}: EXIT FAILED — {e}")
                    else:
                        print(f"   {symbol}: closed internally (paper/no amount)")
                    # Record PnL (fall back to entry price if current_price is stale/None)
                    if not current_price or current_price <= 0:
                        current_price = float(signal.entry_price) if signal else 0.0
                    if current_price and signal:
                        if signal.action == "BUY":
                            pnl = (current_price - signal.entry_price) * amt
                        else:
                            pnl = (signal.entry_price - current_price) * amt
                        usd_size = float(trade_data.get("usd_size", 0.0) or 0.0) or (amt * current_price)
                        fees = self._estimate_fees(usd_size)
                        pnl -= fees
                        self.daily_pnl += pnl
                        self.total_pnl += pnl
                        self.total_trades += 1
                        _sd_result = "WIN" if pnl > 0 else "LOSS"
                        if pnl > 0:
                            self.winning_trades += 1
                            self._consecutive_losses = 0
                            self._consecutive_wins += 1
                        else:
                            self._consecutive_losses += 1
                            self._consecutive_wins = 0
                        _sd_elapsed = 0
                        try:
                            _sd_elapsed = int((datetime.now() - trade_data.get("entry_time", datetime.now())).total_seconds())
                        except Exception:
                            pass
                        _sd_pips = 0.0
                        try:
                            _sd_pip_size = self._pip_size_for_symbol(symbol, float(signal.entry_price))
                            if _sd_pip_size <= 0:
                                _sd_pip_size = 1.0
                            if signal.action == 'BUY':
                                _sd_pips = (current_price - signal.entry_price) / _sd_pip_size
                            else:
                                _sd_pips = (signal.entry_price - current_price) / _sd_pip_size
                        except Exception:
                            pass
                        try:
                            with self._data_lock:
                                self._trade_history.append({
                                    "id": int(getattr(signal, 'signal_id', 0)),
                                    "ts": int(datetime.now().timestamp()),
                                    "symbol": symbol,
                                    "result": _sd_result,
                                    "pnl": float(pnl),
                                    "exit": float(current_price),
                                    "pips": float(_sd_pips),
                                    "reason": "SHUTDOWN",
                                    "duration_sec": _sd_elapsed,
                                    "mfe": float(trade_data.get('mfe', 0.0)),
                                    "mae": float(trade_data.get('mae', 0.0)),
                                    "tp_reached": list(trade_data.get('tp_reached', [])),
                                    "exit_order_ok": bool(_sd_exit_ok),
                                    "exit_order_id": "",
                                    "exit_order_error": "" if _sd_exit_ok else "shutdown_exit_failed",
                                })
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("Shutdown error for %s: %s", symbol, e)
            self.active_signals.clear()
        # Export trade journal
        try:
            self._export_trade_journal()
        except Exception as e:
            logger.error("Trade journal export failed: %s", e)

    def _export_trade_journal(self) -> None:
        """Export trade history to CSV for analysis. Top bots (Freqtrade/Jesse) always do this."""
        try:
            trades = list(self._trade_history)
            if not trades:
                return
            import csv
            # Use fixed name for latest + timestamped backup to prevent overwriting previous sessions
            journal_path = os.path.join(os.path.dirname(__file__), "trade_journal.csv")
            try:
                ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(os.path.dirname(__file__), f"trade_journal_{ts_str}.csv")
            except Exception:
                backup_path = None
            fieldnames = ["id", "ts", "symbol", "result", "pnl", "exit", "pips", "reason", "duration_sec", "mfe", "mae", "tp_reached", "exit_order_ok", "exit_order_id", "exit_order_error"]
            with open(journal_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for t in trades:
                    row = dict(t)
                    if isinstance(row.get("tp_reached"), list):
                        row["tp_reached"] = ",".join(row["tp_reached"])
                    writer.writerow(row)
            print(f"📋 Trade journal exported: {journal_path} ({len(trades)} trades)")
            # Write timestamped backup
            if backup_path:
                try:
                    import shutil
                    shutil.copy2(journal_path, backup_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to export trade journal: %s", e)

    def display_current_status(self):
        """Display current trading status"""
        stats = self._calc_performance_stats()
        _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
        print(f"\n{'='*80}")
        print(f"ROBOXRP PRO - UNIFIED TRADING SYSTEM v13.2 WORKING")
        print(f"   Time: {datetime.now().strftime('%H:%M:%S')}")
        print(f"   Active Trades: {len(self.active_signals)}/{self.max_concurrent_trades}")
        print(f"   Win Rate: {self.get_win_rate():.1f}%")
        print(f"   Profit Factor: {stats.get('profit_factor', 0.0):.2f}")
        print(f"   Expectancy: {_ccy}{stats.get('expectancy', 0.0):+.2f}/trade")
        print(f"   Max Drawdown: {_ccy}{stats.get('max_drawdown', 0.0):.2f}")
        print(f"   Daily P&L: {_ccy}{self.daily_pnl:+.2f}")
        print(f"   Total P&L: {_ccy}{self.total_pnl:+.2f}")
        print(f"   Session Time: {str(datetime.now() - self.session_start).split('.')[0]}")
        print(f"{'='*80}")
        
        if self.active_signals:
            print(f"\nACTIVE TRADES:")
            for symbol, trade in self.active_signals.items():
                signal = trade['signal']
                print(f"   {symbol}: {signal.action} @ {_ccy}{signal.entry_price:.6f}")
                print(f"           P&L: {_ccy}{trade['pnl']:+.2f} | Confidence: {signal.confidence:.1f}%")
    
    async def run(self):
        """Run the trading system - WORKING VERSION"""
        try:
            print(f"\n🚀 STARTING ROBOXRP PRO TRADING SYSTEM v13.2")
            _compound_pct = float(getattr(self, "_compound_position_pct", 0) or 0)
            if _compound_pct > 0:
                print(f"💰 Mode: {self._kraken_trading_mode.upper()} | Position: {_compound_pct:.0f}% of balance (compound) | Max Trades: {self.max_concurrent_trades}")
            else:
                print(f"💰 Mode: {self._kraken_trading_mode.upper()} | Position: €{self._kraken_position_usd:.0f} fixed | Max Trades: {self.max_concurrent_trades}")
            _side_mode = "BUY-ONLY" if self._spot_buy_only else "BUY+SELL (spot safe)"
            print(f"📈 Quote: {self._okx_quote_ccy} | Spread Guard: {self._kraken_max_spread_pct}% | Sides: {_side_mode}")
            print(f"⚡ Strict Exec: {self._kraken_strict_execution} | R:R Min: {self._min_rr_ratio} | RSI Buy Max: {self._kraken_mtf_rsi_buy_max}")
            print(f"🎯 TP1 Min: {self._min_tp1_pct}% | Confidence Min: {self._min_signal_confidence} | Max Trade: {self._max_trade_sec/3600:.1f}h")
            _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
            print(f"🛡️ Fee: {self._taker_fee_pct}% | Max Losses: {self._max_consecutive_losses} | Heat Cap: {_ccy+str(self._max_portfolio_heat_usd) if self._max_portfolio_heat_usd > 0 else 'OFF'} | Max DD: {self._max_drawdown_pct}%")
            _pat_mode = "ON" if self._patient_orders_enabled else "OFF"
            _pat_secs = float(self._patient_order_max_age_sec)
            _pat_wait = f"{_pat_secs/3600:.1f}h" if _pat_secs >= 3600 else f"{_pat_secs/60:.0f}m"
            _pat_price = self._patient_price_mode.upper()
            print(f"📋 Patient Orders: {_pat_mode} | Max Wait: {_pat_wait} | Price: {_pat_price} | Amend: {self._patient_order_amend_pct}%")
            if self._kraken_live_prices_enabled and not self._kraken_trading_enabled:
                print(f"🧪 OKX live prices ON, trading OFF (simulated execution)")

            try:
                self._start_market_data()
            except Exception:
                pass
            # Start dashboard after core system is up, so console stays clean
            self._start_dashboard()
            print(f"🌐 Live Dashboard: http://localhost:{self.dashboard_port}")

            try:
                if self._telegram_enabled():
                    print("📱 Telegram: ENABLED")
                else:
                    missing = []
                    if not bool(getattr(self, "_telegram_bot_token", None)):
                        missing.append("TELEGRAM_BOT_TOKEN")
                    if not bool(getattr(self, "_telegram_chat_id", None)):
                        missing.append("TELEGRAM_CHAT_ID")
                    if missing:
                        print(f"📱 Telegram: DISABLED (missing {', '.join(missing)})")
                    else:
                        print("📱 Telegram: DISABLED (check TELEGRAM_ENABLED=false or disabled after failures)")
            except Exception:
                pass

            print(f"{'='*80}")
            
            # Main trading loop
            while self.running:
                try:
                    self._cycle_count += 1
                    _cycle_t0 = time.time()
                    print(f"\n{'─'*60}")
                    print(f"🔄 CYCLE #{self._cycle_count} | {datetime.now().strftime('%H:%M:%S')} | Active: {len(self.active_signals)} trades")
                    await self.run_trading_cycle()
                    # Track cycle duration (keep last 20 for rolling avg)
                    try:
                        _cd = round(time.time() - _cycle_t0, 1)
                        self._cycle_times.append(_cd)
                        if len(self._cycle_times) > 20:
                            self._cycle_times = self._cycle_times[-20:]
                    except Exception:
                        pass
                    
                    # Adaptive cycle timing (Freqtrade pattern):
                    # Active trades or pending orders → check every 30s (price monitoring + fill detection)
                    # No trades → 60s (signal scanning only, save API calls)
                    _n_active = len(self.active_signals)
                    _n_pending_po = len(getattr(self, "_pending_orders", {}) or {})
                    _cycle_sleep = 15 if (_n_active > 0 or _n_pending_po > 0) else 60
                    print(f"\n⏳ Waiting {_cycle_sleep}s for next cycle...", flush=True)
                    await asyncio.sleep(_cycle_sleep)
                except KeyboardInterrupt:
                    print(f"\n🛑 Trading system stopped by user")
                    break
                except Exception as e:
                    logger.error(f"Error in main loop: {e}")
                    print(f"\n⚠️ Error in main loop: {e}")
                    print(f"Waiting 10 seconds before retry...")
                    await asyncio.sleep(10)
            
        except Exception as e:
            print(f"\n🚨 CRITICAL ERROR: {e}")
            logger.error(f"Critical system error: {e}")
        finally:
            # Graceful shutdown: close active trades and export journal (top bots always do this)
            try:
                self._graceful_shutdown()
            except Exception as e_shut:
                logger.error("Graceful shutdown error: %s", e_shut)

            print(f"\n📊 SESSION SUMMARY:")
            print(f"   Total Trades: {self.total_trades}")
            print(f"   Winning Trades: {self.winning_trades}")
            print(f"   Win Rate: {self.get_win_rate():.1f}%")
            _ccy = "€" if str(getattr(self, "_okx_quote_ccy", "EUR") or "EUR").upper() == "EUR" else "$"
            print(f"   Total P&L: {_ccy}{self.total_pnl:+.2f}")
            print(f"   Session Time: {str(datetime.now() - self.session_start).split('.')[0]}")
            print(f"\nThank you for using ROBOXRP PRO v13.2 WORKING!")

# Main execution
if __name__ == "__main__":
    import sys
    
    try:
        trading_system = UnifiedTradingSystem()

        try:
            asyncio.run(trading_system.run())
        except KeyboardInterrupt:
            print(f"\n🛑 System stopped by user - Shutting down gracefully...")
        except Exception as e:
            print(f"\n🚨 FATAL ERROR: {e}")
            sys.exit(1)

    except Exception as e:
        print(f"\n🚨 INITIALIZATION ERROR: {e}")
        sys.exit(1)
