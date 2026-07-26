# Celebrity cover photos

Drop one image per entry in `auto_content/celebs.json`, named by that entry's
`id`:

    static/celebs/<id>.jpg      (.jpeg / .png / .webp also work)

e.g. `static/celebs/emma-watson-brown.jpg`

`/title-celeb/export` picks these up automatically — `app.celeb_photo_url()`
probes the filesystem, so adding a file is the whole install step. There is no
path field in celebs.json on purpose: a dataset that names a file we never added
would render a broken-image box onto a published slide.

**Missing photo is safe.** The cover falls back to the school logo alone,
centered, and the generator prints a `no photo for <id>` note. It never renders
a placeholder face — a stand-in portrait under "CAN <REAL PERSON> GET INTO X?"
would be worse than no portrait at all.

## What to use

- Square-ish, face centered and near the top (the slide crops to a circle with
  `background-position: center top`).
- ~800x800 or larger. Smaller upscales badly at the 2x render.
- Public promotional or Wikimedia-style images. No watermarked stock.

## Licensing — check before you add

Most Wikimedia Commons celebrity portraits are **CC BY-SA**, which requires
attribution *and* share-alike. That is a real obligation on a branded commercial
carousel, and the slide has nowhere good to put a credit line. Prefer public
domain or an image we have explicit rights to; if a CC BY-SA image is used
anyway, that is a deliberate call to make with the attribution handled, not a
default.
