/**
 * Compatibility shim — original ChannelIcon is now a thin wrapper over
 * the personality-rich ChannelAvatar (see channel-avatar.tsx). Keep the
 * old import path alive so in-flight pages don't break during the sweep
 * to the new primitives.
 *
 * Behaviorally identical to ChannelAvatar with `forceFallback={true}`,
 * which preserves the monogram-only look that the old square chip had.
 * New code should import ChannelAvatar directly so it gets the real YT
 * avatar treatment.
 */
import { ChannelAvatar } from "@/components/app/channel-avatar";
import { channelLabel as _channelLabel } from "@/components/app/channel-meta";

interface ChannelIconProps {
  channel: string;
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function ChannelIcon({ channel, size = "md", className }: ChannelIconProps) {
  return <ChannelAvatar channel={channel} size={size} className={className} />;
}

export const channelLabel = _channelLabel;
