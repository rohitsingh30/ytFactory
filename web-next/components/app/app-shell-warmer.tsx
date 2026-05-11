"use client";

// App-shell warm-fetch. Mounted once at the top of /app/layout.tsx; on
// first mount, fires the lightweight dashboard-class endpoints in
// parallel and primes the SWR cache. By the time the user clicks
// anything in the sidebar, the data is already there — every
// subsequent navigation paints instantly.
//
// Notes:
//   - This component renders nothing.
//   - It only fires on the user's FIRST app-shell mount in this tab,
//     UNLESS the cached entries are older than 30 s (in which case
//     prime() would refetch anyway).
//   - It loads whoami first to decide whether to also warm admin-only
//     endpoints. Whoami itself is primed so the sidebar's own
//     useEffect fetch hits the cache.

import { useEffect, useRef } from "react";
import {
  APP_SHELL_WARM_KEYS,
  APP_SHELL_WARM_KEYS_ADMIN,
  CK,
} from "@/lib/cache-keys";
import { prime } from "@/lib/use-swr-cache";
import { api } from "@/lib/api";

interface WhoAmI {
  signed_in: boolean;
  is_admin?: boolean;
}

export function AppShellWarmer(): null {
  const fired = useRef(false);

  useEffect(() => {
    if (fired.current) return;
    fired.current = true;

    // Always warm whoami first so the sidebar's own fetch hits cache.
    // Resolve admin-ness from it before deciding which keys to warm.
    void prime<WhoAmI>(CK.whoami, () => api.get<WhoAmI>("/api/auth/whoami"))
      .then((me) => {
        // Fire the base set in parallel, ignoring rejections.
        for (const entry of APP_SHELL_WARM_KEYS) {
          void prime(entry.key, entry.fetcher).catch(() => undefined);
        }
        if (me?.is_admin) {
          for (const entry of APP_SHELL_WARM_KEYS_ADMIN) {
            void prime(entry.key, entry.fetcher).catch(() => undefined);
          }
        }
      })
      .catch(() => {
        // Whoami failed (signed-out / network) — still warm the public
        // dashboard-class endpoints. They'll either succeed or set an
        // error chip on the page.
        for (const entry of APP_SHELL_WARM_KEYS) {
          void prime(entry.key, entry.fetcher).catch(() => undefined);
        }
      });
  }, []);

  return null;
}
