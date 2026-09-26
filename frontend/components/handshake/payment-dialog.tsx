"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { money } from "@/lib/format";
import { CreditCard, Lock } from "lucide-react";

const digits = (s: string) => s.replace(/\D/g, "");

function luhn(num: string) {
  let sum = 0;
  for (let i = 0; i < num.length; i++) {
    let d = Number(num[num.length - 1 - i]);
    if (i % 2) { d *= 2; if (d > 9) d -= 9; }
    sum += d;
  }
  return sum % 10 === 0;
}

function expiryValid(v: string) {
  const m = v.match(/^(\d{2})\s*\/\s*(\d{2})$/);
  if (!m) return false;
  const month = Number(m[1]), year = 2000 + Number(m[2]);
  if (month < 1 || month > 12) return false;
  const now = new Date();
  return year > now.getFullYear() || (year === now.getFullYear() && month >= now.getMonth() + 1);
}

const EMPTY = { name: "", number: "", expiry: "", cvc: "", zip: "" };

/**
 * PLACEHOLDER checkout. Stripe (Payment Link / Checkout) replaces this form.
 * Card fields stay in component state only: they are never passed to onPay, logged or stored.
 */
export function PaymentDialog({ amount, title, description, children, submitLabel, open, onOpenChange, onPay }: {
  amount: number;
  title: string;
  description: ReactNode;
  /** Extra content under the card fields, e.g. how the held money is used. */
  children?: ReactNode;
  submitLabel?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPay: () => Promise<void>;
}) {
  const [form, setForm] = useState(EMPTY);
  const [touched, setTouched] = useState(false);
  const [paying, setPaying] = useState(false);

  const num = digits(form.number);
  const errors = {
    name: !form.name.trim() && "Enter the name on the card",
    number: !(num.length >= 13 && num.length <= 19 && luhn(num)) && "Card number isn't valid",
    expiry: !expiryValid(form.expiry) && "Use MM/YY, not in the past",
    cvc: !/^\d{3,4}$/.test(form.cvc) && "3 or 4 digits",
    zip: !/^\d{5}$/.test(form.zip) && "5-digit ZIP",
  };
  const valid = !Object.values(errors).some(Boolean);

  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement>) => {
    let v = e.target.value;
    if (k === "number") v = digits(v).slice(0, 19).replace(/(\d{4})(?=\d)/g, "$1 ");
    if (k === "expiry") { const d = digits(v).slice(0, 4); v = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d; }
    if (k === "cvc") v = digits(v).slice(0, 4);
    if (k === "zip") v = digits(v).slice(0, 5);
    setForm((f) => ({ ...f, [k]: v }));
  };

  function close(next: boolean) {
    if (!next) { setForm(EMPTY); setTouched(false); }
    onOpenChange(next);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (!valid) return;
    setPaying(true);
    try {
      await onPay();
      close(false);
    } finally { setPaying(false); }
  }

  const err = (k: keyof typeof errors) => touched && errors[k] ? <p className="text-xs text-fail">{errors[k]}</p> : null;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} noValidate className="grid gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><CreditCard className="size-5" />{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>

          <p className="rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
            Placeholder checkout. Stripe will replace this form. Card details aren&apos;t sent or saved anywhere.
          </p>

          <div className="grid gap-1.5">
            <Label htmlFor="cc-name">Name on card</Label>
            <Input id="cc-name" autoComplete="cc-name" value={form.name} onChange={set("name")} aria-invalid={touched && !!errors.name} />
            {err("name")}
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="cc-number">Card number</Label>
            <Input id="cc-number" autoComplete="cc-number" inputMode="numeric" placeholder="1234 1234 1234 1234" value={form.number} onChange={set("number")} aria-invalid={touched && !!errors.number} className="font-mono" />
            {err("number")}
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="cc-exp">Expiry</Label>
              <Input id="cc-exp" autoComplete="cc-exp" inputMode="numeric" placeholder="MM/YY" value={form.expiry} onChange={set("expiry")} aria-invalid={touched && !!errors.expiry} />
              {err("expiry")}
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="cc-cvc">CVC</Label>
              <Input id="cc-cvc" autoComplete="cc-csc" inputMode="numeric" value={form.cvc} onChange={set("cvc")} aria-invalid={touched && !!errors.cvc} />
              {err("cvc")}
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="cc-zip">ZIP</Label>
              <Input id="cc-zip" autoComplete="postal-code" inputMode="numeric" value={form.zip} onChange={set("zip")} aria-invalid={touched && !!errors.zip} />
              {err("zip")}
            </div>
          </div>

          {children}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => close(false)}>Cancel</Button>
            <Button type="submit" disabled={paying} className="bg-brand text-brand-foreground hover:bg-brand/90">
              <Lock />{paying ? "Processing…" : submitLabel ?? `Pay ${money(amount)}`}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
