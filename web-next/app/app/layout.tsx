import { Sidebar } from "@/components/nav/sidebar";
import { TopBar } from "@/components/nav/topbar";
import { AppShellWarmer } from "@/components/app/app-shell-warmer";
import { StudioSwRegister } from "@/components/app/studio-sw-register";

export default function StudioLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      {/*
        Warm the SWR cache for sidebar destinations as soon as the
        studio shell mounts. By the time the user clicks any sidebar
        link, the data is already in the in-memory + localStorage
        cache and the destination page paints instantly. Renders
        nothing — see components/app/app-shell-warmer.tsx.
      */}
      <AppShellWarmer />
      {/*
        Register the studio service worker (public/sw.js). The SW
        provides stale-while-revalidate for /api/* GETs at the
        network layer — survives hard reload + browser restart +
        offline. Kill switch: any /app/* URL + `?nosw=1`.
      */}
      <StudioSwRegister />
      <div className="hidden md:block">
        <Sidebar />
      </div>
      <div className="flex flex-1 flex-col overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
