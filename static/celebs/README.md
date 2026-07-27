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

## Licensing — record it in credits.json

Most Wikimedia Commons celebrity portraits are **CC BY-SA**, which requires
attribution *and* share-alike. The owner has explicitly accepted that exposure
for this format, so a CC BY-SA portrait is fine to use. Prefer public domain,
CC0 or plain CC BY when one of comparable quality exists — several entries here
were swapped to a non-share-alike file for exactly that reason — but do not
leave a cover face-less over it.

Every image gets an entry in `credits.json`, keyed by the same id:

    "emma-watson-brown": {
      "license": "CC BY 3.0",
      "author":  "David Shankbone",
      "source":  "https://commons.wikimedia.org/wiki/File:..."
    }

`app.celeb_photo_credit()` composes the on-slide credit line from those three
fields ("Photo: David Shankbone / CC BY 3.0 / Wikimedia Commons") and renders it
small and grey along the bottom edge of the cover, which is what satisfies the
BY obligation. Public-domain and CC0 files name no author and get the shorter
"Photo: CC0 via Wikimedia Commons".

A missing credits entry does **not** suppress the photo — the cover still shows
it, minus the credit line. The entry is cheap; write it.

## Crops

The cover renders the file as a 380px circle at 2x, so the useful floor is a
~760px square crop centred on the face. Square the image yourself before
dropping it in: the slide does `background-size:cover` on an already-square
file, so a full-body source becomes a face the size of a pea.
