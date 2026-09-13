# Store assets

The icon matches SteamLab's dashboard branding. Gallery screenshots are generated
by the responsive Playwright test with synthetic API fixtures, not personal footage
or a running production stream. They contain no real stream keys or face images.

To regenerate from `frontend/`, with Chromium installed:

```sh
STEAMLAB_SCREENSHOT_DIR=../assets npm test -- --grep 'desktop and mobile'
```
