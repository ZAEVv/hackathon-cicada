import { flushSync } from "react-dom";

export type Theme = "dark" | "light";

const KEY = "sct-theme";
const DURATION = 1150;

export const reducedMotion = () =>
  typeof matchMedia === "function" &&
  matchMedia("(prefers-reduced-motion: reduce)").matches;

// Dark is the product's default look; a saved choice wins. Storage can throw
// (private mode, blocked storage), so access is guarded.
export function loadTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage unavailable */
  }
  return "dark";
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "dark" ? "#0e0e10" : "#f6f6f7");
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* storage unavailable: theme lasts for this page only */
  }
}

// Closed blob around (cx, cy): a circle whose radius is disturbed by a few
// sine waves, smoothed into cubic curves. Every call returns the same command
// structure, so consecutive shapes interpolate.
function blob(cx: number, cy: number, r: number, phase: number, amp: number) {
  const N = 12;
  const pts: [number, number][] = [];
  for (let i = 0; i < N; i++) {
    const a = (i / N) * Math.PI * 2;
    const wobble =
      1 +
      amp *
        (0.5 * Math.sin(a * 3 + phase) +
          0.3 * Math.sin(a * 5 - phase * 1.6) +
          0.35 * Math.sin(a * 2 + phase * 0.7));
    pts.push([cx + Math.cos(a) * r * wobble, cy + Math.sin(a) * r * wobble]);
  }
  const f = (n: number) => n.toFixed(1);
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 0; i < N; i++) {
    const p0 = pts[(i - 1 + N) % N];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % N];
    const p3 = pts[(i + 2) % N];
    d +=
      `C${f(p1[0] + (p2[0] - p0[0]) / 6)} ${f(p1[1] + (p2[1] - p0[1]) / 6)} ` +
      `${f(p2[0] - (p3[0] - p1[0]) / 6)} ${f(p2[1] - (p3[1] - p1[1]) / 6)} ` +
      `${f(p2[0])} ${f(p2[1])}`;
  }
  return d + "Z";
}

const ease = (t: number) =>
  t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

// Two pools of the new theme: one pours out of the toggle, a smaller one
// rises from the opposite corner a moment later, and they merge.
function liquidFrames(x: number, y: number, w: number, h: number) {
  const reach = (px: number, py: number) =>
    Math.max(
      Math.hypot(px, py),
      Math.hypot(w - px, py),
      Math.hypot(px, h - py),
      Math.hypot(w - px, h - py),
    );
  const x2 = x > w / 2 ? 0 : w;
  const y2 = y > h / 2 ? 0 : h;
  const r1 = reach(x, y) * 1.12;
  const r2 = reach(x2, y2) * 0.6;
  const STEPS = 26;
  const frames: string[] = [];
  for (let i = 0; i <= STEPS; i++) {
    const t = i / STEPS;
    const late = Math.max(0, (t - 0.18) / 0.82);
    const amp = 0.26 * Math.pow(1 - t, 1.2);
    frames.push(
      `path("${blob(x, y, r1 * ease(t), t * 7, amp)} ${blob(x2, y2, r2 * ease(late), 2 + t * 6, amp * 1.2)}")`,
    );
  }
  return frames;
}

type ViewTransitionLike = { ready: Promise<void>; finished: Promise<void> };

// Switches the theme. With an origin point and View Transitions support the
// new theme spreads from that point as a liquid shape; otherwise the switch
// is instant (also under prefers-reduced-motion).
export function switchTheme(
  next: Theme,
  origin: { x: number; y: number } | null,
  commit: () => void,
) {
  const run = () => {
    flushSync(commit);
    applyTheme(next);
  };
  const doc = document as Document & {
    startViewTransition?: (cb: () => void) => ViewTransitionLike;
  };
  if (
    !origin ||
    typeof doc.startViewTransition !== "function" ||
    reducedMotion()
  ) {
    run();
    return;
  }
  const root = document.documentElement;
  root.classList.add("theme-liquid");
  let transition: ViewTransitionLike;
  try {
    transition = doc.startViewTransition(run);
  } catch {
    root.classList.remove("theme-liquid");
    run();
    return;
  }
  transition.ready
    .then(() => {
      root.animate(
        {
          clipPath: liquidFrames(
            origin.x,
            origin.y,
            window.innerWidth,
            window.innerHeight,
          ),
        },
        {
          duration: DURATION,
          easing: "linear",
          pseudoElement: "::view-transition-new(root)",
        },
      );
    })
    .catch(() => {
      /* transition skipped: the theme is already applied */
    });
  transition.finished
    .catch(() => {})
    .then(() => root.classList.remove("theme-liquid"));
}
