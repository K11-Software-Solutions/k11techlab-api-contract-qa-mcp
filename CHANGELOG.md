# Changelog

## 0.1.1 (22 November 2026)

- **Fix:** fields of an endpoint that returns a top-level list (reported as `[].field`) now match consumers that list
  `field` in `reads` or `sends`. Before, such consumers were reported as unaffected by a breaking change.
- Consumer field paths are normalised the same way on both sides: `[].createdAt`, `createdAt` and `items[].sku` / `items.sku`.

## 0.1.0 (12 November 2026)

- First release.
