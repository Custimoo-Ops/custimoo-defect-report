# Custimoo Defect Report

Internal defect report dashboard — generates from RDS database and deploys to fly.io.

- Lives at: https://custimoo-defect-report-lars.fly.dev/
- Fly.io app: `custimoo-defect-report-lars`
- Fly.io org: `custimoo` / Custimoo
- Auto-updates every 2 hours on weekdays from 06:00–18:00 UTC via GitHub Actions
- Manual trigger: `Actions → Generate & Deploy Report → Run workflow`

Deploy one-off: `flyctl deploy -a custimoo-defect-report-lars --remote-only`
