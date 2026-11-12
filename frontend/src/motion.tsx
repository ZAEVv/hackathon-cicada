import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { fmt } from "./lib";
import { reducedMotion } from "./theme";

// Number that eases from its previous value to the new one.
export function CountUp({
  value,
  digits = 0,
  duration = 900,
}: {
  value: number | null | undefined;
  digits?: number;
  duration?: number;
}) {
  const valid = value != null && Number.isFinite(value);
  const [shown, setShown] = useState<number>(() =>
    valid && reducedMotion() ? (value as number) : 0,
  );
  const last = useRef(shown);
  useEffect(() => {
    if (!valid) return;
    const target = value as number;
    const start = last.current;
    const delta = target - start;
    if (!delta || reducedMotion()) {
      last.current = target;
      setShown(target);
      return;
    }
    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - t0) / duration);
      const v = start + delta * (1 - Math.pow(1 - p, 4));
      last.current = v;
      setShown(v);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, valid, duration]);
  return <span className="num">{valid ? fmt(shown, digits) : "—"}</span>;
}

// Progress ring; the arc draws itself on mount and eases on change.
export function Ring({
  value,
  size = 44,
  stroke = 5,
  tone = "accent",
  children,
}: {
  value: number;
  size?: number;
  stroke?: number;
  tone?: string;
  children?: React.ReactNode;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const p = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
  return (
    <span className={"ring tone-" + tone} style={{ width: size, height: size }}>
      <svg viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          className="ring-track"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
        />
        <circle
          className="ring-arc"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
          strokeDasharray={c}
          strokeDashoffset={c * (1 - p)}
          style={{ "--c": c } as React.CSSProperties}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      {children != null && <span className="ring-center">{children}</span>}
    </span>
  );
}

export function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize((s) =>
        Math.abs(s.w - width) < 0.5 && Math.abs(s.h - height) < 0.5
          ? s
          : { w: width, h: height },
      );
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

// Pointer effects.
// 1. Hover styles live under ".hoverable" on <html>: the class appears when a
//    mouse or pen actually moves and goes away on touch, so hover never
//    sticks after a tap and still works where the browser reports no hover
//    (touch-screen laptops, remote sessions).
// 2. Cursor-following light: the ".glow" element or button under the pointer
//    gets --mx/--my, and CSS paints from that point.
export function useSpotlight() {
  useEffect(() => {
    const root = document.documentElement;
    let raf = 0;
    let pending: PointerEvent | null = null;
    const flush = () => {
      raf = 0;
      const e = pending;
      pending = null;
      if (!e || !(e.target instanceof Element)) return;
      const el = e.target.closest<HTMLElement>(".glow, .primary, .secondary");
      if (!el) return;
      const rect = el.getBoundingClientRect();
      el.style.setProperty("--mx", `${e.clientX - rect.left}px`);
      el.style.setProperty("--my", `${e.clientY - rect.top}px`);
    };
    const onMove = (e: PointerEvent) => {
      if (e.pointerType === "touch") return;
      root.classList.add("hoverable");
      pending = e;
      if (!raf) raf = requestAnimationFrame(flush);
    };
    const onDown = (e: PointerEvent) => {
      if (e.pointerType === "touch") root.classList.remove("hoverable");
    };
    document.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("pointerdown", onDown, { passive: true });
    return () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerdown", onDown);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);
}
