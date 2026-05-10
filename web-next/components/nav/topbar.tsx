"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Bell,
  ChevronRight,
  Menu,
  Moon,
  Plus,
  Search,
  Sun,
} from "lucide-react";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { NAV_ITEMS, Sidebar } from "@/components/nav/sidebar";

function crumbs(pathname: string): { label: string; href?: string }[] {
  const out: { label: string; href?: string }[] = [{ label: "Studio", href: "/app" }];
  const match = NAV_ITEMS.slice()
    .reverse()
    .find((n) => (n.href === "/app" ? pathname === "/app" : pathname.startsWith(n.href)));
  if (match && match.href !== "/app") {
    out.push({ label: match.label, href: match.href });
  }
  // Detail segment (e.g., /app/render/abc)
  const segs = pathname.split("/").filter(Boolean);
  if (segs.length > 2 && match) {
    const tail = segs[segs.length - 1];
    if (tail && tail !== match.href.split("/").pop()) {
      out.push({ label: tail });
    }
  }
  return out;
}

export function TopBar() {
  const pathname = usePathname() ?? "/app";
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    const stored = (typeof window !== "undefined" && localStorage.getItem("yt-theme")) as
      | "dark"
      | "light"
      | null;
    if (stored) {
      setTheme(stored);
      document.documentElement.classList.toggle("light", stored === "light");
      document.documentElement.classList.toggle("dark", stored === "dark");
    }
  }, []);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.classList.toggle("light", next === "light");
    document.documentElement.classList.toggle("dark", next === "dark");
    localStorage.setItem("yt-theme", next);
  }

  const trail = crumbs(pathname);

  return (
    <header className="sticky top-0 z-30 flex h-12 items-center gap-2 border-b border-border bg-background/80 px-3 backdrop-blur md:px-4">
      <Sheet>
        <SheetTrigger asChild>
          <Button variant="ghost" size="icon" className="h-8 w-8 md:hidden" aria-label="Open menu">
            <Menu className="h-4 w-4" />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-60 p-0">
          <Sidebar className="border-r-0" />
        </SheetContent>
      </Sheet>

      {/* Breadcrumbs */}
      <nav className="flex min-w-0 items-center gap-1 text-[12px] tracking-tight">
        {trail.map((c, i) => (
          <span key={`${c.label}-${i}`} className="flex items-center gap-1">
            {i > 0 && <ChevronRight className="h-3 w-3 text-muted-foreground/60" />}
            {c.href && i < trail.length - 1 ? (
              <Link href={c.href} className="text-muted-foreground hover:text-foreground">
                {c.label}
              </Link>
            ) : (
              <span className={i === 0 ? "text-muted-foreground" : "text-foreground"}>{c.label}</span>
            )}
          </span>
        ))}
      </nav>

      <div className="ml-auto flex items-center gap-1.5">
        {/* Search */}
        <div className="relative hidden md:block">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            type="search"
            placeholder="Search renders, channels…"
            className="h-8 w-72 rounded-md border border-border bg-surface pl-8 pr-12 text-[12px] outline-none placeholder:text-muted-foreground focus:border-border-strong"
          />
          <kbd className="pointer-events-none absolute right-2 top-1/2 flex h-5 -translate-y-1/2 items-center rounded border border-border bg-background px-1.5 font-mono text-[10px] text-muted-foreground">
            ⌘K
          </kbd>
        </div>

        <Button asChild size="sm" className="gap-1.5">
          <Link href="/app/create">
            <Plus className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Create</span>
          </Link>
        </Button>

        <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Notifications">
          <Bell className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label="Toggle theme"
          onClick={toggleTheme}
        >
          {theme === "dark" ? <Moon className="h-3.5 w-3.5" /> : <Sun className="h-3.5 w-3.5" />}
        </Button>
      </div>
    </header>
  );
}
