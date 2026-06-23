# Custimoo Defect Report

Internal defect report dashboard — generates from RDS database and deploys to fly.io.

- Lives at: https://custimoo-defect-report-lars.fly.dev/
- Auto-updates weekdays at 08:00 and 16:00 UTC via GitHub Actions
- Manual trigger: `Actions → Generate & Deploy Report → Run workflow`

Deploy one-off: `flyctl deploy -a custimoo-defect-report-lars --remote-only`
