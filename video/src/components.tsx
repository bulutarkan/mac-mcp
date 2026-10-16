import React from "react";
import { AbsoluteFill, Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { theme } from "./theme";

/** A night-time Mac desktop: wallpaper glow, the menu bar and the notch. */
export const Desktop: React.FC<{ children?: React.ReactNode; dim?: number }> = ({ children, dim = 0 }) => (
  <AbsoluteFill style={{ background: theme.night, fontFamily: theme.sans, color: theme.paper }}>
    <AbsoluteFill
      style={{
        background: `radial-gradient(1100px 700px at 75% 10%, ${theme.violet}33, transparent 70%),
                     radial-gradient(900px 600px at 10% 80%, ${theme.teal}22, transparent 70%)`,
      }}
    />
    <div
      style={{
        position: "absolute", top: 0, left: 0, right: 0, height: 44, display: "flex", alignItems: "center",
        padding: "0 28px", gap: 28, fontSize: 22, color: "rgba(236,239,246,0.85)", background: "rgba(10,14,28,0.35)",
      }}
    >
      <span style={{ fontWeight: 600 }}>Finder</span>
      <span>File</span>
      <span>Edit</span>
      <span>View</span>
    </div>
    {children}
    <AbsoluteFill style={{ background: `rgba(5,8,18,${dim})`, pointerEvents: "none" }} />
  </AbsoluteFill>
);

export const Window: React.FC<{
  x: number; y: number; w: number; h: number; title: string; children?: React.ReactNode; style?: React.CSSProperties;
}> = ({ x, y, w, h, title, children, style }) => (
  <div
    style={{
      position: "absolute", left: x, top: y, width: w, height: h, borderRadius: 22, overflow: "hidden",
      background: theme.glass, border: `1px solid ${theme.glassEdge}`, boxShadow: "0 40px 100px rgba(0,0,0,0.45)",
      ...style,
    }}
  >
    <div style={{ height: 52, display: "flex", alignItems: "center", gap: 10, padding: "0 20px", borderBottom: `1px solid ${theme.glassEdge}` }}>
      {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
        <span key={c} style={{ width: 14, height: 14, borderRadius: 7, background: c }} />
      ))}
      <span style={{ marginLeft: 14, fontSize: 20, color: theme.haze }}>{title}</span>
    </div>
    <div style={{ padding: 28 }}>{children}</div>
  </div>
);

/** The caption that narrates each scene (the video has no voice). */
export const Caption: React.FC<{ text: string; start: number; end: number }> = ({ text, start, end }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [start, start + 10, end - 10, end], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const y = interpolate(frame, [start, start + 14], [18, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <div
      style={{
        position: "absolute", left: 0, right: 0, bottom: 70, display: "flex", justifyContent: "center",
        opacity, transform: `translateY(${y}px)`,
      }}
    >
      <div
        style={{
          fontFamily: theme.serif, fontSize: 58, fontWeight: 600, letterSpacing: -0.5, padding: "14px 34px",
          borderRadius: 20, background: "rgba(10,14,28,0.72)", border: `1px solid ${theme.glassEdge}`,
        }}
      >
        {text}
      </div>
    </div>
  );
};

/** A small label pinned to something on screen. */
export const Chip: React.FC<{ text: string; color?: string; style?: React.CSSProperties }> = ({ text, color = theme.paper, style }) => (
  <span
    style={{
      display: "inline-block", fontSize: 18, fontWeight: 700, letterSpacing: 1.2, textTransform: "uppercase",
      padding: "6px 14px", borderRadius: 99, background: "rgba(10,14,28,0.6)", color, ...style,
    }}
  >
    {text}
  </span>
);

/** The app icon as macOS shows it: its own rounded tile, shadow and transparent margin. */
export const AppIcon: React.FC<{ size: number; style?: React.CSSProperties }> = ({ size, style }) => (
  <Img src={staticFile("app-icon.png")} style={{ width: size, height: size, ...style }} />
);

/** The bare monogram, as the app's menu header and Live Island show it. */
export const BrandMark: React.FC<{ size: number; style?: React.CSSProperties }> = ({ size, style }) => (
  <Img src={staticFile("mark.svg")} style={{ width: size, height: size, ...style }} />
);

/** Neutral app glyphs (no vendor artwork): a tile with a simple drawn symbol. */
export const Glyph: React.FC<{ kind: "folder" | "terminal" | "browser" | "sparkles" | "notes"; size: number }> = ({ kind, size }) => {
  const colors: Record<string, string> = { folder: "#4aa3ff", terminal: "#2b2f3a", browser: "#3b82f6", sparkles: theme.violet, notes: "#f5c542" };
  const s = size;
  const symbol = {
    folder: <path d={`M${s * 0.2} ${s * 0.32}h${s * 0.24}l${s * 0.06} ${s * 0.07}h${s * 0.3}v${s * 0.33}h-${s * 0.6}z`} fill="#fff" />,
    terminal: <text x={s * 0.2} y={s * 0.62} fontSize={s * 0.36} fontFamily="monospace" fill="#fff">{">_"}</text>,
    browser: (
      <g>
        <circle cx={s / 2} cy={s / 2} r={s * 0.28} fill="none" stroke="#fff" strokeWidth={s * 0.06} />
        <path d={`M${s * 0.5} ${s * 0.3}l${s * 0.07} ${s * 0.2}l-${s * 0.07} ${s * 0.2}l-${s * 0.07} -${s * 0.2}z`} fill="#fff" />
      </g>
    ),
    sparkles: <path d={`M${s / 2} ${s * 0.22}l${s * 0.07} ${s * 0.21}l${s * 0.21} ${s * 0.07}l-${s * 0.21} ${s * 0.07}l-${s * 0.07} ${s * 0.21}l-${s * 0.07} -${s * 0.21}l-${s * 0.21} -${s * 0.07}l${s * 0.21} -${s * 0.07}z`} fill="#fff" />,
    notes: <path d={`M${s * 0.28} ${s * 0.36}h${s * 0.44}M${s * 0.28} ${s * 0.5}h${s * 0.44}M${s * 0.28} ${s * 0.64}h${s * 0.3}`} stroke="#5a4a12" strokeWidth={s * 0.06} strokeLinecap="round" />,
  }[kind];
  return (
    <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`}>
      <rect width={s} height={s} rx={s * 0.24} fill={colors[kind]} />
      {symbol}
    </svg>
  );
};

/** A spring that starts at `at` (frames), 0 → 1. */
export const useEnter = (at: number, damping = 14) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return spring({ frame: frame - at, fps, config: { damping, mass: 0.8 } });
};
