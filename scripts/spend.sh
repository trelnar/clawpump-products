#!/bin/bash
# Agent API spend, from journaled token usage. Run as root on the VPS.
# Prices: claude-opus-5 $5/M input, $25/M output, cache read 0.1x, 1h write 2x.
sqlite3 /var/lib/tradebot/tradebot.db "
SELECT
  date(ts,'unixepoch') AS day,
  COUNT(*) AS cycles,
  ROUND(SUM(json_extract(detail,'\$.in'))/1e6*5.0
      + SUM(json_extract(detail,'\$.out'))/1e6*25.0
      + SUM(json_extract(detail,'\$.cache_read'))/1e6*0.5
      + SUM(json_extract(detail,'\$.cache_write'))/1e6*10.0, 3) AS usd,
  SUM(json_extract(detail,'\$.cache_read')) AS cached_tokens
FROM events WHERE kind='agent_usage'
GROUP BY day ORDER BY day DESC LIMIT 7;"
