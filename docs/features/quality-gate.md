# Quality Gate

The quality gate protects against false alerts caused by the justUp! host's own
network problems. When its connectivity is poor, due checks are paused instead of
raising DOWN alerts.

## How it works

- Every 30 s justUp! probes its reference targets (five by default, at least
  three) and computes a smoothed score.
- If the score falls below the minimum, due checks are paused and affected
  targets show `unknown`. `unknown` never alerts.
- When the score recovers, checks resume.

The gate state and its history are shown in a dashboard widget.

## Settings

Settings > Network, applied without restart:

| Setting | Default | Purpose |
| --- | --- | --- |
| Probe Targets | `https://1.1.1.1`, `8.8.8.8`, `9.9.9.9`, `https://www.de-cix.net`, `https://cloudflare.com` | Hosts to probe (3 to 20) |
| Minimum Score | 70 | Below this score checks pause (0-100) |
| RTT Threshold | 500 ms | Round-trip time at which the latency part of the score reaches zero (1-10000) |

See [Settings](../configuration/settings.md).
