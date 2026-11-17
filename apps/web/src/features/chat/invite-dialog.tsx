"use client";

import { Check, Copy, Link2, UserPlus } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { type Invite, useCreateInvite, useInvites, useRevokeInvite } from "@/lib/api/hooks";

const EXPIRY = [
  { label: "1 hour", hours: 1 },
  { label: "1 day", hours: 24 },
  { label: "7 days", hours: 24 * 7 },
  { label: "30 days", hours: 24 * 30 },
] as const;

const USES = [
  { label: "Unlimited", value: null },
  { label: "1 person", value: 1 },
  { label: "5 people", value: 5 },
  { label: "10 people", value: 10 },
] as const;

export function inviteUrl(token: string, origin: string): string {
  return `${origin.replace(/\/$/, "")}/invite/${token}`;
}

function describe(i: Invite): string {
  const uses = i.max_uses ? `${i.uses}/${i.max_uses} used` : `${i.uses} used`;
  const exp = i.expires_at ? `expires ${new Date(i.expires_at).toLocaleString()}` : "never expires";
  const role = i.role === "editor" ? "Editor" : "Viewer";
  return `${role} · ${uses} · ${exp}`;
}

const selectCls =
  "h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30";

export function InviteDialog({
  chatId,
  open,
  onOpenChange,
}: {
  chatId: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const uid = useId();
  const create = useCreateInvite(chatId);
  const invites = useInvites(chatId, open);
  const revoke = useRevokeInvite(chatId);
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [hours, setHours] = useState<number>(24 * 7);
  const [uses, setUses] = useState<number | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const active = invites.data?.filter((i) => i.state === "active") ?? [];

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) setLink(null);
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Invite people</DialogTitle>
          <DialogDescription>
            Anyone with the link can join after signing in. They chat on your API key but never see
            it.
          </DialogDescription>
        </DialogHeader>

        {link ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-lg border bg-muted/50 p-2">
              <Link2 className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              <input
                readOnly
                value={link}
                aria-label="Invite link"
                onFocus={(e) => e.currentTarget.select()}
                className="min-w-0 flex-1 bg-transparent font-mono text-xs outline-none"
              />
              <Button
                size="sm"
                onClick={() =>
                  void navigator.clipboard?.writeText(link).then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                  })
                }
              >
                {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy"}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              This link is shown only once. Create a new one any time.
            </p>
            <Button variant="outline" onClick={() => setLink(null)}>
              Create another link
            </Button>
          </div>
        ) : (
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate(
                { role, expires_in_hours: hours, max_uses: uses },
                {
                  onSuccess: (r) => setLink(inviteUrl(r.token, window.location.origin)),
                  onError: (err) => toast.error(err.message),
                },
              );
            }}
          >
            <div className="grid grid-cols-3 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor={`${uid}-role`}>Role</Label>
                <select
                  id={`${uid}-role`}
                  className={selectCls}
                  value={role}
                  onChange={(e) => setRole(e.target.value as "editor" | "viewer")}
                >
                  <option value="editor">Editor</option>
                  <option value="viewer">Viewer</option>
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${uid}-exp`}>Expires</Label>
                <select
                  id={`${uid}-exp`}
                  className={selectCls}
                  value={hours}
                  onChange={(e) => setHours(Number(e.target.value))}
                >
                  {EXPIRY.map((x) => (
                    <option key={x.hours} value={x.hours}>
                      {x.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${uid}-uses`}>Uses</Label>
                <select
                  id={`${uid}-uses`}
                  className={selectCls}
                  value={uses ?? ""}
                  onChange={(e) => setUses(e.target.value ? Number(e.target.value) : null)}
                >
                  {USES.map((x) => (
                    <option key={x.label} value={x.value ?? ""}>
                      {x.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              {role === "editor"
                ? "Editors can send messages to the AI."
                : "Viewers can read along live but can't send messages."}
            </p>
            <Button type="submit" disabled={create.isPending}>
              <UserPlus /> {create.isPending ? "Creating…" : "Create invite link"}
            </Button>
          </form>
        )}

        {active.length > 0 && (
          <div className="space-y-2 border-t pt-3">
            <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Active links
            </h3>
            <ul className="space-y-1.5">
              {active.map((i) => (
                <li key={i.id} className="flex items-center justify-between gap-2 text-sm">
                  <span className="min-w-0 truncate text-muted-foreground">
                    {describe(i)}
                  </span>
                  <Button
                    size="xs"
                    variant="ghost"
                    className="text-destructive"
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(i.id, { onSuccess: () => toast("Link revoked") })}
                  >
                    Revoke
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
