"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Cloud,
  LayoutDashboard,
  ListChecks,
  Settings as SettingsIcon,
  Shield,
  ToggleRight,
  Tv2,
  Wand2,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  shortcut?: string;
  /** When true, only render if the signed-in user is admin. */
  adminOnly?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/app", label: "Dashboard", icon: LayoutDashboard, shortcut: "G D" },
  { href: "/app/create", label: "Create", icon: Wand2, shortcut: "C" },
  { href: "/app/queue", label: "Queue", icon: ListChecks, shortcut: "G Q" },
  { href: "/app/channels", label: "Channels", icon: Tv2, shortcut: "G C" },
  { href: "/app/burner-channels", label: "Burner Channels", icon: ToggleRight, shortcut: "G B" },
  { href: "/app/cloud", label: "Cloud", icon: Cloud, shortcut: "G I", adminOnly: true },
  { href: "/app/admin", label: "Admin", icon: Shield, shortcut: "G A", adminOnly: true },
  { href: "/app/settings", label: "Settings", icon: SettingsIcon, shortcut: "G S" },
];

interface WhoAmI {
  signed_in: boolean;
  email?: string;
  name?: string;
  picture?: string;
  status?: string;
  is_admin?: boolean;
}

export function Sidebar({ className }: { className?: string }) {
  const pathname = usePathname() ?? "/app";
  const [me, setMe] = useState<WhoAmI | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<WhoAmI>("/api/auth/whoami")
      .then((res) => {
        if (!cancelled) setMe(res);
      })
      .catch(() => {
        if (!cancelled) setMe({ signed_in: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const items = NAV_ITEMS.filter((item) => !item.adminOnly || me?.is_admin);
  const initials =
    (me?.name || me?.email || "?").trim().charAt(0).toUpperCase() || "?";
  const subtitle = me?.signed_in ? (me.is_admin ? "admin" : me.status ?? "user") : "signed out";

  return (
    <aside
      className={cn(
        "flex h-full w-56 flex-col border-r border-border bg-surface",
        className,
      )}
    >
      <div className="flex h-12 items-center px-3">
        <Link
          href="/"
          className="flex items-center gap-2 rounded-md px-1.5 py-1 text-[13px] font-medium tracking-tight text-foreground hover:bg-surface-2"
        >
          <span className="grid h-5 w-5 place-items-center rounded-[5px] border border-border bg-background font-mono text-[9px]">
            yt
          </span>
          ytFactory
        </Link>
      </div>

      <nav className="flex-1 space-y-px px-2 pb-2 pt-1">
        {items.map((item) => {
          const active =
            item.href === "/app"
              ? pathname === "/app"
              : pathname === item.href || pathname.startsWith(item.href + "/");
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "group flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] tracking-tight transition-colors",
                active
                  ? "bg-surface-2 text-foreground"
                  : "text-muted-foreground hover:bg-surface-2/60 hover:text-foreground",
              )}
            >
              <item.icon
                className={cn(
                  "h-4 w-4 transition-colors",
                  active ? "text-foreground" : "text-muted-foreground group-hover:text-foreground",
                )}
              />
              <span className="flex-1 truncate">{item.label}</span>
              {item.shortcut && (
                <span className="hidden font-mono text-[10px] text-muted-foreground/70 group-hover:inline">
                  {item.shortcut}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-border px-3 py-3">
        <div className="flex items-center gap-2">
          {me?.picture ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={me.picture}
              alt=""
              className="h-7 w-7 rounded-full border border-border object-cover"
              referrerPolicy="no-referrer"
            />
          ) : (
            <span className="grid h-7 w-7 place-items-center rounded-full bg-surface-2 font-mono text-[10px] text-foreground border border-border">
              {initials}
            </span>
          )}
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12px] font-medium tracking-tight">
              {me?.signed_in ? me?.name || me?.email || "Signed in" : "Signed out"}
            </div>
            <div className="truncate font-mono text-[10px] text-muted-foreground">
              {subtitle}
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
