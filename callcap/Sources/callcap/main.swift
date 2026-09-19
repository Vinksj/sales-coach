// callcap: dual-channel call capture for Sales Coach.
//
//   channel 0 = me    microphone through AVAudioEngine with Apple voice processing
//                     (echo cancellation) when it initialises, plain mic otherwise.
//   channel 1 = them  system audio through a Core Audio process tap, using
//                     AudioTeeCore (vendored, MIT License, Copyright 2025 Nick Payne,
//                     https://github.com/makeusabrew/audiotee). Spike S1 showed our
//                     own tap setup failing AudioDeviceStart ('nope') where AudioTee's
//                     starts, so the tap path is theirs, unmodified.
//
// stdout carries binary frames only:
//   [0xCA][u8 channel][u64 LE host_time_ns][u32 LE n][n x s16le mono @ sample rate]
// stderr carries JSON status lines (starting, *_started, heartbeat, error, stopped).

import AVFoundation
import AudioTeeCore
import AudioToolbox
import CoreAudio
import Foundation

// MARK: - status output

func emit(_ fields: [String: Any]) {
    var record = fields
    record["ts"] = Date().timeIntervalSince1970
    guard let data = try? JSONSerialization.data(withJSONObject: record),
          let line = String(data: data, encoding: .utf8) else { return }
    FileHandle.standardError.write((line + "\n").data(using: .utf8)!)
}

/// Routes AudioTee's own logging into our status stream; debug/info stay quiet.
final class StatusLogger: AudioTeeLogger {
    func debug(_ message: String, context: [String: String]?) {}
    func info(_ message: String, context: [String: String]?) {}
    func error(_ message: String, context: [String: String]?) {
        var record: [String: Any] = ["event": "warning", "source": "audiotee", "message": message]
        if let context = context { record["context"] = context }
        emit(record)
    }
}

// MARK: - options

struct Options {
    var sampleRate: Double = 16000
    var mic = true
    var system = true
    var voiceProcessing = true
    var duration: Double? = nil
}

func parseOptions() -> Options {
    var options = Options()
    var args = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = args.next() {
        switch arg {
        case "--sample-rate":
            if let value = args.next(), let rate = Double(value) { options.sampleRate = rate }
        case "--no-mic": options.mic = false
        case "--no-system": options.system = false
        case "--no-vp": options.voiceProcessing = false
        case "--duration":
            if let value = args.next(), let seconds = Double(value) { options.duration = seconds }
        case "-h", "--help":
            print("usage: callcap [--sample-rate 16000] [--no-mic] [--no-system] [--no-vp] [--duration SECONDS]")
            exit(0)
        default:
            emit(["event": "warning", "message": "unknown argument \(arg)"])
        }
    }
    return options
}

// MARK: - frame writer

final class FrameWriter {
    private let queue = DispatchQueue(label: "callcap.writer")
    private var samples: [UInt8: Int] = [0: 0, 1: 0]
    private var peaks: [UInt8: Int] = [0: 0, 1: 0]

    func write(channel: UInt8, hostTimeNs: UInt64, pcm: UnsafePointer<Int16>, count: Int) {
        guard count > 0 else { return }
        var peak = 0
        for i in 0..<count {
            let magnitude = abs(Int(pcm[i]))
            if magnitude > peak { peak = magnitude }
        }
        var frame = Data(capacity: 14 + count * 2)
        frame.append(0xCA)
        frame.append(channel)
        var time = hostTimeNs.littleEndian
        withUnsafeBytes(of: &time) { frame.append(contentsOf: $0) }
        var n = UInt32(count).littleEndian
        withUnsafeBytes(of: &n) { frame.append(contentsOf: $0) }
        frame.append(UnsafeBufferPointer(start: pcm, count: count))
        queue.async {
            self.samples[channel, default: 0] += count
            if peak > self.peaks[channel, default: 0] { self.peaks[channel] = peak }
            do {
                try FileHandle.standardOutput.write(contentsOf: frame)
            } catch {
                emit(["event": "stdout_closed"])
                exit(0)
            }
        }
    }

    /// Totals since start, peaks since the previous snapshot.
    func snapshot() -> [String: Any] {
        return queue.sync {
            let result: [String: Any] = [
                "samples_me": samples[0] ?? 0, "samples_them": samples[1] ?? 0,
                "peak_me": peaks[0] ?? 0, "peak_them": peaks[1] ?? 0,
            ]
            peaks = [0: 0, 1: 0]
            return result
        }
    }
}

// MARK: - resampling to 16 kHz mono s16le (microphone path)

final class Resampler {
    private let converter: AVAudioConverter
    private let outFormat: AVAudioFormat
    private let monoFormat: AVAudioFormat?
    private var warned = false

    init?(from inFormat: AVAudioFormat, rate: Double) {
        guard let out = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: rate,
                                      channels: 1, interleaved: true) else { return nil }
        var source = inFormat
        var mono: AVAudioFormat?
        if inFormat.channelCount > 1 {
            // AVAudioConverter's downmix does not handle every device layout: a 3-channel default
            // input (2026-09-12, a virtual audio device) produced no frames and no error at all.
            // Average the channels ourselves first, then only the sample rate has to change.
            guard let m = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inFormat.sampleRate,
                                        channels: 1, interleaved: false) else { return nil }
            mono = m
            source = m
        }
        guard let converter = AVAudioConverter(from: source, to: out) else { return nil }
        converter.downmix = true
        self.converter = converter
        self.outFormat = out
        self.monoFormat = mono
    }

    private func warnOnce(_ message: String) {
        guard !warned else { return }
        warned = true
        emit(["event": "warning", "source": "resampler", "message": message])
    }

    /// Average every channel into one. Only for non-interleaved float32, which is what an
    /// AVAudioEngine input tap delivers; anything else falls through to the converter.
    private func toMono(_ buffer: AVAudioPCMBuffer, _ mono: AVAudioFormat) -> AVAudioPCMBuffer? {
        guard buffer.format.commonFormat == .pcmFormatFloat32, !buffer.format.isInterleaved,
              let source = buffer.floatChannelData,
              let mixed = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: buffer.frameLength),
              let target = mixed.floatChannelData?[0] else {
            warnOnce("cannot mix \(buffer.format) down to mono; passing it to the converter")
            return nil
        }
        let frames = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        for frame in 0..<frames {
            var sum: Float = 0
            for channel in 0..<channels { sum += source[channel][frame] }
            target[frame] = sum / Float(channels)
        }
        mixed.frameLength = buffer.frameLength
        return mixed
    }

    func convert(_ buffer: AVAudioPCMBuffer, _ handler: (UnsafePointer<Int16>, Int) -> Void) {
        let input = monoFormat.flatMap { toMono(buffer, $0) } ?? buffer
        let ratio = outFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        _ = converter.convert(to: out, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return input
        }
        if let error = error {
            warnOnce("convert from \(input.format): \(error.localizedDescription)")
            return
        }
        if out.frameLength == 0 {
            warnOnce("converter returned no frames from \(input.format)")
            return
        }
        if let channel = out.int16ChannelData?[0] {
            handler(channel, Int(out.frameLength))
        }
    }
}

enum CaptureError: Error, CustomStringConvertible {
    case format(String)
    var description: String {
        switch self {
        case .format(let what): return what
        }
    }
}

// MARK: - system audio (them), via AudioTeeCore

/// Receives AudioTee's converted chunks (s16le mono at our rate) and frames them.
final class TapOutput: AudioOutputHandler {
    private let writer: FrameWriter
    private let rate: Double

    init(writer: FrameWriter, rate: Double) {
        self.writer = writer
        self.rate = rate
    }

    // Timestamps are anchored to the sample count, not to each chunk's delivery time: a chunk that
    // arrives late (an IOProc stall while the model runs) would otherwise insert silence that is
    // never taken back, shifting every later 'them' sample against 'me' by the worst stall seen.
    private var anchorNs: UInt64 = 0
    private var delivered: Int = 0
    private let reanchorNs: UInt64 = 1_000_000_000      // a real dropout: the wall clock disagrees by > 1 s

    func handleAudioData(_ pointer: UnsafeRawPointer, count: Int) {
        let n = count / 2
        guard n > 0 else { return }
        let samples = pointer.bindMemory(to: Int16.self, capacity: n)
        // AudioTee hands over a finished chunk; its first sample was captured n/rate seconds ago.
        let nowNs = AudioConvertHostTimeToNanos(mach_absolute_time())
        let wallStartNs = nowNs &- UInt64(Double(n) / rate * 1_000_000_000)
        let expectedNs = anchorNs &+ UInt64(Double(delivered) / rate * 1_000_000_000)
        let drift = wallStartNs > expectedNs ? wallStartNs - expectedNs : expectedNs - wallStartNs
        if anchorNs == 0 || drift > reanchorNs {
            anchorNs = wallStartNs
            delivered = 0
        }
        let startNs = anchorNs &+ UInt64(Double(delivered) / rate * 1_000_000_000)
        delivered += n
        writer.write(channel: 1, hostTimeNs: startNs, pcm: samples, count: n)
    }

    func handleMetadata(_ metadata: AudioStreamMetadata) {}
    func handleStreamStart() {}
    func handleStreamStop() {}
}

final class SystemTap {
    private var manager: AudioTapManager?
    private var recorder: AudioRecorder?

    func start(writer: FrameWriter, rate: Double) throws {
        AudioTeeLogging.logger = StatusLogger()
        let manager = AudioTapManager()
        try manager.setupAudioTap(with: TapConfiguration(processes: [], muteBehavior: .unmuted,
                                                         isExclusive: true, isMono: true))
        guard let device = manager.getDeviceID() else { throw CaptureError.format("no aggregate device") }
        let recorder = try AudioRecorder(deviceID: device, outputHandler: TapOutput(writer: writer, rate: rate),
                                         convertToSampleRate: rate, chunkDuration: 0.1)
        let format = recorder.outputFormat
        guard format.mSampleRate == rate, format.mBitsPerChannel == 16, format.mChannelsPerFrame == 1 else {
            throw CaptureError.format("tap output is \(format.mSampleRate) Hz, \(format.mBitsPerChannel) bit, "
                                      + "\(format.mChannelsPerFrame) ch; expected \(rate) Hz 16-bit mono")
        }
        try recorder.startRecording()
        self.manager = manager
        self.recorder = recorder
        emit(["event": "system_started", "converting": recorder.isConverting])
    }

    func stop() {
        recorder?.stopRecording()
        recorder = nil
        manager = nil      // AudioTapManager's deinit destroys the tap and aggregate device
    }
}

// MARK: - microphone (me)

func microphoneAllowed() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
        return true
    case .notDetermined:
        let done = DispatchSemaphore(value: 0)
        var granted = false
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            granted = ok
            done.signal()
        }
        done.wait()
        return granted
    default:
        return false
    }
}

final class MicCapture {
    private let engine = AVAudioEngine()
    private var resampler: Resampler?
    private var buffersSeen = 0

    func start(writer: FrameWriter, rate: Double, voiceProcessing: Bool) throws {
        let input = engine.inputNode
        if voiceProcessing {
            // Instantiate the main mixer BEFORE enabling voice processing.
            // Accessing it connects it to the output node, giving the
            // voice-processing I/O unit the output side it initialises. Enabling
            // VP first fails -10875 (kAUInitialize on outputNode), which then
            // drops us to the raw 3-channel fallback.
            engine.mainMixerNode.outputVolume = 0
            try input.setVoiceProcessingEnabled(true)
            if #available(macOS 14.0, *) {
                // Do not duck the call audio while voice processing runs.
                input.voiceProcessingOtherAudioDuckingConfiguration =
                    AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false,
                                                                         duckingLevel: .min)
            }
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            throw CaptureError.format("microphone format unusable: \(format)")
        }
        // Route the mic into a silent mixer, in BOTH paths. Two reasons, and the
        // second one cost a debugging round: (1) the voice-processing I/O unit
        // initialises the output side too and fails (-10875) unless the graph
        // renders to output; (2) without any rendering path the engine never pulls
        // the input node, so installTap's callback is never invoked at all — the
        // engine starts, reports mic_started, and delivers zero buffers in silence.
        engine.connect(input, to: engine.mainMixerNode, format: nil)
        engine.mainMixerNode.outputVolume = 0
        // Pass nil, so the engine taps in the node's OWN format. Reading the format
        // ourselves is what silenced this tap: the node reported 3 channels on one
        // run and 5 on the next, and a channel count the node disagrees with makes
        // the engine drop the tap with no error and no callback. Whatever arrives,
        // the resampler is built from that first buffer and mixed down to mono.
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, when in
            guard let self = self else { return }
            if self.resampler == nil {
                emit(["event": "mic_format", "format": "\(buffer.format)",
                      "channels": Int(buffer.format.channelCount),
                      "rate": buffer.format.sampleRate])
                self.resampler = Resampler(from: buffer.format, rate: rate)
                if self.resampler == nil {
                    emit(["event": "warning", "source": "mic",
                          "message": "no resampler for \(buffer.format)"])
                }
            }
            self.buffersSeen += 1
            let nanos = AudioConvertHostTimeToNanos(when.isHostTimeValid ? when.hostTime : mach_absolute_time())
            self.resampler?.convert(buffer) { samples, count in
                writer.write(channel: 0, hostTimeNs: nanos, pcm: samples, count: count)
            }
        }
        engine.prepare()
        try engine.start()
        // Voice processing starts cleanly and then delivers nothing: with VP on,
        // this input node reports a 5-channel layout and its tap is never called,
        // while plain input gives 1 ch 48 kHz float and works. A successful start()
        // is therefore not evidence of audio, and the caller's no-VP fallback never
        // ran because nothing threw. Make VP prove itself with a real buffer.
        if voiceProcessing {
            let deadline = Date().addingTimeInterval(1.5)
            while buffersSeen == 0 && Date() < deadline { usleep(50_000) }
            if buffersSeen == 0 {
                input.removeTap(onBus: 0)
                engine.stop()
                throw CaptureError.format("voice processing started but produced no audio")
            }
        }
        emit(["event": "mic_started", "mic_rate": format.sampleRate,
              "mic_channels": Int(format.channelCount), "voice_processing": voiceProcessing])
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        // A tap that never fired used to look exactly like a healthy start: the
        // engine ran, mic_started was logged, and every sample count stayed zero.
        // Say so out loud instead.
        if buffersSeen == 0 {
            emit(["event": "warning", "source": "mic",
                  "message": "microphone tap received no buffers"])
        }
    }
}

// MARK: - TCC responsibility
//
// macOS attributes microphone and system-audio permission to the RESPONSIBLE
// process, which for a CLI is whatever app launched it (Terminal, the Claude
// app, a Python server). Re-spawn ourselves with responsibility disclaimed, so
// callcap is its own responsible process and TCC uses callcap's embedded
// Info.plist and identifier, whoever launches it. stdout/stderr are inherited;
// the parent only forwards signals and propagates the exit code.
// CALLCAP_DISCLAIMED=1 skips the re-spawn (then permissions attach to the launcher).

typealias DisclaimFunction = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32

var forwardedSignals: [DispatchSourceSignal] = []

func relaunchDisclaimedIfNeeded() {
    if ProcessInfo.processInfo.environment["CALLCAP_DISCLAIMED"] != nil { return }
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim"),
          let path = Bundle.main.executablePath else {
        emit(["event": "warning", "message": "cannot disclaim responsibility; permissions attach to the parent app"])
        return
    }
    let disclaim = unsafeBitCast(symbol, to: DisclaimFunction.self)
    var attributes: posix_spawnattr_t? = nil
    posix_spawnattr_init(&attributes)
    _ = disclaim(&attributes, 1)
    var environment = ProcessInfo.processInfo.environment
    environment["CALLCAP_DISCLAIMED"] = "1"
    var envp: [UnsafeMutablePointer<CChar>?] = environment.map { strdup("\($0.key)=\($0.value)") }
    envp.append(nil)
    var argv: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.map { strdup($0) }
    argv.append(nil)
    var child: pid_t = 0
    let rc = posix_spawn(&child, path, nil, &attributes, argv, envp)
    posix_spawnattr_destroy(&attributes)
    if rc != 0 {
        emit(["event": "warning", "message": "disclaimed spawn failed (\(rc)); running in-process"])
        return
    }
    for sig in [SIGINT, SIGTERM] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { kill(child, sig) }
        source.resume()
        forwardedSignals.append(source)
    }
    DispatchQueue.global().async {
        var status: Int32 = 0
        waitpid(child, &status, 0)
        let signaled = status & 0x7f
        exit(signaled == 0 ? (status >> 8) & 0xff : 128 + signaled)
    }
    dispatchMain()
}

// MARK: - main

signal(SIGPIPE, SIG_IGN)
let respawned = ProcessInfo.processInfo.environment["CALLCAP_DISCLAIMED"] == "1"
relaunchDisclaimedIfNeeded()
let options = parseOptions()
let writer = FrameWriter()
var systemTap: SystemTap?
var microphone: MicCapture?

// Stop requests are handled on their own queue and installed before anything that can block the
// main thread (the microphone permission prompt), so a SIGTERM during that prompt still stops the
// capture instead of leaving the mic hot. If the launcher (or the server) dies, this process follows.
let controlQueue = DispatchQueue(label: "callcap.control")
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let interruptSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: controlQueue)
interruptSource.setEventHandler { shutdown(0) }
interruptSource.resume()
let terminateSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: controlQueue)
terminateSource.setEventHandler { shutdown(0) }
terminateSource.resume()
let parentSource = DispatchSource.makeProcessSource(identifier: getppid(), eventMask: .exit, queue: controlQueue)
parentSource.setEventHandler { shutdown(0) }
parentSource.resume()

let micAuthorization: String = {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}()
emit(["event": "starting", "sample_rate": options.sampleRate, "mic": options.mic,
      "system": options.system, "voice_processing": options.voiceProcessing,
      "pid": Int(ProcessInfo.processInfo.processIdentifier), "own_responsibility": respawned,
      "mic_authorization": micAuthorization])

if options.system {
    let tap = SystemTap()
    do {
        try tap.start(writer: writer, rate: options.sampleRate)
        systemTap = tap
    } catch {
        tap.stop()
        emit(["event": "error", "source": "system", "message": "\(error)"])
    }
}

if options.mic {
    if microphoneAllowed() {
        var started = false
        for useVP in (options.voiceProcessing ? [true, false] : [false]) {
            let mic = MicCapture()
            do {
                try mic.start(writer: writer, rate: options.sampleRate, voiceProcessing: useVP)
                microphone = mic
                started = true
                break
            } catch {
                mic.stop()
                emit(["event": "error", "source": "mic", "voice_processing": useVP, "message": "\(error)"])
            }
        }
        if !started { emit(["event": "error", "source": "mic", "message": "microphone could not start"]) }
    } else {
        emit(["event": "error", "source": "mic", "message": "microphone permission denied"])
    }
}

if systemTap == nil && microphone == nil {
    emit(["event": "fatal", "message": "no capture source started"])
    exit(2)
}

func shutdown(_ code: Int32) -> Never {
    microphone?.stop()
    systemTap?.stop()
    var final = writer.snapshot()
    final["event"] = "stopped"
    emit(final)
    exit(code)
}

let heartbeat = DispatchSource.makeTimerSource(queue: .main)
heartbeat.schedule(deadline: .now() + 2, repeating: 2)
heartbeat.setEventHandler {
    var beat = writer.snapshot()
    beat["event"] = "heartbeat"
    emit(beat)
}
heartbeat.resume()

if let seconds = options.duration {
    DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { shutdown(0) }
}

dispatchMain()
