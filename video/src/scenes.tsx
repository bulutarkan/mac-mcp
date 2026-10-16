import React from "react";
import { AbsoluteFill, interpolate, Sequence, useCurrentFrame } from "remotion";
import { AppIcon, BrandMark, Caption, Chip, Desktop, Glyph, useEnter, Window } from "./components";
import { seconds, theme } from "./theme";

const clamp = { extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const };

// 0–4 s: the AI can talk about the Mac but not use it.
const Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const ask = useEnter(6);
  const reply = useEnter(40);
  return (
    <Desktop>
      <Window x={460} y={250} w={1000} h={330} title="Chat">
        <div style={{ display: "flex", flexDirection: "column", gap: 26, fontSize: 30 }}>
          <div style={{ alignSelf: "flex-end", maxWidth: 680, padding: "18px 24px", borderRadius: 22, background: theme.violet,
                        color: "#fff", opacity: ask, transform: `translateY(${(1 - ask) * 20}px)` }}>
            Summarize the PDFs in my Downloads folder.
          </div>
          <div style={{ alignSelf: "flex-start", maxWidth: 700, padding: "18px 24px", borderRadius: 22, background: "rgba(10,14,28,0.5)",
                        color: theme.haze, opacity: reply, transform: `translateY(${(1 - reply) * 20}px)` }}>
            I can't open files or apps on your Mac{".".repeat(Math.min(3, Math.max(0, Math.floor((frame - 50) / 8))))}
          </div>
        </div>
      </Window>
      <Caption text="Your AI can think. It can't use your Mac." start={0} end={seconds(4)} />
    </Desktop>
  );
};

// 4–9 s: one-click connect.
const Connect: React.FC = () => {
  const frame = useCurrentFrame();
  const panel = useEnter(0, 16);
  const apps = ["Claude Desktop", "Claude Code", "ChatGPT", "Codex", "OpenCode"];
  return (
    <Desktop>
      <div style={{ position: "absolute", right: 120, top: 70, width: 620, opacity: panel, transform: `translateY(${(1 - panel) * -24}px) scale(${0.96 + 0.04 * panel})`,
                    transformOrigin: "top right", borderRadius: 24, background: theme.glass, border: `1px solid ${theme.glassEdge}`,
                    boxShadow: "0 40px 100px rgba(0,0,0,0.5)", padding: 30 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 22 }}>
          <BrandMark size={52} />
          <div style={{ fontFamily: theme.serif, fontSize: 36, fontWeight: 600 }}>Connect your AI app.</div>
        </div>
        {apps.map((name, index) => {
          const done = frame > 30 + index * 16;
          const pop = useEnter(30 + index * 16, 12);
          return (
            <div key={name} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 0",
                                     borderTop: index ? `1px solid ${theme.glassEdge}` : "none", fontSize: 28 }}>
              <span>{name}</span>
              {done ? (
                <span style={{ width: 40, height: 40, borderRadius: 20, background: theme.ok, color: "#06281a", display: "grid",
                               placeItems: "center", fontWeight: 900, fontSize: 24, transform: `scale(${pop})` }}>✓</span>
              ) : (
                <span style={{ padding: "6px 18px", borderRadius: 12, background: theme.ember, color: theme.emberInk, fontWeight: 700, fontSize: 22 }}>Connect</span>
              )}
            </div>
          );
        })}
      </div>
      <Caption text="Connect the AI apps you already use." start={0} end={seconds(5)} />
    </Desktop>
  );
};

// 9–16 s: the live island beside the notch shows what is in use, then a tick.
const Island: React.FC = () => {
  const frame = useCurrentFrame();
  const enter = useEnter(0, 18);
  const slots: Array<"folder" | "terminal" | "browser"> = ["folder", "terminal", "browser"];
  const phase = Math.floor(Math.max(0, frame - 20) / 40);
  const working = phase < slots.length;
  const tick = useEnter(20 + slots.length * 40, 11);
  const notchW = 520, wing = 130, h = 92;
  const spin = (frame * 360) / 34;
  return (
    <Desktop>
      <div style={{ position: "absolute", top: 0, left: "50%", transform: `translateX(-50%) scaleX(${0.7 + 0.3 * enter})`, opacity: enter,
                    width: notchW + wing * 2, height: h, background: "#000", borderRadius: "0 0 26px 26px",
                    display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 40px",
                    boxShadow: "0 24px 60px rgba(0,0,0,0.5)" }}>
        <BrandMark size={56} />
        <div style={{ width: 72, height: 72, position: "relative", display: "grid", placeItems: "center" }}>
          {working ? (
            <>
              <Glyph kind={slots[phase]} size={44} />
              <svg width={72} height={72} style={{ position: "absolute", inset: 0, transform: `rotate(${spin}deg)` }}>
                <circle cx={36} cy={36} r={32} fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth={4} />
                <circle cx={36} cy={36} r={32} fill="none" stroke={theme.working} strokeWidth={4} strokeLinecap="round" strokeDasharray="56 260" />
              </svg>
            </>
          ) : (
            <span style={{ width: 54, height: 54, borderRadius: 27, background: theme.ok, color: "#06281a", display: "grid", placeItems: "center",
                           fontWeight: 900, fontSize: 32, transform: `scale(${tick})` }}>✓</span>
          )}
        </div>
      </div>
      <div style={{ position: "absolute", top: 140, left: 0, right: 0, textAlign: "center", opacity: interpolate(frame, [30, 50], [0, 1], clamp) }}>
        <Chip text="Live island · optional, off by default" color={theme.haze} />
      </div>
      <Window x={560} y={420} w={800} h={270} title="Recent">
        {["Looking through Downloads", "Running a command", "Looking at a page"].map((line, index) => {
          const show = interpolate(frame, [40 + index * 40, 52 + index * 40], [0, 1], clamp);
          return (
            <div key={line} style={{ display: "flex", alignItems: "center", gap: 18, fontSize: 26, padding: "8px 0", opacity: show }}>
              <Glyph kind={slots[index]} size={34} />
              <span style={{ flex: 1 }}>{line}</span>
              <span style={{ color: theme.ok, fontWeight: 800 }}>✓</span>
            </div>
          );
        })}
      </Window>
      <Caption text="See what it's doing." start={0} end={seconds(7)} />
    </Desktop>
  );
};

// 16–23 s: a Safari tab fills a form behind the window you are typing in.
const Browser: React.FC = () => {
  const frame = useCurrentFrame();
  const name = "Ayşe Yılmaz";
  const mail = "ayse@example.com";
  const typed = (text: string, from: number) => text.slice(0, Math.max(0, Math.floor((frame - from) / 3)));
  const pulse = 0.55 + 0.45 * Math.sin(frame / 7);
  const notes = "Weekly notes: launch checklist, reply to Ayşe, book the train";
  return (
    <Desktop>
      <Window x={140} y={120} w={1000} h={640} title="Safari" style={{ boxShadow: `0 0 0 4px rgba(94,211,196,${pulse}), 0 40px 100px rgba(0,0,0,0.45)` }}>
        <div style={{ fontSize: 26, color: theme.haze, marginBottom: 20 }}>Sign up</div>
        {[["Name", typed(name, 20)], ["Email", typed(mail, 20 + name.length * 3 + 8)]].map(([label, value]) => (
          <div key={label} style={{ marginBottom: 22 }}>
            <div style={{ fontSize: 20, color: theme.haze, marginBottom: 8 }}>{label}</div>
            <div style={{ height: 58, borderRadius: 12, background: "rgba(10,14,28,0.5)", border: `1px solid ${theme.glassEdge}`, fontSize: 26,
                          display: "flex", alignItems: "center", padding: "0 18px" }}>{value}</div>
          </div>
        ))}
        <div style={{ position: "absolute", right: 28, top: 70 }}><Chip text="Mac MCP · Filling the form" color={theme.teal} /></div>
      </Window>
      <Window x={1020} y={360} w={760} h={360} title="Notes" style={{ zIndex: 2 }}>
        <div style={{ fontSize: 26, lineHeight: 1.6 }}>
          {notes.slice(0, Math.max(0, Math.floor(frame / 2)))}
          <span style={{ opacity: Math.floor(frame / 12) % 2 ? 1 : 0 }}>|</span>
        </div>
        <div style={{ marginTop: 24 }}><Chip text="You keep working" color={theme.haze} /></div>
      </Window>
      <Caption text="Visible, non-focus-stealing browser work." start={0} end={seconds(7)} />
    </Desktop>
  );
};

// 23–30 s: a scheduled task runs and leaves a report.
const Scheduled: React.FC = () => {
  const frame = useCurrentFrame();
  const report = useEnter(70, 16);
  const steps = ["Looked for the right tool", "Read last week's leads", "Compared with the week before", "Wrote the summary"];
  return (
    <Desktop>
      <Window x={170} y={220} w={760} h={300} title="Scheduled Tasks">
        <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 24 }}>
          <span style={{ fontFamily: theme.serif, fontSize: 38, fontWeight: 600 }}>Scheduled Tasks</span>
          <Chip text="Experimental" color={theme.violet} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 18, padding: 20, borderRadius: 18, background: "rgba(10,14,28,0.45)" }}>
          <Glyph kind="sparkles" size={56} />
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 30, fontWeight: 650 }}>Weekly ad report</div>
            <div style={{ fontSize: 22, color: theme.haze }}>Every weekday at 08:30 · {frame > 60 ? <span style={{ color: theme.ok }}>Done this morning</span> : "Running now"}</div>
          </div>
          <span style={{ padding: "8px 18px", borderRadius: 12, background: theme.ember, color: theme.emberInk, fontWeight: 700, fontSize: 22 }}>Run now</span>
        </div>
      </Window>
      <Window x={980} y={170} w={780} h={420} title="Weekly ad report"
              style={{ opacity: report, transform: `translateY(${(1 - report) * 30}px)` }}>
        <div style={{ display: "flex", gap: 10, marginBottom: 22 }}>
          {["Result", "Steps"].map((tab, index) => (
            <span key={tab} style={{ padding: "6px 20px", borderRadius: 10, fontSize: 22, fontWeight: 600,
                                     background: index === 1 ? theme.working : "rgba(10,14,28,0.5)" }}>{tab}</span>
          ))}
        </div>
        {steps.map((step, index) => {
          const show = interpolate(frame, [90 + index * 18, 102 + index * 18], [0, 1], clamp);
          return (
            <div key={step} style={{ display: "flex", alignItems: "center", gap: 16, padding: "12px 0", fontSize: 26, opacity: show,
                                     transform: `translateX(${(1 - show) * 16}px)` }}>
              <span style={{ width: 38, height: 38, borderRadius: 19, background: `${theme.working}33`, display: "grid", placeItems: "center" }}>
                <Glyph kind={index === 0 ? "sparkles" : index === 3 ? "notes" : "browser"} size={24} />
              </span>
              <span>{step}</span>
            </div>
          );
        })}
      </Window>
      <Caption text="Hand off work. Get a report back." start={0} end={seconds(7)} />
    </Desktop>
  );
};

// 30–36 s: risky actions wait for you; pause and private mode are one click.
const Charge: React.FC = () => {
  const frame = useCurrentFrame();
  const card = useEnter(4, 14);
  const toggles = useEnter(70, 16);
  const pressed = frame > 50;
  return (
    <Desktop>
      <div style={{ position: "absolute", left: 300, top: 230, width: 640, padding: 34, borderRadius: 26, background: theme.glass,
                    border: `1px solid ${theme.glassEdge}`, boxShadow: "0 40px 100px rgba(0,0,0,0.5)",
                    opacity: card, transform: `scale(${0.94 + 0.06 * card})` }}>
        <div style={{ fontFamily: theme.serif, fontSize: 40, fontWeight: 600, marginBottom: 10 }}>Send this email?</div>
        <div style={{ fontSize: 24, color: theme.haze, marginBottom: 28 }}>To Ayşe: “The report is attached.”</div>
        <div style={{ display: "flex", gap: 16, justifyContent: "flex-end" }}>
          <span style={{ padding: "12px 26px", borderRadius: 14, background: "rgba(10,14,28,0.5)", fontSize: 24, fontWeight: 600 }}>Don’t send</span>
          <span style={{ padding: "12px 26px", borderRadius: 14, background: theme.ember, color: theme.emberInk, fontSize: 24, fontWeight: 700,
                         transform: pressed ? "scale(0.95)" : "none" }}>Send</span>
        </div>
      </div>
      <div style={{ position: "absolute", right: 260, top: 280, width: 560, padding: 30, borderRadius: 24, background: theme.glass,
                    border: `1px solid ${theme.glassEdge}`, opacity: toggles, transform: `translateY(${(1 - toggles) * 24}px)` }}>
        {["Pause observation", "Private mode"].map((label, index) => {
          const on = frame > 100 + index * 20;
          return (
            <div key={label} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 0", fontSize: 28 }}>
              <span>{label}</span>
              <span style={{ width: 74, height: 42, borderRadius: 21, background: on ? theme.working : "rgba(255,255,255,0.18)", position: "relative" }}>
                <span style={{ position: "absolute", top: 4, left: on ? 36 : 4, width: 34, height: 34, borderRadius: 17, background: "#fff" }} />
              </span>
            </div>
          );
        })}
      </div>
      <Caption text="You stay in charge." start={0} end={seconds(6)} />
    </Desktop>
  );
};

// 36–40 s: the promise and where to get it.
const End: React.FC = () => {
  const enter = useEnter(0, 15);
  return (
    <AbsoluteFill style={{ background: theme.night, color: theme.paper, fontFamily: theme.sans, alignItems: "center", justifyContent: "center" }}>
      <AbsoluteFill style={{ background: `radial-gradient(900px 600px at 50% 40%, ${theme.violet}40, transparent 70%)` }} />
      <div style={{ textAlign: "center", opacity: enter, transform: `translateY(${(1 - enter) * 24}px)` }}>
        <AppIcon size={184} />
        <div style={{ fontFamily: theme.serif, fontSize: 84, fontWeight: 600, marginTop: 34 }}>Let your AI apps use your Mac.</div>
        <div style={{ fontSize: 34, color: theme.haze, marginTop: 18 }}>Mac MCP · Open source, MIT · bulutarkan.github.io/mac-mcp</div>
      </div>
    </AbsoluteFill>
  );
};

export const MacMcpDemo: React.FC = () => (
  <AbsoluteFill style={{ background: theme.night }}>
    <Sequence durationInFrames={seconds(4)}><Problem /></Sequence>
    <Sequence from={seconds(4)} durationInFrames={seconds(5)}><Connect /></Sequence>
    <Sequence from={seconds(9)} durationInFrames={seconds(7)}><Island /></Sequence>
    <Sequence from={seconds(16)} durationInFrames={seconds(7)}><Browser /></Sequence>
    <Sequence from={seconds(23)} durationInFrames={seconds(7)}><Scheduled /></Sequence>
    <Sequence from={seconds(30)} durationInFrames={seconds(6)}><Charge /></Sequence>
    <Sequence from={seconds(36)} durationInFrames={seconds(4)}><End /></Sequence>
  </AbsoluteFill>
);
