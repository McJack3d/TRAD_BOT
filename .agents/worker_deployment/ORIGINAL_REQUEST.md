## 2026-06-08T14:06:36Z
Add a main CLI entrypoint (`if __name__ == "__main__":` block and `main()` function) to `src/strategy/regime_live.py` so that it can be run continuously as a daemon. Create the systemd service file `deploy/systemd/regime-bot.service` (mirroring `deploy/systemd/bot.service` but configured for the regime-switching strategy daemon).
Write findings, implemented entrypoint code, systemd config, and results to `/Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_deployment/handoff.md`.
