# TEV room proxy

`noema/room_proxy.py` lets the TEV Mac app open the dashboard with no login for the owner.

    TEV.app (~/.tev-url = https://<node>.<tailnet>.ts.net/trading-room)
      -> tailscale serve (HTTPS, tailnet only) -> 127.0.0.1:8788 room proxy -> 127.0.0.1:8787 dashboard

* Bypass only for `Tailscale-User-Login` in `NOEMA_ROOM_PROXY_TRUSTED_LOGINS` (default `gryszzz@github`),
  from a loopback peer, not Funnel. tailscale serve sets that header itself and drops client copies.
* Everyone else gets HTTP Basic auth (`NOEMA_CONSOLE_USERNAME/PASSWORD`, else a random password in
  `$NOEMA_RUN_DIR/room-proxy.secret`, mode 600).
* Started by `scripts/run-room-proxy.sh` from start.sh every watchdog cycle; heartbeat `agent:proxy`.
* Never enable Funnel for this node.
