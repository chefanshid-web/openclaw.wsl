# Specification: Freqtrade Trade Management & Bot Lifecycle Controls

## 1. Executive Summary

This specification documents the behavioral architecture, data contracts, and control flows of two core subsystems reverse-engineered from **FreqUI** (the official web frontend for Freqtrade):
1. **Trade Management & Order Execution** (Active trade monitoring, manual exit, partial exit, position adjustment/entry, trade deletion, order cancellation, and reload).
2. **Bot Lifecycle & Control** (Starting, stopping, pausing trade entry, configuration reloading, ping/health checks, and multi-tier state synchronization).

All findings have been extracted through static AST analysis and dependency graph reconstruction using Reverse Engineer Anything (REA) MCP tools, augmented by clean-room behavioral specification without verbatim code reproduction.

---

## 2. REA Evidence & Methodology Ledger

### 2.1 Evidence Identifiers
| Evidence Identifier | Subject / Artifact | Analysis Operation | REA Provider / Profile | Authority / Confidence |
| :--- | :--- | :--- | :--- | :--- |
| `ev_88abc579e3eeb4189f37eee76de121a780f4a622ca95f5a83a0895890f2ad542` | `target-app` (FreqUI root) | `analyze_javascript_application` | `rea-javascript-artifact-reconstruction` | `shipped-artifact` / Complete graph |
| `ev_c49852e7336c3aafe778b0fbe47d54531d5740baaeb078cf5501c61de482232f` | `target-app` (Root SHA: `76f944d...`) | `inspect_analysis_view` (summary) | `rea-analysis-view` | `derived` / Exact AST metric |
| `ev_58fcf172cdc37f56f62be570ba0eb207e2e52abe44ac725c971f59c6e4c98d62` | Module Collection (Offset 0-64) | `inspect_analysis_view` (page 0) | `rea-analysis-view` | `derived` / Module inventory |
| `ev_8d0ae0acf3793fa084641785780ddd154defac5d1c0f3920e056ee0f11fb6da9` | Module Collection (Offset 64-128) | `inspect_analysis_view` (page 1) | `rea-analysis-view` | `derived` / Module inventory |
| `ev_cddcf1079e884dd13993373a201b6daf78f4870c746df6e1b95603193f53595b` | Feature Seed: `forceexit` | `trace_application_feature` | `rea-javascript-application-workflows` | `derived` / Bidirectional trace |
| `ev_e029964e41146b161c2b1d5f0148fbcdacaa0fcc76a0e914c52e733ec2d64118` | Feature Seed: `reload_config` | `trace_application_feature` | `rea-javascript-application-workflows` | `derived` / Bidirectional trace |

### 2.2 Evidence Discipline (Observed vs. Inferred)
- **[Observed]**: Concrete syntax, AST relations, HTTP routes, JSON schemas, props, store actions, Vue component templates, and event emitters directly recovered from the source graph.
- **[Inferred]**: Temporal ordering of async UI events, modal display states dependent on runtime user interactions, and Python backend state transitions on the Freqtrade server side.

---

## 3. Subsystem 1: Trade Management & Order Execution

### 3.1 Overview & Responsibilities
The Trade Management subsystem provides real-time tracking of active and historical trades, calculations of profit/loss (ratio, percentage, fiat values), stop-loss metrics, and explicit trade interventions:
- Forcing market or limit exits (complete or partial).
- Entering new positions or increasing existing positions manually.
- Canceling open orders associated with a trade.
- Deleting invalid or desynchronized trade entries from the local database.
- Reloading trade state directly from exchange order records.

### 3.2 Exact Architectural File Map & Responsibilities
- `src/pages/trade.vue`: Primary trade interface rendering responsive grid panes (Charts, Open Trades, Closed Trades, Multi-pane metrics).
- `src/components/ftbot/TradeList.vue`: Data table for active and historical trades supporting row selection, sorting, and contextual action menus.
- `src/components/ftbot/TradeActionsPopover.vue`: Contextual trigger wrapping the action set with popover overlay state management.
- `src/components/ftbot/TradeActions.vue`: Action button cluster with conditional visibility governed by server API capability flags (`botFeatures`).
- `src/components/ftbot/ForceExitForm.vue`: Modal interface for specifying exit order type (`limit` vs `market`), partial exit amount, and limit price.
- `src/components/ftbot/ForceEntryForm.vue`: Modal interface for manual trade entry or position scaling (pair, order type, side, stake amount, leverage, entry tag).
- `src/components/ftbot/TradeDetail.vue`: High-resolution trade inspection panel displaying risk analysis, stoploss distances, order breakdowns, and custom trade metadata.
- `src/composables/useForceTrade.ts`: Dynamic overlay factory generating promise-wrapped modal instances for force exit/entry forms.
- `src/stores/ftbot.ts`: Bot sub-store handling trade data structures, HTTP endpoint invocation, and cache invalidation.
- `src/stores/ftbotwrapper.ts`: Multibot orchestrator aggregating trade requests and routing actions to the active sub-store.
- `src/composables/api.ts`: HTTP client built on `ofetch` with bearer token injection, automated 401 refresh, and error classification.

### 3.3 Call Stack & End-to-End Traces

#### Trace A: Forced Trade Exit (Full or Partial)
```
[User Action in TradeList / TradeActionsPopover / TradeActions]
   │
   ▼
TradeActions.vue -> emits 'forceExit' or 'forceExitPartial'
   │
   ▼
TradeList.vue -> forceExitHandler() / forceExitPartialHandler()
   │
   ├── [If partial exit requested]:
   │      useForceTrade.ts -> forceExitDialog()
   │         │
   │         ▼
   │      ForceExitForm.vue -> user specifies amount, ordertype, optional price
   │         │
   │         ▼
   │      ForceExitForm.vue -> handleExit()
   │         │
   │         ▼
   │      ftbot.ts -> forceexit(payload: ForceExitPayload)
   │
   └── [If instant exit requested]:
          settingsStore confirmation dialog
          │
          ▼
          ftbotwrapper.ts -> forceSellMulti(payload: MultiForceExitPayload)
             │
             ▼
          ftbot.ts -> forceexit(payload: ForceExitPayload)
             │
             ▼
          api.ts -> POST /api/v1/forcesell
             │
             ▼
          showAlert("Exit order for <tradeid> created", "success")
```

#### Trace B: Manual Trade Entry / Position Increase
```
[User Action in BotControls or TradeActions ("Increase position")]
   │
   ▼
useForceTrade.ts -> forceEntryDialog({ pair, positionIncrease })
   │
   ▼
ForceEntryForm.vue -> User enters pair, side, stake amount, price, leverage, tag
   │
   ▼
ForceEntryForm.vue -> handleEntry()
   │
   ▼
ftbot.ts -> forceentry(payload: ForceEnterPayload)
   │
   ▼
api.ts -> POST /api/v1/forcebuy
   │
   ▼
showAlert("Order for <pair> created.", "success")
```

#### Trace C: Trade Deletion & Open Order Cancellation
```
[User Action in TradeActions ("Delete trade" or "Cancel open orders")]
   │
   ▼
TradeList.vue -> removeTradeHandler() or cancelOpenOrderHandler()
   │
   ▼
useConfirmBox.ts -> Modal confirmation: "This action cannot be undone."
   │
   ▼
ftbotwrapper.ts -> deleteTradeMulti() or cancelOpenOrderMulti()
   │
   ▼
ftbot.ts -> deleteTrade(tradeid) or cancelOpenOrder(tradeid)
   │
   ├── DELETE /api/v1/trades/{tradeid}
   └── DELETE /api/v1/trades/{tradeid}/open-order
```

### 3.4 Data Contracts & Schemas

#### Input: `ForceExitPayload`
- `tradeid`: string | number (Required. Trade identifier or `"all"` for bulk liquidation).
- `ordertype`: string (Optional: `"limit"` | `"market"`, default resolved from bot configuration).
- `amount`: number (Optional: partial exit volume in base currency; defaults to full owning balance).
- `price`: number (Optional: limit exit price, only permitted if feature `forceExitWithPrice` is active).

#### Input: `ForceEnterPayload`
- `pair`: string (Required: trading pair, e.g., `"BTC/USDT"`).
- `side`: `"long"` | `"short"` (Optional: position side, enabled only when `forceEnterShort` and `short_allowed` are true).
- `price`: number (Optional: limit purchase price).
- `ordertype`: string (Optional: `"limit"` | `"market"`).
- `stakeamount`: number (Optional: currency amount to invest; defaults to strategy configured size).
- `entry_tag`: string (Optional: strategy entry label, default `"force_entry"`).
- `leverage`: number (Optional: leverage multiplier for margin/futures trades).

#### Output: `Trade` Object (Primary Fields)
- `trade_id`: Unique numeric trade ID.
- `pair`: Asset symbol pair.
- `is_open`: Boolean indicating whether position is active.
- `amount`: Current held quantity of base currency.
- `stake_amount`: Capital committed in quote/stake currency.
- `open_rate`: Price at which entry executed.
- `current_rate`: Real-time market valuation of asset.
- `close_rate`: Exit price (for closed trades).
- `profit_ratio`: Floating-point profit relative to stake (e.g. `0.05` for +5%).
- `profit_pct`: Percentage profit representation.
- `stop_loss_abs`: Absolute price boundary for stop-loss trigger.
- `stop_loss_ratio`: Stop-loss threshold ratio relative to entry.
- `stoploss_current_dist`: Distance between current market rate and stop-loss level.
- `orders`: Array of associated exchange orders (`BTOrder` / `Order`) detailing fill quantities, fees, and sides.

### 3.5 Edge Cases & Business Constraints
1. **Feature-Gated API Parameters**:
   - `forceExitParams` (min API v1.2): Enables selecting between limit and market exit orders.
   - `forceExitWithPrice` (min API v2.45): Unlocks the explicit price input field in limit exit requests.
   - `forceEntryTag` (min API v2.17): Allows custom entry tagging and triggers partial exit support.
   - `cancelOpenOrders` (min API v2.24): Enables canceling open exit/entry orders directly.
2. **Precision Rounding**:
   - Amounts and prices must conform to exchange step rules: formatted dynamically using `trade.amount_precision` and `trade.price_precision`.
3. **Optimistic Invalidation**:
   - When `/status` returns an array of trades differing in length or ID sequence from the locally cached `openTrades`, `refreshRequired` is flagged, initiating a slow refresh sequence to retrieve full state.

---

## 4. Subsystem 2: Bot Lifecycle & Control

### 4.1 Overview & Responsibilities
The Bot Lifecycle subsystem manages the operational loop of the Freqtrade daemon:
- Starting the trading loop (`running` state).
- Stopping the execution loop (`stopped` state, terminating active polling and order handling).
- Pausing entry execution (`stopbuy` state, maintaining open trade management without taking new positions).
- Dynamically reloading configuration and strategy files from disk.
- Liveness detection and automatic reconnection through heartbeat polling and WebSocket event propagation.

### 4.2 Exact Architectural File Map & Responsibilities
- `src/components/ftbot/BotControls.vue`: Primary control strip housing Play, Stop, Pause (StopBuy), Reload Config, and ForceExit All action buttons.
- `src/components/ftbot/BotStatus.vue`: Status pane displaying bot state (`running` vs `stopped`), strategy name, exchange, trading mode, and performance summary.
- `src/components/ftbot/ReloadControl.vue`: Header widget providing global auto-refresh toggling and manual refresh triggers.
- `src/stores/ftbot.ts`: Core state holder storing `botState`, connection statuses (`isBotOnline`, `isBotLoggedIn`), and dispatching lifecycle API calls.
- `src/stores/ftbotwrapper.ts`: Root coordinator managing interval timers (`refreshInterval`, `refreshIntervalSlow`), WebSocket lifecycle, and multi-bot synchronization.
- `src/composables/api.ts`: API middleware that intercepts 401s, attempts token refresh, and triggers `markBotOffline()` upon network or server failures.

### 4.3 Call Stack & End-to-End Traces

#### Trace A: Bot State Change (Start / Stop / StopBuy)
```
[User Action in BotControls.vue (Click Play / Stop / Pause)]
   │
   ▼
BotControls.vue -> handleStopBot() / handleStopBuy() / startBot()
   │
   ├── [Confirmation Dialog]: Prompt user before terminating daemon
   │
   ▼
ftbot.ts -> startBot() / stopBot() / stopBuy()
   │
   ├── [api.ts]: POST /api/v1/start   (body: {})
   ├── [api.ts]: POST /api/v1/stop    (body: {})
   └── [api.ts]: POST /api/v1/stopbuy (body: {})
   │
   ▼
ftbot.ts receives StatusResponse: { status: "..." }
   │
   ▼
ftbot.ts -> showAlert(data.status)
```

#### Trace B: Configuration Reload
```
[User Action in BotControls.vue ("Reload Config")]
   │
   ▼
BotControls.vue -> handleReloadConfig()
   │
   ├── [Confirmation]: "Reload configuration (including strategy)?"
   │
   ▼
ftbot.ts -> reloadConfig()
   │
   ▼
api.ts -> POST /api/v1/reload_config (body: {})
   │
   ▼
ftbot.ts -> showAlert(res.status) -> Toast: "Config reloaded successfully."
```

#### Trace C: Multi-Tier Synchronization & Liveness Architecture
```
[Background Timer in ftbotwrapper.ts]
   │
   ├── [Every 5,000ms]: allRefreshFrequent()
   │      │
   │      ▼
   │   ftbot.ts -> refreshFrequent()
   │      ├── getOpenTrades() -> GET /api/v1/status
   │      └── getLocks()      -> GET /api/v1/locks
   │
   └── [Every 60,000ms]: allRefreshSlow()
          │
          ▼
       ftbot.ts -> refreshSlow(forceUpdate = false)
          ├── getState()           -> GET /api/v1/show_config
          ├── getProfit()          -> GET /api/v1/profit_all (or /profit)
          ├── getTrades()          -> GET /api/v1/trades?limit=500&offset=0
          ├── getBalance()         -> GET /api/v1/balance
          ├── updateWalletChange() -> GET /api/v1/historic_balance
          ├── getWhitelist()       -> GET /api/v1/whitelist
          ├── getBlacklist()       -> GET /api/v1/blacklist
          └── getCurrentStrategy() -> GET /api/v1/strategy/{name}
```

#### Trace D: Real-Time Event Subscription (WebSocket)
```
ftbot.ts -> getState() receives successful config
   │
   ▼
ftbot.ts -> startWebSocket()
   │
   ▼
useWebSocket connects: {baseWsUrl}/message/ws?token={accessToken}
   │
   ├── Subscriptions sent on connect:
   │      - 'whitelist'
   │      - 'entry_fill'
   │      - 'exit_fill'
   │      - 'entry_cancel'
   │      - 'exit_cancel'
   │      - 'new_candle' (if websocketNewCandle feature enabled)
   │
   ▼
Incoming message: _handleWebsocketMessage(event)
   ├── entryFill / exitFill: triggers showNotification(msg)
   ├── whitelist: updates local whitelist array
   └── newCandle: triggers getPairCandles() for active chart pairs
```

### 4.4 Data Contracts & Schemas

#### Data Model: `BotState` (Response of `GET /api/v1/show_config`)
- `version`: string (Freqtrade engine release version, e.g. `"2022.8"`).
- `api_version`: number (Float API version used for feature gating, e.g. `2.50`).
- `state`: `"running"` | `"stopped"` | `"reload_config"`.
- `runmode`: `"live"` | `"dry_run"` | `"backtest"` | `"webserver"`.
- `dry_run`: boolean (True if operating in paper trading mode).
- `trading_mode`: `"spot"` | `"margin"` | `"futures"`.
- `short_allowed`: boolean.
- `stake_currency`: string (e.g., `"USDT"`).
- `stake_amount`: string | number.
- `available_capital`: number.
- `max_open_trades`: number.
- `stoploss`: number.
- `trailing_stop`: boolean.
- `force_entry_enable`: boolean (Enables or disables manual force entry in the UI).
- `order_types`: Dictionary defining default order behavior (`entry`, `exit`, `emergency_exit`, `force_exit`, `force_entry`, `stoploss`).

#### Lifecycle API Endpoints Summary
| HTTP Verb | Path | Request Body | Response Payload | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/ping` | None | `{ status: "pong" }` | Bot heartbeat check; marks bot online/offline |
| `GET` | `/api/v1/show_config` | None | `BotState` JSON object | Comprehensive bot running configuration |
| `POST` | `/api/v1/start` | `{}` | `{ status: string }` | Transitions bot loop from stopped to running |
| `POST` | `/api/v1/stop` | `{}` | `{ status: string }` | Suspends all bot trading operations |
| `POST` | `/api/v1/stopbuy` | `{}` | `{ status: string }` | Pauses new position entry; maintains open trades |
| `POST` | `/api/v1/reload_config`| `{}` | `{ status: string }` | Re-reads configuration and active strategy files |
| `GET` | `/api/v1/status` | None | `Trade[]` | Retrives all currently open positions |
| `GET` | `/api/v1/trades` | Query: `limit`, `offset` | `TradeResponse` | Paginated closed trade history |
| `POST` | `/api/v1/forcesell` | `ForceExitPayload` | `StatusResponse` | Requests manual liquidation of trade(s) |
| `POST` | `/api/v1/forcebuy` | `ForceEnterPayload` | `StatusResponse` / `Trade` | Requests manual entry for a pair |
| `DELETE` | `/api/v1/trades/{id}` | None | `DeleteTradeResponse` | Deletes a trade record from the bot database |
| `DELETE` | `/api/v1/trades/{id}/open-order` | None | `DeleteTradeResponse` | Cancels any unfilled open order for a trade |
| `POST` | `/api/v1/trades/{id}/reload` | None | `Trade` | Queries exchange and updates trade data |

### 4.5 Edge Cases & Fault Recovery
1. **Network Disconnection & Offline Detection**:
   - If `/ping` or a REST call throws a network error or HTTP 500, `markBotOffline()` sets `isBotOnline = false`.
   - When `/ping` subsequently succeeds, `setIsBotOnline(true)` triggers an immediate full refresh sequence (`refreshSlow(true)`).
2. **Session Expiry & Token Refresh**:
   - Upon encountering HTTP 401 Unauthorized, `useApi` intercepts the error, calls `userService.refreshToken()`, and transparently replays the request once with the new access token. If refresh fails, `markBotOffline(true)` marks the bot logged out.
3. **Sequential Pagination for Database Safety**:
   - In `getTrades()`, paginated retrieval (page size 500) executes sequentially via `do...while` rather than parallel `Promise.all` calls, preventing lock contention on the backend SQLite database.
4. **WebSocket Reconnect & Fallback**:
   - If the WebSocket connection drops, `autoReconnect` re-attempts connection every 10,000ms. If WebSockets are unavailable or in `webserver` mode, the app falls back entirely to the 5s/60s polling intervals.

---

## 5. Verification Matrix: Observed Evidence vs. Inferences

| Architectural Feature | Status | REA Evidence Reference / Location | Note |
| :--- | :--- | :--- | :--- |
| `analyze_javascript_application` Graph | **Observed** | `ev_88abc579e3eeb4189f37eee76de121a780f4a622ca95f5a83a0895890f2ad542` | 97 parsed JS/TS files, 38,702 AST nodes |
| `POST /forcesell` endpoint & payload | **Observed** | `src/stores/ftbot.ts:1023-1032`, Trace `ev_cddcf1079e884dd13993373a201b6daf78f4870c746df6e1b95603193f53595b` | Exact route and schema observed in source |
| `POST /reload_config` endpoint | **Observed** | `src/stores/ftbot.ts:967-977`, Trace `ev_e029964e41146b161c2b1d5f0148fbcdacaa0fcc76a0e914c52e733ec2d64118` | Exact route and schema observed in source |
| `TradeActions.vue` conditional buttons | **Observed** | `src/components/ftbot/TradeActions.vue:26-97` | Feature checks for `forceExitParams`, `cancelOpenOrders`, etc. |
| 5s / 60s Polling Loop Architecture | **Observed** | `src/stores/ftbotwrapper.ts:424-437` | Exact interval values observed |
| WebSocket Event Dispatch Mechanism | **Observed** | `src/stores/ftbot.ts:1347-1377` | Message types `entryFill`, `exitFill`, `whitelist`, etc. |
| Execution Order on User Confirmation | **Inferred** | `TradeList.vue:90-115`, `BotControls.vue:13-21` | Dependent on modal acceptance and browser event loop |
| Backend Order Matching Behavior | **Inferred** | Server API responses (`show_config.json`, `trades.json`) | Inferred from REST response contracts; server source is Python |
