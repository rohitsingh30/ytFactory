"use client";

import { Toaster as Sonner } from "sonner";

type ToasterProps = React.ComponentProps<typeof Sonner>;

export function Toaster(props: ToasterProps) {
  return (
    <Sonner
      theme="dark"
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            "group toast border border-border bg-card text-card-foreground shadow-2xl",
          description: "text-muted-foreground",
          actionButton: "bg-foreground text-background",
          cancelButton: "bg-surface text-muted-foreground",
        },
      }}
      {...props}
    />
  );
}
