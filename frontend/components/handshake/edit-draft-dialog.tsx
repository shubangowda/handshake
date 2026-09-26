"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { Constraint, DraftPatch, DraftRecord } from "@/lib/types";
import { FIELD_LABELS } from "@/lib/format";

const toDateInput = (iso: string | null | undefined) => (iso ? iso.slice(0, 10) : "");
const num = (s: string) => (s.trim() === "" ? null : Number(s));

function valueToText(v: Constraint["value"]) {
  return Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v);
}
function textToValue(text: string, original: Constraint["value"]): Constraint["value"] {
  if (Array.isArray(original)) return text.split(",").map((s) => s.trim()).filter(Boolean);
  if (typeof original === "number") return Number(text);
  if (typeof original === "boolean") return /^(y|yes|true)$/i.test(text.trim());
  return text.trim();
}

export function EditDraftDialog({ draft, open, onOpenChange, onSave }: {
  draft: DraftRecord;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (patch: DraftPatch) => Promise<void>;
}) {
  const [target, setTarget] = useState(String(draft.spend.target ?? ""));
  const [cap, setCap] = useState(String(draft.spend.hard_cap_all_in));
  const [shipping, setShipping] = useState(String(draft.delivery?.max_shipping ?? ""));
  const [deliverBy, setDeliverBy] = useState(toDateInput(draft.delivery?.deliver_by));
  const [values, setValues] = useState(draft.constraints.map((c) => valueToText(c.value)));
  const [saving, setSaving] = useState(false);

  const capN = Number(cap);
  const targetN = num(target);
  const error = !(capN > 0) ? "Maximum total must be more than $0."
    : targetN != null && targetN > capN ? "Target can't be above the maximum total." : null;

  async function save() {
    if (error) return;
    setSaving(true);
    const constraints = draft.constraints.map((c, i) => {
      const changed = values[i] !== valueToText(c.value);
      return changed ? { ...c, value: textToValue(values[i], c.value), source: "user" as const } : c;
    });
    const patch: DraftPatch = { target: targetN, hard_cap_all_in: capN, constraints };
    if (draft.delivery) {
      patch.max_shipping = num(shipping);
      const prev = toDateInput(draft.delivery.deliver_by);
      if (deliverBy !== prev) patch.deliver_by = deliverBy ? new Date(`${deliverBy}T20:00:00`).toISOString() : null;
    }
    try { await onSave(patch); onOpenChange(false); } finally { setSaving(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit contract</DialogTitle>
          <DialogDescription>Anything you change becomes your value instead of Handshake&apos;s guess.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-2">
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5"><Label htmlFor="target">Target price ($)</Label>
              <Input id="target" inputMode="decimal" value={target} onChange={(e) => setTarget(e.target.value)} /></div>
            <div className="grid gap-1.5"><Label htmlFor="cap">Maximum total ($)</Label>
              <Input id="cap" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} aria-invalid={!!error} /></div>
          </div>
          {draft.delivery && (
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-1.5"><Label htmlFor="ship">Max shipping ($)</Label>
                <Input id="ship" inputMode="decimal" value={shipping} onChange={(e) => setShipping(e.target.value)} /></div>
              <div className="grid gap-1.5"><Label htmlFor="by">Deliver by</Label>
                <Input id="by" type="date" value={deliverBy} onChange={(e) => setDeliverBy(e.target.value)} /></div>
            </div>
          )}
          {draft.constraints.map((c, i) => (
            <div key={i} className="grid gap-1.5">
              <Label htmlFor={`c${i}`}>{FIELD_LABELS[c.field]} <span className="font-normal text-muted-foreground">({c.severity === "soft" ? "preference" : "must"})</span></Label>
              <Input id={`c${i}`} value={values[i]} onChange={(e) => setValues((v) => v.map((x, j) => (j === i ? e.target.value : x)))} />
            </div>
          ))}
          {error && <p className="text-sm text-fail">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={save} disabled={!!error || saving}>{saving ? "Saving…" : "Save changes"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
