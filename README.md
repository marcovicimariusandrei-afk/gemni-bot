# Polybot Skuld (v6.5.11)

A Polymarket CLOB bot designed for live trading and realistic dry-run simulations.

## Deployment to Railway

This repository is pre-configured for deployment on Railway via GitHub.

1. Push this repository to GitHub.
2. In Railway, click **New Project** -> **Deploy from GitHub repo** and select this repo.
3. Railway will automatically detect the Python environment via `nixpacks.toml` and `runtime.txt`.
4. Go to the **Variables** tab in Railway and add the variables listed in `.env.example`.
5. Provide your live `PRIVATE_KEY` and `PROXY_WALLET` values. 

**Important:** Leave `MODE=dry` on the first run to ensure the simulation works with live market data before committing real funds.
