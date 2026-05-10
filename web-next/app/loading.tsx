import { Loader2 } from "lucide-react";

export default function Loading() {
  return (
    <main className="flex min-h-screen items-center justify-center text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
    </main>
  );
}
