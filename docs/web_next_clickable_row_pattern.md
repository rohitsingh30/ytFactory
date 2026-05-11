# Clickable-row pattern for drawer-bearing list pages (web-next)

**Rule (added 2026-05-11 after the Burner Channels regression):**

When a `/app/<page>` shows a list of rows AND a side drawer with the
row's detail / live state, **every interaction on a row that has no
other primary destination must open that drawer**. That includes:

1. **The row body** itself — clicking the icon / title / metadata
   line should open the drawer, not be inert.
2. **Every action button on the row** — Subscribe / Cross-engage /
   View live / Last run / **Stop** / and any future addition. Stop
   in particular MUST pop the drawer first so the operator can watch
   the worker wind down (phase → "stopped", tabs closing, …) instead
   of staring at a row with no feedback.
3. **The error path of any action** — if the API call to start /
   stop / mutate fails, still call `onOpen()` so the drawer surfaces
   the prior known state (which is often *why* the new request
   failed).

The single source of truth is the same `onOpen()` callback the parent
page passes to the row. Rows never own drawer state themselves.

## Reference implementation

`web-next/app/app/burner-channels/page.tsx` `BurnerRow` is the reference:

```tsx
function BurnerRow({ burner, onOpen, onAfterAction }: { ... }) {
  async function start(mode) {
    setBusy(true);
    try {
      const r = await burnerApi.start(burner.slug, mode);
      // ... toast ...
      onOpen();          // ← happy path
      onAfterAction();
    } catch (e) {
      onOpen();          // ← error path (prior state still useful)
      toast.error("Couldn't start", { description: ... });
    } finally { setBusy(false); }
  }

  async function stop() {
    setBusy(true);
    onOpen();            // ← BEFORE the mutation, not after
    try { await burnerApi.stop(burner.slug); ... }
    catch (e) { ... }
    finally { setBusy(false); }
  }

  return (
    <div className="...row-shell...">
      {/* Body — div + role="button" so nested <a> stays valid HTML */}
      <div
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault(); onOpen();
          }
        }}
        className="flex cursor-pointer items-center gap-3 ..."
        aria-label={`Open engage panel for ${burner.title}`}
      >
        {/* icon, title, slug, email, last_action_msg */}
        {burner.channel_id && (
          <a
            href={`https://youtube.com/channel/${burner.channel_id}`}
            target="_blank" rel="noreferrer"
            onClick={(e) => e.stopPropagation()}   // ← critical
            ...
          >YouTube</a>
        )}
      </div>
      {/* Action buttons (siblings, not children of the body) */}
      <div className="flex items-center gap-2"> ... </div>
    </div>
  );
}
```

## HTML gotcha — nested `<a>` inside `<button>` is invalid

Don't wrap the whole row body in `<button>` — that breaks the moment
the row contains an external `<a>` (e.g., the YouTube channel link
above). Browsers render it but the spec disallows it and React
hydration warnings surface in the console. Use a `<div role="button"
tabIndex={0}>` with an `onKeyDown` handler that fires on Enter/Space
so keyboard activation matches a real button.

For nested anchors / interactive elements inside the clickable body,
add `onClick={(e) => e.stopPropagation()}` so clicking them goes to
their own destination, not also to `onOpen()`.

## Pages this rule applies to (audit 2026-05-11)

| Page | Drawer-bearing? | Compliant? |
|---|---|---|
| `/app/burner-channels` | yes (`EngageDrawer`) | ✅ since 2026-05-11 |
| `/app/queue` (Held column) | yes (`HeldCritiqueDialog`) | ✅ — row IS the `<button>` (no nested anchors) |
| `/app/queue` (Running/Queued/Completed columns) | no — rows navigate to `/app/render/<id>` | n/a (different pattern) |
| `/app/channels` | no — rows navigate to `/app/channels/<key>` | n/a |

When you add a NEW drawer-bearing list page (next likely candidate:
when burner detail grows into a full inspector), apply this rule
from the start.

## Related

- `docs/burner_channels.md` — the page where this regression surfaced.
- `docs/website_personality.md` Layer 2 / Layer 3 — UI primitives
  this pattern composes with.
- `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_web_next_clickable_row_drawer.md`
  — terse memory pointer.
