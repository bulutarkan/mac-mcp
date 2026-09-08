import Foundation
import AVFoundation
import AudioToolbox
import CoreAudio

struct InputDevice {
    let id: AudioDeviceID
    let uid: String
    let name: String
}

enum SystemAudioInput {
    static func devices() -> [InputDevice] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        let systemObject = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(systemObject, &address, 0, nil, &size) == noErr else {
            return []
        }
        let count = Int(size) / MemoryLayout<AudioDeviceID>.size
        var ids = [AudioDeviceID](repeating: 0, count: count)
        let status = ids.withUnsafeMutableBytes { bytes in
            AudioObjectGetPropertyData(systemObject, &address, 0, nil, &size, bytes.baseAddress!)
        }
        guard status == noErr else { return [] }
        return ids.compactMap { id in
            guard inputChannelCount(for: id) > 0,
                  let uid = stringProperty(kAudioDevicePropertyDeviceUID, deviceID: id),
                  let name = stringProperty(kAudioObjectPropertyName, deviceID: id) else {
                return nil
            }
            return InputDevice(id: id, uid: uid, name: name)
        }
    }

    static func defaultDeviceName() -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var deviceID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size,
            &deviceID
        )
        guard status == noErr, deviceID != 0 else { return "System Default" }
        return stringProperty(kAudioObjectPropertyName, deviceID: deviceID) ?? "System Default"
    }

    static func configure(_ inputNode: AVAudioInputNode, deviceUID: String?) throws {
        guard let deviceUID, !deviceUID.isEmpty else { return }
        guard let resolved = deviceID(forUID: deviceUID), let audioUnit = inputNode.audioUnit else {
            throw NSError(domain: "MacMCPVoiceHelper", code: 11, userInfo: [NSLocalizedDescriptionKey: "Input device is unavailable"])
        }
        var deviceID = resolved
        let status = AudioUnitSetProperty(
            audioUnit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &deviceID,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )
        guard status == noErr else {
            throw NSError(domain: "MacMCPVoiceHelper", code: Int(status), userInfo: [NSLocalizedDescriptionKey: "Could not configure input device (Core Audio \(status))"])
        }
    }

    private static func deviceID(forUID uid: String) -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var uidValue: CFString = uid as CFString
        let qualifierSize = UInt32(MemoryLayout<CFString>.size)
        var deviceID = AudioDeviceID(0)
        var dataSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = withUnsafePointer(to: &uidValue) { pointer in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                qualifierSize,
                pointer,
                &dataSize,
                &deviceID
            )
        }
        return status == noErr && deviceID != 0 ? deviceID : nil
    }

    private static func inputChannelCount(for deviceID: AudioDeviceID) -> UInt32 {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &size) == noErr, size > 0 else {
            return 0
        }
        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: Int(size),
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, raw) == noErr else {
            return 0
        }
        let list = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        return UnsafeMutableAudioBufferListPointer(list).reduce(0) { $0 + $1.mNumberChannels }
    }

    private static func stringProperty(_ selector: AudioObjectPropertySelector, deviceID: AudioDeviceID) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &value)
        guard status == noErr, let value else { return nil }
        return value.takeUnretainedValue() as String
    }
}

struct HelperResult: Codable {
    let ok: Bool
    let status: String
    let audio_path: String?
    let input_device: String?
    let timed_out: Bool
    let error: String?
}

enum RecordAttempt {
    case success(String)
    case noFrames(String)
    case timedOut(String)
    case failure(String)
}

func argument(_ name: String, default defaultValue: String) -> String {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: name), index + 1 < args.count else { return defaultValue }
    return args[index + 1]
}

func writeResult(_ result: HelperResult, path: String) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    if let data = try? encoder.encode(result) {
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
    }
}

func record(
    outputPath: String,
    timeoutSeconds: Double,
    device: InputDevice?,
    displayName: String,
    failFastIfNoFrames: Bool
) -> RecordAttempt {
    try? FileManager.default.removeItem(atPath: outputPath)
    let engine = AVAudioEngine()
    let input = engine.inputNode
    do {
        try SystemAudioInput.configure(input, deviceUID: device?.uid)
    } catch {
        return .failure(error.localizedDescription)
    }

    let format = input.outputFormat(forBus: 0)
    guard format.sampleRate > 0, format.channelCount > 0 else {
        return .failure("The selected microphone did not provide a usable audio format")
    }

    let file: AVAudioFile
    do {
        file = try AVAudioFile(forWriting: URL(fileURLWithPath: outputPath), settings: format.settings)
    } catch {
        return .failure("Could not create temporary audio file: \(error.localizedDescription)")
    }

    let lock = NSLock()
    var framesSeen = 0
    var heardSpeech = false
    var lastSpeechAt = Date.distantPast

    input.installTap(onBus: 0, bufferSize: 512, format: format) { buffer, _ in
        try? file.write(from: buffer)
        guard let channels = buffer.floatChannelData else { return }
        let frameCount = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)
        guard frameCount > 0, channelCount > 0 else { return }
        var sumSquares: Float = 0
        for channel in 0..<channelCount {
            let samples = channels[channel]
            for frame in 0..<frameCount {
                let sample = samples[frame]
                sumSquares += sample * sample
            }
        }
        let rms = sqrt(sumSquares / Float(frameCount * channelCount))
        let decibels = 20 * log10(max(rms, 0.000001))
        lock.lock()
        framesSeen += frameCount
        if decibels > -48.0 {
            heardSpeech = true
            lastSpeechAt = Date()
        }
        lock.unlock()
    }

    engine.prepare()
    do {
        try engine.start()
    } catch {
        input.removeTap(onBus: 0)
        return .failure("Could not start microphone capture: \(error.localizedDescription)")
    }

    let started = Date()
    var noFrames = false
    while Date().timeIntervalSince(started) < timeoutSeconds {
        RunLoop.current.run(until: Date().addingTimeInterval(0.08))
        lock.lock()
        let currentFrames = framesSeen
        let didHearSpeech = heardSpeech
        let silenceDuration = didHearSpeech ? Date().timeIntervalSince(lastSpeechAt) : 0
        lock.unlock()

        if didHearSpeech && silenceDuration >= 1.25 { break }
        if failFastIfNoFrames && Date().timeIntervalSince(started) >= 1.8 && currentFrames == 0 {
            noFrames = true
            break
        }
    }

    input.removeTap(onBus: 0)
    engine.stop()

    lock.lock()
    let finalFrames = framesSeen
    let finalHeardSpeech = heardSpeech
    lock.unlock()

    if noFrames || finalFrames == 0 {
        try? FileManager.default.removeItem(atPath: outputPath)
        return .noFrames(displayName)
    }
    if !finalHeardSpeech {
        try? FileManager.default.removeItem(atPath: outputPath)
        return .timedOut(displayName)
    }
    return .success(displayName)
}

let resultPath = argument("--result", default: "/tmp/mac-mcp-voice-result.json")
let audioPath = argument("--audio", default: "/tmp/mac-mcp-voice-response.wav")
let inputMode = argument("--input", default: "auto")
let timeoutSeconds = max(2.0, min(Double(argument("--timeout", default: "45")) ?? 45.0, 300.0))
try? FileManager.default.removeItem(atPath: resultPath)
try? FileManager.default.removeItem(atPath: audioPath)

let permissionSemaphore = DispatchSemaphore(value: 0)
var permissionGranted = false
AVCaptureDevice.requestAccess(for: .audio) { granted in
    permissionGranted = granted
    permissionSemaphore.signal()
}
_ = permissionSemaphore.wait(timeout: .now() + 30)

guard permissionGranted else {
    writeResult(
        HelperResult(ok: false, status: "error", audio_path: nil, input_device: nil, timed_out: false, error: "Microphone permission denied or timed out"),
        path: resultPath
    )
    exit(1)
}

let devices = SystemAudioInput.devices()
let builtIn = devices.first { device in
    let lowered = device.name.lowercased()
    return lowered.contains("macbook") && lowered.contains("microphone")
}

let firstAttempt: RecordAttempt
if inputMode == "built-in", let builtIn {
    firstAttempt = record(outputPath: audioPath, timeoutSeconds: timeoutSeconds, device: builtIn, displayName: builtIn.name, failFastIfNoFrames: false)
} else if inputMode.hasPrefix("name:") {
    let wanted = String(inputMode.dropFirst(5)).trimmingCharacters(in: .whitespacesAndNewlines)
    if let chosen = devices.first(where: { $0.name.caseInsensitiveCompare(wanted) == .orderedSame })
        ?? devices.first(where: { $0.name.localizedCaseInsensitiveContains(wanted) }) {
        firstAttempt = record(outputPath: audioPath, timeoutSeconds: timeoutSeconds, device: chosen, displayName: chosen.name, failFastIfNoFrames: false)
    } else {
        firstAttempt = .failure("Requested microphone was not found: \(wanted)")
    }
} else {
    firstAttempt = record(
        outputPath: audioPath,
        timeoutSeconds: timeoutSeconds,
        device: nil,
        displayName: SystemAudioInput.defaultDeviceName(),
        failFastIfNoFrames: true
    )
}

let finalAttempt: RecordAttempt
switch firstAttempt {
case .noFrames where inputMode == "auto" && builtIn != nil:
    finalAttempt = record(
        outputPath: audioPath,
        timeoutSeconds: timeoutSeconds,
        device: builtIn,
        displayName: builtIn!.name,
        failFastIfNoFrames: false
    )
default:
    finalAttempt = firstAttempt
}

switch finalAttempt {
case .success(let deviceName):
    writeResult(
        HelperResult(ok: true, status: "recorded", audio_path: audioPath, input_device: deviceName, timed_out: false, error: nil),
        path: resultPath
    )
    exit(0)
case .timedOut(let deviceName):
    writeResult(
        HelperResult(ok: true, status: "timed_out", audio_path: nil, input_device: deviceName, timed_out: true, error: nil),
        path: resultPath
    )
    exit(0)
case .noFrames(let deviceName):
    writeResult(
        HelperResult(ok: false, status: "error", audio_path: nil, input_device: deviceName, timed_out: false, error: "The microphone produced no audio frames"),
        path: resultPath
    )
    exit(1)
case .failure(let message):
    writeResult(
        HelperResult(ok: false, status: "error", audio_path: nil, input_device: nil, timed_out: false, error: message),
        path: resultPath
    )
    exit(1)
}
