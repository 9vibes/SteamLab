# Store assets

The icon matches KUNAS/Labs' dashboard branding. Gallery screenshots are generated
by the responsive Playwright test with synthetic API fixtures, not personal footage
or a running production stream. They contain no real stream keys or face images.

To regenerate from `frontend/`, with Chromium installed:

```sh
STEAMLAB_SCREENSHOT_DIR=../assets npm test -- --grep 'desktop and mobile'
```

The 1.2.0 multi-view gallery uses four named synthetic feed fixtures and simulated
recording/bitrate state. It contains no personal footage or real publishing keys.

```sh
STEAMLAB_SCREENSHOT_DIR=../assets npm test -- --grep 'multi-view release gallery'
```
