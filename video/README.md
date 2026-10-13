# Mac MCP product video

The 40-second video on the product page and in the README, made with [Remotion](https://www.remotion.dev/).
It is a development tool only: the app, the installer and the updater never run npm or use this folder.

```bash
cd video
npm ci
npm run studio   # preview and edit
npm run render   # out/mac-mcp-demo.mp4, 1280×720 H.264
npm run still    # out/demo-poster.jpg
npm run export   # copies both into site/assets/media/ (fails if over budget)
```

The scenes redraw Mac MCP's own screens with neutral glyphs (no vendor artwork). Every UI label the
video shows is listed in `src/ui-strings.json`, and `tests/test_site.py` checks each one exists in the app.
Captions are in `site/assets/media/demo.en.vtt`; keep them in step with `src/scenes.tsx`.

Remotion is free for individuals and companies of up to three people; larger companies need a
[company license](https://www.remotion.dev/license).
