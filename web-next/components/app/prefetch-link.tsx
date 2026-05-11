"use client";

// Drop-in replacement for next/link that prefetches the destination
// page's API data in addition to its JS bundle.
//
// next/link already prefetches the route's JS chunk on hover (when
// `prefetch` is not false). What it does NOT do is prefetch the data
// the page will fetch on mount. That's what we add here: when the
// user hovers, focuses, or starts a pointer-down on the link, we
// look up the destination href in the ROUTE_PREFETCHES table and
// fire prime() for each (key, fetcher) pair.
//
// Because prime() is idempotent + dedupes in-flight requests, calling
// this on every hover is safe and cheap. The 30 s freshness gate
// inside prime() means a hover during an active session usually
// returns immediately from cache without firing the network.

import Link, { type LinkProps } from "next/link";
import { type ComponentProps, type MouseEvent, type FocusEvent, type PointerEvent } from "react";
import { getRoutePrefetches } from "@/lib/cache-keys";
import { prime } from "@/lib/use-swr-cache";

type PrefetchLinkProps = LinkProps &
  Omit<ComponentProps<"a">, keyof LinkProps> & {
    children: React.ReactNode;
  };

function fireDataPrefetch(href: string): void {
  const entries = getRoutePrefetches(href);
  for (const entry of entries) {
    void prime(entry.key, entry.fetcher).catch(() => undefined);
  }
}

export function PrefetchLink({
  href,
  onMouseEnter,
  onFocus,
  onPointerDown,
  ...rest
}: PrefetchLinkProps) {
  const hrefStr = typeof href === "string" ? href : (href as { pathname?: string }).pathname ?? "";

  return (
    <Link
      href={href}
      onMouseEnter={(ev: MouseEvent<HTMLAnchorElement>) => {
        if (hrefStr) fireDataPrefetch(hrefStr);
        onMouseEnter?.(ev);
      }}
      onFocus={(ev: FocusEvent<HTMLAnchorElement>) => {
        if (hrefStr) fireDataPrefetch(hrefStr);
        onFocus?.(ev);
      }}
      onPointerDown={(ev: PointerEvent<HTMLAnchorElement>) => {
        if (hrefStr) fireDataPrefetch(hrefStr);
        onPointerDown?.(ev);
      }}
      {...rest}
    />
  );
}
