/**
 * Per-channel cosmetic metadata — monogram, gradient tones, label.
 * Single source of truth for both ChannelAvatar (fallback colors) and
 * ChannelBanner (gradient when no banner image is mirrored).
 *
 * Tones are intentionally stronger than the old `bg-rose-500/10` pastels
 * so a fallback chip still reads as "real" next to a real avatar.
 */
export interface ChannelTone {
  /** Tailwind utility classes for the avatar fallback chip background. */
  bg: string;
  /** Tailwind utility classes for the avatar fallback chip text. */
  text: string;
  /** Tailwind gradient classes for the banner fallback (full-bleed). */
  banner: string;
  /** Soft accent (used in chips, recent-video tile borders). */
  accent: string;
}

export const CHANNEL_TONE: Record<string, ChannelTone> = {
  mystoriesanimated: {
    bg: "bg-gradient-to-br from-rose-500 to-rose-700",
    text: "text-white",
    banner: "bg-gradient-to-br from-rose-500/40 via-rose-700/20 to-amber-500/30",
    accent: "text-rose-200",
  },
  sportsrecapped: {
    bg: "bg-gradient-to-br from-emerald-500 to-emerald-700",
    text: "text-white",
    banner: "bg-gradient-to-br from-emerald-500/40 via-teal-600/25 to-sky-500/20",
    accent: "text-emerald-200",
  },
  hindutavaanimated: {
    bg: "bg-gradient-to-br from-amber-500 to-orange-700",
    text: "text-white",
    banner: "bg-gradient-to-br from-amber-500/45 via-orange-600/25 to-red-700/20",
    accent: "text-amber-200",
  },
  historyrecapped: {
    bg: "bg-gradient-to-br from-stone-500 to-stone-800",
    text: "text-stone-50",
    banner: "bg-gradient-to-br from-stone-700/40 via-amber-700/20 to-stone-900/40",
    accent: "text-stone-200",
  },
  rhymetimejunction: {
    bg: "bg-gradient-to-br from-pink-500 to-fuchsia-700",
    text: "text-white",
    banner: "bg-gradient-to-br from-pink-500/40 via-fuchsia-600/25 to-violet-500/30",
    accent: "text-pink-200",
  },
  scrollpulse: {
    bg: "bg-gradient-to-br from-orange-500 to-rose-700",
    text: "text-white",
    banner: "bg-gradient-to-br from-orange-500/45 via-rose-600/25 to-fuchsia-500/20",
    accent: "text-orange-200",
  },
  cosmosdecoded: {
    bg: "bg-gradient-to-br from-violet-500 to-blue-700",
    text: "text-white",
    banner: "bg-gradient-to-br from-violet-500/40 via-blue-600/30 to-cyan-500/20",
    accent: "text-violet-200",
  },
  _fallback: {
    bg: "bg-gradient-to-br from-zinc-600 to-zinc-800",
    text: "text-zinc-100",
    banner: "bg-gradient-to-br from-zinc-700/40 to-zinc-900/40",
    accent: "text-zinc-200",
  },
};

const CHANNEL_LABEL: Record<string, string> = {
  mystoriesanimated: "MyStoriesAnimated",
  sportsrecapped: "SportsRecapped",
  hindutavaanimated: "HindutavaAnimated",
  historyrecapped: "History Recapped",
  rhymetimejunction: "Rhyme Time Junction",
  scrollpulse: "ScrollPulse",
  cosmosdecoded: "Cosmos Decoded",
};

const MONOGRAM: Record<string, string> = {
  mystoriesanimated: "MS",
  sportsrecapped: "SR",
  hindutavaanimated: "HA",
  historyrecapped: "HR",
  rhymetimejunction: "RT",
  scrollpulse: "SP",
  cosmosdecoded: "CD",
};

export function channelLabel(channel: string | null | undefined): string {
  if (!channel) return "—";
  return CHANNEL_LABEL[channel] ?? channel;
}

export function channelMonogram(channel: string): string {
  return MONOGRAM[channel] ?? channel.slice(0, 2).toUpperCase();
}

/** Compact, formatted count: 1.2K / 4.5M / 12 */
export function fmtCount(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + "K";
  return String(n);
}
