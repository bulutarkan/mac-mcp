"""#125: the Voice fields save only a value edge-tts takes, on Return or when left, and the OpenAI
Decisions scope picker never says it is in use while it is Off."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
RULES = SOURCES / "SettingsRules.swift"
VIEW = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")

HARNESS = r'''
import Foundation

@main
struct RulesHarness {
    static func main() {
        // The voice names edge-tts accepts (its own pattern), and nothing else.
        for good in ["tr-TR-AhmetNeural", "en-US-AriaNeural", " en-GB-SoniaNeural ", "zh-CN-liaoning-XiaobeiNeural",
                     "fil-PH-AngeloNeural", "Microsoft Server Speech Text to Speech Voice (cy-GB, NiaNeural)"] {
            precondition(VoiceSettingsFormat.voice(good) != nil, "voice \(good) refused")
        }
        precondition(VoiceSettingsFormat.voice(" en-GB-SoniaNeural ") == "en-GB-SoniaNeural", "voice is trimmed")
        for bad in ["", "Ahmet", "tr-TR-Ahmet", "TR-tr-AhmetNeural", "tr_TR_AhmetNeural", "tr-TR-"] {
            precondition(VoiceSettingsFormat.voice(bad) == nil, "voice \(bad) taken")
        }
        // Rates as a signed percentage; a bare number is read as one.
        let rates: [(String, String?)] = [
            ("-5%", "-5%"), ("+10%", "+10%"), ("10%", "+10%"), ("10", "+10%"), ("-5", "-5%"), (" +0% ", "+0%"),
            ("fast", nil), ("", nil), ("5%%", nil), ("+-5%", nil), ("1.5%", nil), ("1000%", nil), ("%", nil),
        ]
        for (typed, saved) in rates {
            precondition(VoiceSettingsFormat.rate(typed) == saved, "rate \(typed) -> \(String(describing: VoiceSettingsFormat.rate(typed)))")
        }
        precondition(VoiceSettingsFormat.voiceProblem("Ahmet").contains("Not saved"), "the note says it was not saved")
        precondition(VoiceSettingsFormat.rateProblem("fast").contains("-5% or +10%"), "the note shows the format")

        // Decisions: the four choices, and a status that follows them.
        precondition(DecisionScope.allCases.map { $0.title } == ["Off", "Browser", "Mac apps", "Both"], "titles")
        precondition(DecisionScope.allCases.map { $0.rawValue } == ["off", "browser", "native", "both"], "values")
        for key in ["valid", "missing", "invalid", "unverified"] {
            for check in [nil, "valid", "timeout"] as [String?] {
                let off = DecisionScope.statusText(scope: .off, keyStatus: key, checkStatus: check)
                precondition(off.hasPrefix("Off"), off)
                precondition(!off.lowercased().contains("active") && !off.contains("used for"), "Off says in use: \(off)")
            }
        }
        precondition(DecisionScope.statusText(scope: .browser, keyStatus: "valid", checkStatus: "valid")
                     == "Key verified · used for browser pages", "browser")
        precondition(DecisionScope.statusText(scope: .native, keyStatus: "valid", checkStatus: nil)
                     == "Key verified · used for Mac apps", "native")
        precondition(DecisionScope.statusText(scope: .both, keyStatus: "valid", checkStatus: nil)
                     == "Key verified · used for browser pages and Mac apps", "both")
        precondition(!DecisionScope.statusText(scope: .both, keyStatus: "missing", checkStatus: nil).contains("used for"), "no key")
        precondition(!DecisionScope.statusText(scope: .both, keyStatus: "invalid", checkStatus: nil).contains("used for"), "bad key")
        print("SETTINGS_RULES_PASS")
    }
}
'''


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class SettingsRulesHarnessTests(unittest.TestCase):
    def test_voice_values_and_decision_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "RulesHarness.swift"
            binary = Path(td) / "settings-rules-harness"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(RULES), str(harness), "-o", str(binary)],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("SETTINGS_RULES_PASS", run.stdout)


class VoiceFieldSourceTests(unittest.TestCase):
    def test_voice_name_and_rate_save_on_commit_not_while_typing(self) -> None:
        voice = VIEW[VIEW.index("private var voicePane"):VIEW.index("private var clientsPane")]
        # Typing changes only the draft; nothing persists on each keystroke.
        self.assertNotIn(".onChange(of: settings.voiceName)", voice)
        self.assertNotIn(".onChange(of: settings.ttsRate)", voice)
        self.assertNotIn("text: $settings.voiceName", voice)
        self.assertNotIn("text: $settings.ttsRate", voice)
        self.assertIn("set: { voiceNameDraft = $0 }", voice)
        self.assertIn("set: { ttsRateDraft = $0 }", voice)
        # Return, leaving the field and leaving the pane commit.
        self.assertIn(".onSubmit { commitVoiceName() }", voice)
        self.assertIn(".onSubmit { commitTTSRate() }", voice)
        self.assertIn("if voiceField == .name { commitVoiceName() }", voice)
        self.assertIn("if voiceField == .rate { commitTTSRate() }", voice)
        self.assertIn(".onDisappear {", voice)
        self.assertIn("such as tr-TR-AhmetNeural", voice)
        self.assertIn("such as -5% or +10%", voice)
        commit = VIEW[VIEW.index("private func commitVoiceName()"):VIEW.index("private func saveGroqKey()")]
        # A value edge-tts would refuse stays in the field with a note and is not saved.
        self.assertLess(commit.index("voiceNameProblem = VoiceSettingsFormat.voiceProblem(draft)"),
                        commit.index("settings.voiceName = value"))
        self.assertLess(commit.index("ttsRateProblem = VoiceSettingsFormat.rateProblem(draft)"),
                        commit.index("settings.ttsRate = value"))
        self.assertEqual(2, commit.count('persistSettings(scope: .voice, success: "Saved · applies live")'))
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("Sources/SettingsRules.swift", build)


class DecisionScopeSourceTests(unittest.TestCase):
    def test_scope_is_a_picker_saved_through_the_settings_merge(self) -> None:
        box = VIEW[VIEW.index("private var decisionAccelerationBox"):VIEW.index("private var currentUpdateCommit")]
        self.assertIn('Picker("Use OpenAI Decisions", selection: decisionScopeBinding)', box)
        self.assertIn("ForEach(DecisionScope.allCases)", box)
        self.assertNotIn("Toggle(", box)
        self.assertNotIn("Decision layer active", VIEW)
        binding = box[box.index("private var decisionScopeBinding"):box.index("private var decisionKeyStatusText")]
        self.assertIn("settings.decisionAccelerationEnabled = scope != .off", binding)
        self.assertIn("settings.decisionAccelerationScope = scope.rawValue", binding)
        # The existing merge-and-write save: unrelated fields in settings.json are kept.
        self.assertIn("persistSettings(scope: .decisions", binding)
        shown = box[box.index("private var decisionScope: DecisionScope"):box.index("private var decisionScopeBinding")]
        self.assertIn("guard settings.decisionAccelerationEnabled else { return .off }", shown)
        store = (SOURCES / "SettingsStore.swift").read_text(encoding="utf-8")
        self.assertIn("decision_acceleration: .init(enabled: decisionAccelerationEnabled, scope: decisionAccelerationScope)", store)
        self.assertIn("var merged = Self.deepMerge(existing, payloadObject)", store)


if __name__ == "__main__":
    unittest.main()
