import Foundation
import CoreAudio

struct AudioDeviceInfo {
    let id: AudioDeviceID
    let name: String
    let uid: String
}

func stringProperty(_ selector: AudioObjectPropertySelector, id: AudioDeviceID) -> String? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr,
          let value else { return nil }
    return value.takeUnretainedValue() as String
}

func devices() -> [AudioDeviceInfo] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    let status = ids.withUnsafeMutableBytes { bytes in
        AudioObjectGetPropertyData(system, &address, 0, nil, &size, bytes.baseAddress!)
    }
    guard status == noErr else { return [] }
    return ids.compactMap { id in
        guard let name = stringProperty(kAudioObjectPropertyName, id: id),
              let uid = stringProperty(kAudioDevicePropertyDeviceUID, id: id) else { return nil }
        return AudioDeviceInfo(id: id, name: name, uid: uid)
    }
}

func defaultDeviceID(_ selector: AudioObjectPropertySelector) -> AudioDeviceID? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var id = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject),
        &address,
        0,
        nil,
        &size,
        &id
    )
    return status == noErr && id != 0 ? id : nil
}

func setDefault(_ selector: AudioObjectPropertySelector, id: AudioDeviceID) -> Bool {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var mutableID = id
    let size = UInt32(MemoryLayout<AudioDeviceID>.size)
    return AudioObjectSetPropertyData(
        AudioObjectID(kAudioObjectSystemObject),
        &address,
        0,
        nil,
        size,
        &mutableID
    ) == noErr
}

func setOutput(_ device: AudioDeviceInfo) -> Bool {
    // Voice prompts should not disturb the user's system-alert output route.
    return setDefault(kAudioHardwarePropertyDefaultOutputDevice, id: device.id)
}

let args = CommandLine.arguments
let allDevices = devices()

func description(_ id: AudioDeviceID?) -> String {
    guard let id, let device = allDevices.first(where: { $0.id == id }) else { return "unknown|unknown" }
    return "\(device.name)|\(device.uid)"
}

if args.count == 1 || args[1] == "get" {
    print("output=" + description(defaultDeviceID(kAudioHardwarePropertyDefaultOutputDevice)))
    print("system=" + description(defaultDeviceID(kAudioHardwarePropertyDefaultSystemOutputDevice)))
    exit(0)
}

let command = args[1]
let selected: AudioDeviceInfo?
if command == "set-uid", args.count >= 3 {
    selected = allDevices.first(where: { $0.uid == args[2] })
} else if command == "set-name", args.count >= 3 {
    let wanted = args[2].lowercased()
    selected = allDevices.first(where: { $0.name.lowercased() == wanted })
        ?? allDevices.first(where: { $0.name.lowercased().contains(wanted) })
} else if command == "set-builtin" {
    selected = allDevices.first(where: {
        let name = $0.name.lowercased()
        return name.contains("speakers") && !name.contains("airpods") && !name.contains("headphones")
    })
} else {
    selected = nil
}

guard let selected else {
    fputs("audio output device not found\n", stderr)
    exit(2)
}

let ok = setOutput(selected)
print("set=\(selected.name)|\(selected.uid)|ok=\(ok)")
exit(ok ? 0 : 3)
