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
# The quote currency the portfolio actually holds. Products quoted in anything
# else are unbuyable however good they look -- see coinbase.quote_balance.
COINBASE_QUOTE = env("COINBASE_QUOTE", "USDC")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-opus-5")
# Identity-linked (Personal) API keys must name the workspace they act in.
# Leave unset for a Workspace-type key.
ANTHROPIC_WORKSPACE_ID = env("ANTHROPIC_WORKSPACE_ID")
# Cost controls (agent layer). Editable via secrets.env without a code change.
DISCOVERY_INTERVAL_SEC = int(env("DISCOVERY_INTERVAL_SEC", "900"))
AGENT_EFFORT = env("AGENT_EFFORT", "low")          # low effort suits routine scanning
AGENT_MAX_TOKENS = int(env("AGENT_MAX_TOKENS", "8000"))
AGENT_MAX_CANDIDATES = int(env("AGENT_MAX_CANDIDATES", "15"))  # payload cap per cycle
AGENT_CANDLE_SHORTLIST = int(env("AGENT_CANDLE_SHORTLIST", "2"))  # candles fetched per cycle
SOLANA_KEYFILE = env("SOLANA_KEYFILE", "/etc/tradebot/solana_wallet.json")
EVM_KEYFILE = env("EVM_KEYFILE", "/etc/tradebot/evm_wallet.key")
SOLANA_RPC = env("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
# Jupiter has retired an endpoint under us before: quote-api.jup.ag stopped
# resolving and every Solana swap died at DNS. Tried in order; the first that
# answers is remembered for the process.
JUPITER_BASES = [b.strip() for b in env(
    "JUPITER_BASES",
    "https://lite-api.jup.ag/swap/v1,https://api.jup.ag/swap/v1,"
    "https://quote-api.jup.ag/v6").split(",") if b.strip()]
BASE_RPC = env("BASE_RPC", "https://mainnet.base.org")

LOG_STDOUT = env("TRADEBOT_LOG_STDOUT", "1") not in ("0", "false", "")

# --- paths ------------------------------------------------------------------
DB_PATH = env("TRADEBOT_DB", "/var/lib/tradebot/tradebot.db")

# --- hard limits (risk-limits skill; NOT overridable at runtime) ------------
# HONESTY NOTE: these numbers were chosen, not derived. Nothing in this system
# has ever validated them against a realised loss distribution -- there is no
# outcome data yet (see journal.outcomes). They are enforced strictly, which
# makes them look better-founded than they are. Treat them as placeholders
# pending calibration, not as settled policy.
MAX_POSITION_PCT = 0.05          # hard limit 1: 5% of portfolio per position
HALT_DRAWDOWN_PCT = 0.20         # hard limit 2: 20% off trailing-24h peak
HALT_WINDOW_SEC = 24 * 3600

# --- editable defaults (mirror the skill tables) ----------------------------
MAX_CONCURRENT_POSITIONS = 10
MAX_AGGREGATE_DEPLOYED_PCT = 0.50
MAX_PER_VENUE_PCT = 0.50
MAX_PER_CHAIN_PCT = 0.30
MAX_CORRELATION_GROUP_PCT = 0.15
# Equal to MAX_POSITION_PCT by design: this check exists to catch a
# mis-computed order, not to enforce sizing policy. At 2.5% it silently made
# the position cap unreachable -- every phase-4 order was rejected as a fat
# finger, and phase 3 passed only by landing exactly on the boundary.
MAX_SINGLE_ORDER_PCT = 0.05      # fat-finger: single order notional cap
MAX_PRICE_DEVIATION = 0.05       # fat-finger: vs reference price
SLIPPAGE_TIERS = {"deep": 0.0025, "liquid": 0.0075, "thin": 0.015, "micro": 0.03}
MAX_BOOK_DEPTH_SHARE = 0.10      # order notional vs visible depth
MAX_POOL_SHARE = 0.01            # swap notional vs pool liquidity
EXIT_SAFETY_MAX_TAX = 0.10       # max transfer tax
EXIT_SAFETY_FRESH_SEC = 600      # exit-safety check freshness
TICKET_MAX_AGE_SEC = 900         # BUY NOW ticket max research age
WHITELIST_TTL_SEC = 7 * 86400    # an approval authorises this asset for 1 week
WHITELIST_MAX_REENTRIES = 3      # re-entries per approval before re-asking
WHITELIST_REAPPROVE_AFTER_LOSS = True   # a losing exit ends the authorisation
APPROVAL_EXPIRY_SEC = 1800       # standard approval expiry
APPROVAL_EXPIRY_FAST_SEC = 600   # high-velocity approval expiry
GATE_TIME_BUDGET_SEC = 5
GAS_EXITS_FLOOR = 20             # native-token float sized for N exits
GAS_COST_PER_EXIT = {"solana": 0.0008, "base": 0.00004}  # native units, one swap
ROUNDTRIP_COST_MAX = 0.03        # gas-aware minimum-position rule
MONITOR_INTERVAL_TOKEN_SEC = 5
MONITOR_INTERVAL_CEX_SEC = 10
VALUE_SAMPLE_SEC = 60            # rolling value series sampling
RECON_INTERVAL_SEC = 300         # reconciliation poll
STALE_PRICE_SEC = 120
HEARTBEAT_SEC = 60
FILL_TIMEOUT_CEX_SEC = 60        # wait for a limit order to fill before cancelling
FILL_TIMEOUT_EVM_SEC = 180       # wait for a Base swap receipt
FILL_TIMEOUT_SOL_SEC = 90        # wait for a Solana signature to confirm
QTY_SANITY_FACTOR = 10           # booked qty*price must be within this of cost
SETTLE_READ_TRIES = 5            # balance re-reads after a confirmed swap
SETTLE_READ_SLEEP_SEC = 3
TELEGRAM_STALE_SEC = 300         # no successful poll for this long -> SELL_ONLY
TELEGRAM_WATCHDOG_SEC = 30       # how often the core checks the poller
AGENT_STALE_SEC = 2700           # no completed research cycle -> alert
AGENT_WATCHDOG_SEC = 300
TRACK_WINDOW_SEC = 72 * 3600     # forecast resolution horizon (1-3 day thesis)
TRACK_BATCH = 60                 # forecasts sampled per pass
TRACK_INTERVAL_SEC = 900
RECON_POSITIONS_SEC = 1800       # position-vs-venue reconciliation
POSITION_DRIFT_PCT = 0.02        # book vs venue mismatch worth alerting on
TIME_STOP_SLACK = 1.5            # reassess at this multiple of the predicted window
TIME_STOP_DEFAULT_SEC = 72 * 3600
LIQ_DRAIN_WARN = 0.30            # pool liquidity down vs entry -> urgent reeval
LIQ_DRAIN_EXIT = 0.50            # -> exit evaluation, default exit

# --- go-live phases (go-live skill) -----------------------------------------
# phase: 0 = wiring (no orders), 1 = venue-minimum sizing, 2..4 = 25/50/100%
PHASE_SIZE_FACTOR = {0: 0.0, 1: 0.0, 2: 0.25, 3: 0.50, 4: 1.00}
PHASE1_ORDER_USD = 5.0           # venue-minimum sizing for phase 1

VENUES = {"coinbase": "cex", "solana": "chain", "base": "chain"}
CHAIN_GAS_TOKEN = {"solana": "SOL", "base": "ETH"}

# --- transaction safety -----------------------------------------------------
# Both DEX paths sign transactions built by a third party. Simulate first and
# refuse anything that reverts; allowlist the contract we grant approval to.
SIMULATE_BEFORE_SEND = env("SIMULATE_BEFORE_SEND", "1") not in ("0", "false", "")
EVM_ROUTER_ALLOWLIST = [
    a.strip() for a in env(
        "EVM_ROUTER_ALLOWLIST",
        "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"  # KyberSwap MetaAggregationRouterV2
    ).split(",") if a.strip()
]
