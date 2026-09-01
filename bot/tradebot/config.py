"""Configuration. Secrets come from the environment (systemd EnvironmentFile=
/etc/tradebot/secrets.env). Everything else is an editable default mirroring
the skill tables in .agents/skills/."""
import os


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"missing required env var {name}")
    return v


# --- identities & secrets (from secrets.env) --------------------------------
TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_USER_ID = int(env("TELEGRAM_USER_ID", "0"))
HEALTHCHECK_URL = env("HEALTHCHECK_URL")
COINBASE_API_KEY = env("COINBASE_API_KEY")           # CDP key name
COINBASE_API_SECRET = env("COINBASE_API_SECRET")     # CDP private key (Ed25519)
COINBASE_PORTFOLIO = env("COINBASE_PORTFOLIO", "HypeBot")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-opus-5")
# Cost controls (agent layer). Editable via secrets.env without a code change.
DISCOVERY_INTERVAL_SEC = int(env("DISCOVERY_INTERVAL_SEC", "900"))
AGENT_EFFORT = env("AGENT_EFFORT", "low")          # low effort suits routine scanning
AGENT_MAX_TOKENS = int(env("AGENT_MAX_TOKENS", "8000"))
AGENT_MAX_CANDIDATES = int(env("AGENT_MAX_CANDIDATES", "15"))  # payload cap per cycle
SOLANA_KEYFILE = env("SOLANA_KEYFILE", "/etc/tradebot/solana_wallet.json")
EVM_KEYFILE = env("EVM_KEYFILE", "/etc/tradebot/evm_wallet.key")
SOLANA_RPC = env("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
BASE_RPC = env("BASE_RPC", "https://mainnet.base.org")

# --- paths ------------------------------------------------------------------
DB_PATH = env("TRADEBOT_DB", "/var/lib/tradebot/tradebot.db")

# --- hard limits (risk-limits skill; NOT overridable at runtime) ------------
MAX_POSITION_PCT = 0.05          # hard limit 1: 5% of portfolio per position
HALT_DRAWDOWN_PCT = 0.20         # hard limit 2: 20% off trailing-24h peak
HALT_WINDOW_SEC = 24 * 3600

# --- editable defaults (mirror the skill tables) ----------------------------
MAX_CONCURRENT_POSITIONS = 10
MAX_AGGREGATE_DEPLOYED_PCT = 0.50
MAX_PER_VENUE_PCT = 0.50
MAX_PER_CHAIN_PCT = 0.30
MAX_CORRELATION_GROUP_PCT = 0.15
MAX_SINGLE_ORDER_PCT = 0.025     # fat-finger: single order notional cap
MAX_PRICE_DEVIATION = 0.05       # fat-finger: vs reference price
SLIPPAGE_TIERS = {"deep": 0.0025, "liquid": 0.0075, "thin": 0.015, "micro": 0.03}
MAX_BOOK_DEPTH_SHARE = 0.10      # order notional vs visible depth
MAX_POOL_SHARE = 0.01            # swap notional vs pool liquidity
EXIT_SAFETY_MAX_TAX = 0.10       # max transfer tax
EXIT_SAFETY_FRESH_SEC = 600      # exit-safety check freshness
TICKET_MAX_AGE_SEC = 900         # BUY NOW ticket max research age
APPROVAL_EXPIRY_SEC = 1800       # standard approval expiry
APPROVAL_EXPIRY_FAST_SEC = 600   # high-velocity approval expiry
GATE_TIME_BUDGET_SEC = 5
GAS_EXITS_FLOOR = 20             # native-token float sized for N exits
ROUNDTRIP_COST_MAX = 0.03        # gas-aware minimum-position rule
MONITOR_INTERVAL_TOKEN_SEC = 5
MONITOR_INTERVAL_CEX_SEC = 10
VALUE_SAMPLE_SEC = 60            # rolling value series sampling
RECON_INTERVAL_SEC = 300         # reconciliation poll
STALE_PRICE_SEC = 120
HEARTBEAT_SEC = 60
LIQ_DRAIN_WARN = 0.30            # pool liquidity down vs entry -> urgent reeval
LIQ_DRAIN_EXIT = 0.50            # -> exit evaluation, default exit

# --- go-live phases (go-live skill) -----------------------------------------
# phase: 0 = wiring (no orders), 1 = venue-minimum sizing, 2..4 = 25/50/100%
PHASE_SIZE_FACTOR = {0: 0.0, 1: 0.0, 2: 0.25, 3: 0.50, 4: 1.00}
PHASE1_ORDER_USD = 5.0           # venue-minimum sizing for phase 1

VENUES = {"coinbase": "cex", "solana": "chain", "base": "chain"}
CHAIN_GAS_TOKEN = {"solana": "SOL", "base": "ETH"}
