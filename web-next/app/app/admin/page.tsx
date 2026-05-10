"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, RefreshCw, Shield, ShieldOff, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/page-header";
import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { cn, relativeTime } from "@/lib/utils";

interface AuthUser {
  email: string;
  name?: string;
  picture?: string;
  status: "approved" | "pending" | "denied" | string;
  is_admin?: boolean;
  requested_at?: string;
  approved_at?: string;
  approved_by?: string;
  decision_at?: string;
  decision_by?: string;
  request_note?: string;
  last_seen_at?: string;
}

interface WhoAmI {
  signed_in: boolean;
  is_admin?: boolean;
  email?: string;
}

export default function AdminPage() {
  const [me, setMe] = useState<WhoAmI | null>(null);
  const [pending, setPending] = useState<AuthUser[] | null>(null);
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [actingOn, setActingOn] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const [pendingRes, usersRes] = await Promise.all([
        api.get<{ pending: AuthUser[] }>("/api/admin/requests"),
        api.get<{ users: AuthUser[] }>("/api/admin/users"),
      ]);
      setPending(pendingRes.pending ?? []);
      setUsers(usersRes.users ?? []);
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status}: ${(e.body as any)?.error ?? e.message}` : String(e);
      toast.error("Failed to load access data", { description: msg });
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .get<WhoAmI>("/api/auth/whoami")
      .then((res) => {
        if (cancelled) return;
        setMe(res);
        if (res.is_admin) refresh();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  useEffect(() => {
    if (!me?.is_admin) return;
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [me?.is_admin, refresh]);

  async function decide(email: string, action: "approve" | "deny") {
    setActingOn(email);
    try {
      await api.post(`/api/admin/requests/${encodeURIComponent(email)}/${action}`);
      toast.success(`${action === "approve" ? "Approved" : "Denied"} ${email}`);
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status}: ${(e.body as any)?.detail ?? e.message}` : String(e);
      toast.error(`Failed to ${action}`, { description: msg });
    } finally {
      setActingOn(null);
    }
  }

  if (me && !me.is_admin) {
    return (
      <div className="flex h-full flex-col">
        <PageHeader
          eyebrow="Access control"
          title="Admin"
          description="This area is restricted to ytFactory admins."
        />
        <div className="px-6 py-10 md:px-8">
          <EmptyState
            icon={ShieldOff}
            title="Not an admin"
            description={
              me.signed_in
                ? `Signed in as ${me.email}. Ask an admin to grant you access.`
                : "Sign in with an admin account to view this page."
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="Access control"
        title="Admin"
        description="Approve sign-in requests and review user activity."
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={refresh}
            disabled={refreshing}
            className="h-8 gap-1.5"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
            Refresh
          </Button>
        }
      />

      <div className="flex flex-1 flex-col gap-10 px-6 py-8 md:px-8">
        <Section
          title="Pending requests"
          subtitle={
            pending === null
              ? undefined
              : pending.length === 0
                ? "No outstanding requests."
                : `${pending.length} awaiting decision`
          }
        >
          {pending === null ? (
            <Skeleton className="h-24 w-full" />
          ) : pending.length === 0 ? (
            <EmptyState
              icon={Shield}
              title="Inbox zero"
              description="No one has requested access since you last checked."
            />
          ) : (
            <UserTable
              rows={pending}
              actions={(row) => (
                <div className="flex justify-end gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 px-2.5 text-[12px]"
                    onClick={() => decide(row.email, "deny")}
                    disabled={actingOn === row.email}
                  >
                    {actingOn === row.email ? (
                      <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                    ) : (
                      <X className="mr-1 h-3 w-3" />
                    )}
                    Deny
                  </Button>
                  <Button
                    size="sm"
                    className="h-7 px-2.5 text-[12px]"
                    onClick={() => decide(row.email, "approve")}
                    disabled={actingOn === row.email}
                  >
                    {actingOn === row.email ? (
                      <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                    ) : (
                      <Check className="mr-1 h-3 w-3" />
                    )}
                    Approve
                  </Button>
                </div>
              )}
            />
          )}
        </Section>

        <Section
          title="All users"
          subtitle={
            users === null ? undefined : users.length === 0 ? "No records yet." : `${users.length} total`
          }
        >
          {users === null ? (
            <Skeleton className="h-32 w-full" />
          ) : users.length === 0 ? (
            <EmptyState
              icon={Shield}
              title="No users"
              description="The auth_users collection is empty. It populates as people sign in."
            />
          ) : (
            <UserTable rows={users} />
          )}
        </Section>
      </div>
    </div>
  );
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-[14px] font-medium tracking-tight">{title}</h2>
        {subtitle && (
          <span className="text-[12px] text-muted-foreground">{subtitle}</span>
        )}
      </div>
      {children}
    </section>
  );
}

function UserTable({
  rows,
  actions,
}: {
  rows: AuthUser[];
  actions?: (row: AuthUser) => React.ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <table className="w-full text-left">
        <thead className="bg-surface/40">
          <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            <Th>User</Th>
            <Th>Status</Th>
            <Th>Requested</Th>
            <Th>Decision by</Th>
            {actions && <Th className="text-right">Action</Th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.email} className="border-t border-border">
              <Td>
                <div className="flex items-center gap-2.5">
                  {row.picture ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={row.picture}
                      alt=""
                      referrerPolicy="no-referrer"
                      className="h-7 w-7 rounded-full border border-border object-cover"
                    />
                  ) : (
                    <span className="grid h-7 w-7 place-items-center rounded-full border border-border bg-surface-2 font-mono text-[10px]">
                      {(row.name || row.email).charAt(0).toUpperCase()}
                    </span>
                  )}
                  <div className="min-w-0">
                    <div className="truncate text-[13px] font-medium tracking-tight">
                      {row.email}
                      {row.is_admin && (
                        <span className="ml-2 font-mono text-[10px] uppercase tracking-[0.18em] text-violet-400">
                          admin
                        </span>
                      )}
                    </div>
                    {row.name && (
                      <div className="truncate text-[11px] text-muted-foreground">
                        {row.name}
                      </div>
                    )}
                  </div>
                </div>
              </Td>
              <Td>
                <StatusBadge status={row.status} />
              </Td>
              <Td className="text-[12px] text-muted-foreground">
                {row.requested_at ? relativeTime(row.requested_at) : "—"}
              </Td>
              <Td className="text-[12px] text-muted-foreground">
                {row.decision_by || row.approved_by || "—"}
              </Td>
              {actions && <Td>{actions(row)}</Td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <th className={cn("px-4 py-2.5 font-medium", className)}>{children}</th>
  );
}

function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <td className={cn("px-4 py-3 align-middle", className)}>{children}</td>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "approved"
      ? "text-emerald-400 bg-emerald-500/10 border-emerald-500/30"
      : status === "pending"
        ? "text-amber-400 bg-amber-500/10 border-amber-500/30"
        : status === "denied"
          ? "text-rose-400 bg-rose-500/10 border-rose-500/30"
          : "text-muted-foreground bg-surface-2 border-border";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em]",
        tone,
      )}
    >
      {status}
    </span>
  );
}
